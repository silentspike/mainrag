//! SIGKILL tests against generated files and the parent's owned disposable DB.
//! These test application-process loss, not PostgreSQL or machine power loss.

use super::*;
use crate::services::pack_maintenance::{self, RepackPolicy};
use std::os::unix::process::ExitStatusExt;
use std::process::Stdio;

const CHILD_TEST: &str =
    "services::shadow_slice::pack_reader_tests::process_crashes::pack_crash_child";

fn policy() -> RepackPolicy {
    RepackPolicy {
        minimum_dead_bytes: 0,
        minimum_dead_basis_points: 0,
        max_entries: 16,
        max_logical_bytes: 1048576,
        reserve_free_bytes: 0,
        io_buffer_bytes: 4096,
        codec: BodyCodec::Zstd,
    }
}

#[tokio::test]
#[ignore = "child entrypoint; invoked only by the owned process-crash fixture"]
async fn pack_crash_child() -> Result<()> {
    let mut config: tokio_postgres::Config =
        std::env::var("MAINRAG_INDEX_TEST_DATABASE_URL")?.parse()?;
    ensure!(config.get_dbname() == Some("mainrag_index_fixture"));
    let database = std::env::var("MAINRAG_PACK_CRASH_DATABASE")?;
    let suffix = database
        .strip_prefix("pack_readers_")
        .context("fixture database required")?;
    let owner = Uuid::parse_str(suffix)?;
    ensure!(database == format!("pack_readers_{}", owner.simple()));
    let old: Uuid = std::env::var("MAINRAG_PACK_CRASH_ID")?.parse()?;
    let new: Uuid = std::env::var("MAINRAG_PACK_CRASH_NEW")?.parse()?;
    let gc: i64 = std::env::var("MAINRAG_PACK_CRASH_GC")?.parse()?;
    let stage = std::env::var("MAINRAG_PACK_CRASH_STAGE")?;
    let root = std::env::temp_dir()
        .join(format!("mainrag-{database}"))
        .join(format!("crash-{new}"));
    ensure!(root.is_dir(), "parent must create the fixture root");
    config
        .dbname(&database)
        .application_name(&format!("pack-crash-{new}"));
    let (mut client, _connection) = open(&config).await?;
    client
        .batch_execute(&format!(
            "SET app.user_id='{PRINCIPAL}'; SET statement_timeout='10s'"
        ))
        .await?;
    if matches!(
        stage.as_str(),
        "before_unlink" | "after_unlink" | "after_receipt"
    ) {
        pack_maintenance::finish(&mut client, &root, old, 16, 4096).await?;
    } else {
        pack_maintenance::repack(&mut client, &root, old, new, gc, &policy()).await?;
    }
    anyhow::bail!("child returned without reaching its crash checkpoint")
}

async fn kill_at_checkpoint(
    client: &Client,
    database: &str,
    root: &Path,
    old: Uuid,
    new: Uuid,
    gc: i64,
    stage: &str,
) -> Result<()> {
    let mut child = tokio::process::Command::new(std::env::current_exe()?)
        .args([CHILD_TEST, "--ignored", "--exact", "--nocapture"])
        .env("MAINRAG_PACK_CRASH_DATABASE", database)
        .env("MAINRAG_PACK_CRASH_ID", old.to_string())
        .env("MAINRAG_PACK_CRASH_NEW", new.to_string())
        .env("MAINRAG_PACK_CRASH_GC", gc.to_string())
        .env("MAINRAG_PACK_CRASH_STAGE", stage)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()?;
    let reached = tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            if root.join("fixture-checkpoint").try_exists()? {
                ensure!(std::fs::read_to_string(root.join("fixture-checkpoint"))? == stage);
                return Ok::<_, anyhow::Error>(());
            }
            ensure!(
                child.try_wait()?.is_none(),
                "child exited before checkpoint {stage}"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await;
    // Always terminate and reap this exact owned child, also on checkpoint failure.
    child.kill().await?;
    let status = child.wait().await?;
    ensure!(
        status.signal() == Some(9),
        "expected real SIGKILL at {stage}"
    );
    reached.context("child checkpoint timed out")??;
    // Server rollback is asynchronous after client death. Wait for that exact
    // connection to disappear, never terminate arbitrary database backends.
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            let count: i64 = client.query_one("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND application_name=$1", &[&format!("pack-crash-{new}")]).await?.get(0);
            if count == 0 { return Ok::<_, anyhow::Error>(()); }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }).await.context("killed fixture connection did not drain")??;
    Ok(())
}

pub(super) async fn exercise(client: &mut Client, observer: &Client, root: &Path) -> Result<()> {
    let database: String = client
        .query_one("SELECT current_database()", &[])
        .await?
        .get(0);
    for (case, stage) in [
        "before_publish",
        "after_publish",
        "after_switch",
        "after_commit",
        "before_unlink",
        "after_unlink",
        "after_receipt",
    ]
    .into_iter()
    .enumerate()
    {
        let new = Uuid::new_v4();
        let case_root = root.join(format!("crash-{new}"));
        let bytes = [vec![case as u8 + 10; 65536], vec![case as u8 + 30; 32768]];
        let mut builder = PackBuilder::new(&case_root, Uuid::new_v4(), Uuid::new_v4(), 4096)?;
        for body in &bytes {
            builder.add_reader(Cursor::new(body), BodyCodec::Identity, None)?;
        }
        let old = builder.seal()?.publish()?;
        let ids = register_multiple(client, &old).await?;
        for (index, id) in ids.iter().enumerate() {
            let anchor = 1000 + i64::try_from(case * 100 + index * 10)?;
            client.execute("INSERT INTO artifact_version(id,raw_body_id) SELECT $1::BIGINT+value,$2 FROM generate_series(1,4) value", &[&anchor,id]).await?;
        }
        let gc: i64 = client.query_one("INSERT INTO storage_v2_gc_epoch(source_id,status) VALUES(NULL,'verified') RETURNING id", &[]).await?.get(0);
        let removal = matches!(stage, "before_unlink" | "after_unlink" | "after_receipt");
        let reader_epoch = content_body::begin_reader_epoch(observer).await?;
        if removal {
            pack_maintenance::repack(client, &case_root, old.manifest.pack_id, new, gc, &policy())
                .await?;
            content_body::end_reader_epoch(observer, reader_epoch).await?;
            client
                .execute(
                    "UPDATE storage_v2_gc_epoch SET status='sweeping' WHERE id=$1",
                    &[&gc],
                )
                .await?;
        }
        kill_at_checkpoint(
            client,
            &database,
            &case_root,
            old.manifest.pack_id,
            new,
            gc,
            stage,
        )
        .await?;
        let switched = removal || stage == "after_commit";
        let placement = if switched { new } else { old.manifest.pack_id };
        for id in &ids {
            ensure!(
                client
                    .query_one("SELECT pack_id FROM content_body WHERE id=$1", &[id])
                    .await?
                    .get::<_, Uuid>(0)
                    == placement,
                "partial placement at {stage}"
            );
            ensure!(
                client
                    .query_one(
                        "SELECT count(*) FROM artifact_version WHERE raw_body_id=$1",
                        &[id]
                    )
                    .await?
                    .get::<_, i64>(0)
                    == 4
            );
        }
        let new_path = case_root.join(format!("{new}.pack"));
        ensure!(new_path.exists() == (stage != "before_publish"));
        ensure!(old.path.exists() == !matches!(stage, "after_unlink" | "after_receipt"));
        let old_status: String = client
            .query_one(
                "SELECT status::TEXT FROM content_pack WHERE id=$1",
                &[&old.manifest.pack_id],
            )
            .await?
            .get(0);
        ensure!(
            old_status
                == if removal {
                    "reclaimed"
                } else if switched {
                    "retired"
                } else {
                    "published"
                }
        );
        if stage == "before_publish" {
            ensure!(
                std::fs::read_dir(case_root.join(".building"))?
                    .next()
                    .is_some(),
                "SIGKILL must leave identifiable staging"
            );
        }
        for (body, id) in bytes.iter().zip(&ids) {
            ensure!(
                find_and_verify_existing_body(client, &case_root, body, 4096)
                    .await?
                    .context("crash lost body")?
                    .id
                    == *id
            );
        }
        if !removal {
            // A separately registered reader survives the writer process crash.
            for entry in &old.manifest.entries {
                old.reader().verify_integrity(entry, None, 4096)?;
            }
            let retry = pack_maintenance::repack(
                client,
                &case_root,
                old.manifest.pack_id,
                new,
                gc,
                &policy(),
            )
            .await?;
            ensure!(retry.resumed_after_switch == switched && retry.moved_entries == 2);
            ensure!(
                pack_maintenance::finish(client, &case_root, old.manifest.pack_id, 16, 4096)
                    .await
                    .is_err()
            );
            content_body::end_reader_epoch(observer, reader_epoch).await?;
            client
                .execute(
                    "UPDATE storage_v2_gc_epoch SET status='sweeping' WHERE id=$1",
                    &[&gc],
                )
                .await?;
        }
        let receipt_before: i64 = client
            .query_one(
                "SELECT count(*) FROM storage_v2_pack_removal_receipt WHERE pack_id=$1",
                &[&old.manifest.pack_id],
            )
            .await?
            .get(0);
        ensure!(receipt_before == i64::from(stage == "after_receipt"));
        let result =
            pack_maintenance::finish(client, &case_root, old.manifest.pack_id, 16, 4096).await?;
        ensure!(result.receipt_already_present == (stage == "after_receipt"));
        ensure!(
            pack_maintenance::finish(client, &case_root, old.manifest.pack_id, 16, 4096)
                .await?
                .receipt_already_present
        );
        ensure!(!old.path.exists() && new_path.exists());
        ensure!(
            client
                .query_one(
                    "SELECT file_bytes FROM storage_v2_pack_removal_receipt WHERE pack_id=$1",
                    &[&old.manifest.pack_id]
                )
                .await?
                .get::<_, i64>(0)
                == i64::try_from(old.manifest.stored_bytes)?
        );
        for (body, id) in bytes.iter().zip(&ids) {
            ensure!(
                find_and_verify_existing_body(client, &case_root, body, 4096)
                    .await?
                    .context("retry lost body")?
                    .id
                    == *id
            );
        }
        ensure!(open_epochs(client).await? == 0);
        println!("pack process crash: {stage}: SIGKILL, atomic placement, byte identity, stable anchors and idempotent recovery PASS");
    }
    Ok(())
}
