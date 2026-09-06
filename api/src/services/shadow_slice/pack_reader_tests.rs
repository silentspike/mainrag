//! Real files and controlled SQL transitions, with no production database access.

use super::*;
use anyhow::ensure;
use futures::FutureExt;
use std::panic::AssertUnwindSafe;
use std::time::Duration;
use tokio_postgres::{Client, NoTls};

const PRINCIPAL: &str = "00000000-0000-4000-8000-000000000011";

struct Directory(std::path::PathBuf);

impl Drop for Directory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

async fn open(
    config: &tokio_postgres::Config,
) -> Result<(
    Client,
    tokio::task::JoinHandle<Result<(), tokio_postgres::Error>>,
)> {
    let (client, connection) = config.connect(NoTls).await?;
    Ok((client, tokio::spawn(connection)))
}

async fn install(client: &Client) -> Result<()> {
    // Only prerequisites for the real pack migrations. This fixture does not
    // substitute for the full-schema authorization gate in the Python suite.
    client
        .batch_execute(&format!(
            "
        CREATE TABLE artifact_version(id BIGINT PRIMARY KEY, raw_body_id BIGINT);
        CREATE TABLE storage_v2_gc_epoch(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_id BIGINT NOT NULL, status TEXT NOT NULL);
        CREATE FUNCTION storage_v2_is_admin() RETURNS BOOLEAN LANGUAGE SQL AS
            $$ SELECT current_setting('app.user_id', TRUE) = '{PRINCIPAL}' $$;
        CREATE FUNCTION storage_v2_can_access_source(BIGINT, TEXT) RETURNS BOOLEAN
            LANGUAGE SQL AS $$ SELECT FALSE $$;
        SET app.user_id = '{PRINCIPAL}';
    "
        ))
        .await?;
    client
        .batch_execute(include_str!(
            "../../../../migrations/030_storage_v2_content_bodies.sql"
        ))
        .await?;
    client
        .batch_execute(include_str!(
            "../../../../migrations/055_storage_v2_pack_epoch_commit_fence.sql"
        ))
        .await?;
    client
        .batch_execute(include_str!(
            "../../../../migrations/056_storage_v2_pack_removal_receipts.sql"
        ))
        .await?;
    Ok(())
}

async fn register(
    client: &mut Client,
    pack: &super::super::content_store::PublishedPack,
    existing: Option<i64>,
) -> Result<i64> {
    let entry = &pack.manifest.entries[0];
    let id = pack.manifest.pack_id;
    let transaction = client.transaction().await?;
    content_body::create_pack(&transaction, id, &format!("{id}.pack"), Uuid::new_v4()).await?;
    let body = if let Some(body) = existing {
        body
    } else {
        transaction
            .query_one(
                "INSERT INTO content_body(digest_algorithm,digest,logical_length,pack_id) \
             VALUES ('sha256-v1',$1,$2,$3) RETURNING id",
                &[
                    &entry.body.digest.as_slice(),
                    &i64::try_from(entry.body.logical_length)?,
                    &id,
                ],
            )
            .await?
            .get(0)
    };
    transaction.execute(
        "INSERT INTO content_pack_entry(pack_id,ordinal,body_id,pack_offset,stored_length,codec,entry_digest) \
         VALUES ($1,0,$2,0,$3,$4::TEXT::storage_v2_body_codec,$5)",
        &[&id, &body, &i64::try_from(entry.stored_length)?, &entry.codec.database_name(), &entry.entry_digest.as_slice()],
    ).await?;
    content_body::verify_pack(
        &transaction,
        id,
        &pack.manifest.sha256,
        i64::try_from(pack.manifest.stored_bytes)?,
    )
    .await?;
    content_body::publish_pack(&transaction, id).await?;
    transaction.commit().await?;
    Ok(body)
}

fn pack(
    root: &Path,
    bytes: &[u8],
    codec: BodyCodec,
) -> Result<super::super::content_store::PublishedPack> {
    let mut builder = PackBuilder::new(root, Uuid::new_v4(), Uuid::new_v4(), 4096)?;
    let entry = builder.add_reader(Cursor::new(bytes), codec, None)?;
    let sealed = builder.seal()?;
    sealed.verify_entry(&entry, None)?;
    Ok(sealed.publish()?)
}

async fn open_epochs(client: &Client) -> Result<i64> {
    Ok(client
        .query_one(
            "SELECT count(*) FROM content_reader_epoch WHERE finished_at IS NULL",
            &[],
        )
        .await?
        .get(0))
}

async fn register_multiple(
    client: &mut Client,
    pack: &super::super::content_store::PublishedPack,
) -> Result<Vec<i64>> {
    let transaction = client.transaction().await?;
    let id = pack.manifest.pack_id;
    content_body::create_pack(&transaction, id, &format!("{id}.pack"), Uuid::new_v4()).await?;
    let mut bodies = Vec::new();
    for entry in &pack.manifest.entries {
        let body: i64 = transaction.query_one("INSERT INTO content_body(digest_algorithm,digest,logical_length,pack_id) VALUES('sha256-v1',$1,$2,$3) RETURNING id", &[&entry.body.digest.as_slice(), &i64::try_from(entry.body.logical_length)?, &id]).await?.get(0);
        transaction.execute("INSERT INTO content_pack_entry(pack_id,ordinal,body_id,pack_offset,stored_length,codec,entry_digest) VALUES($1,$2,$3,$4,$5,$6::TEXT::storage_v2_body_codec,$7)", &[&id,&i64::try_from(entry.ordinal)?,&body,&i64::try_from(entry.pack_offset)?,&i64::try_from(entry.stored_length)?,&entry.codec.database_name(),&entry.entry_digest.as_slice()]).await?;
        bodies.push(body);
    }
    content_body::verify_pack(
        &transaction,
        id,
        &pack.manifest.sha256,
        i64::try_from(pack.manifest.stored_bytes)?,
    )
    .await?;
    content_body::publish_pack(&transaction, id).await?;
    transaction.commit().await?;
    Ok(bodies)
}

async fn exercise_maintenance(client: &mut Client, observer: &Client, root: &Path) -> Result<()> {
    use super::super::pack_maintenance::{self, RepackPolicy};
    let incomplete = Uuid::new_v4();
    content_body::create_pack(
        client,
        incomplete,
        &format!("{incomplete}.pack"),
        Uuid::new_v4(),
    )
    .await?;
    client.execute("INSERT INTO content_pack_entry(pack_id,ordinal,body_id,pack_offset,stored_length,codec,entry_digest) SELECT $1,0,id,0,1,'identity',decode(repeat('00',32),'hex') FROM content_body ORDER BY id LIMIT 1", &[&incomplete]).await?;
    let incomplete_policy = RepackPolicy {
        minimum_dead_bytes: 0,
        minimum_dead_basis_points: 0,
        max_entries: 16,
        max_logical_bytes: 1048576,
        reserve_free_bytes: 0,
        io_buffer_bytes: 4096,
        codec: BodyCodec::Zstd,
    };
    let error = pack_maintenance::repack(
        client,
        root,
        incomplete,
        Uuid::new_v4(),
        0,
        &incomplete_policy,
    )
    .await
    .expect_err("incomplete manifest must be rejected");
    ensure!(error.to_string().contains("completed publication"));
    for (case, failure) in [(0_u8, "insert"), (1_u8, "switch")] {
        let live = vec![b'a' + case; 256 * 1024];
        let dead = vec![b'c' + case; 128 * 1024];
        let mut builder = PackBuilder::new(root, Uuid::new_v4(), Uuid::new_v4(), 4096)?;
        builder.add_reader(Cursor::new(&dead), BodyCodec::Identity, None)?;
        builder.add_reader(Cursor::new(&live), BodyCodec::Identity, None)?;
        let old = builder.seal()?.publish()?;
        let mut bodies = register_multiple(client, &old).await?;
        bodies.swap(0, 1); // The surviving entry deliberately has a nonzero source offset.
                           // This entry already has a different physical home. Repacking must use
                           // authoritative placements, not the old pack's advisory live_bytes.
        let other = pack(root, &dead, BodyCodec::Zstd)?;
        register(client, &other, Some(bodies[1])).await?;
        client
            .execute(
                "UPDATE content_body SET pack_id=$1 WHERE id=$2",
                &[&other.manifest.pack_id, &bodies[1]],
            )
            .await?;
        client.execute("INSERT INTO artifact_version(id,raw_body_id) SELECT $1::BIGINT+value,$2 FROM generate_series(1,8) value", &[&(i64::from(case)*100),&bodies[0]]).await?;
        let new = Uuid::new_v4();
        let gc: i64 = client.query_one("INSERT INTO storage_v2_gc_epoch(source_id,status) VALUES(NULL,'verified') RETURNING id", &[]).await?.get(0);
        let policy = RepackPolicy {
            minimum_dead_bytes: 1,
            minimum_dead_basis_points: 1000,
            max_entries: 16,
            max_logical_bytes: 1024 * 1024,
            reserve_free_bytes: 0,
            io_buffer_bytes: 4096,
            codec: BodyCodec::Zstd,
        };
        let mut rejected = policy.clone();
        rejected.reserve_free_bytes = u64::MAX;
        ensure!(
            pack_maintenance::repack(client, root, old.manifest.pack_id, new, gc, &rejected)
                .await
                .is_err()
        );
        ensure!(!root.join(format!("{new}.pack")).exists());
        rejected = policy.clone();
        rejected.minimum_dead_basis_points = 10000;
        ensure!(
            pack_maintenance::repack(client, root, old.manifest.pack_id, new, gc, &rejected)
                .await
                .is_err()
        );
        let lock = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(root.join(".maintenance.lock"))?;
        lock.try_lock()?;
        ensure!(
            pack_maintenance::repack(client, root, old.manifest.pack_id, new, gc, &policy)
                .await
                .is_err()
        );
        drop(lock);
        let predicate = if failure == "insert" {
            format!("NEW.id='{new}'")
        } else {
            format!("NEW.id='{}' AND NEW.status='retired'", old.manifest.pack_id)
        };
        client.batch_execute(&format!("CREATE FUNCTION fixture_repack_failure() RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN IF {predicate} THEN RAISE EXCEPTION 'fixture repack interruption'; END IF; RETURN NEW; END $$; CREATE TRIGGER fixture_repack_failure BEFORE INSERT OR UPDATE ON content_pack FOR EACH ROW EXECUTE FUNCTION fixture_repack_failure();")).await?;
        ensure!(
            pack_maintenance::repack(client, root, old.manifest.pack_id, new, gc, &policy)
                .await
                .is_err()
        );
        ensure!(old.path.exists() && root.join(format!("{new}.pack")).exists());
        ensure!(
            client
                .query_one(
                    "SELECT pack_id FROM content_body WHERE id=$1",
                    &[&bodies[0]]
                )
                .await?
                .get::<_, Uuid>(0)
                == old.manifest.pack_id
        );
        ensure!(open_epochs(client).await? == 0);
        client.batch_execute("DROP TRIGGER fixture_repack_failure ON content_pack; DROP FUNCTION fixture_repack_failure();").await?;
        let reader_epoch = content_body::begin_reader_epoch(observer).await?;
        let report =
            pack_maintenance::repack(client, root, old.manifest.pack_id, new, gc, &policy).await?;
        ensure!(
            report.moved_entries == 1
                && report.excluded_entry_bytes == dead.len() as u64
                && report.moved_logical_bytes == live.len() as u64
        );
        ensure!(report.new_file_bytes < report.old_file_bytes && !report.resumed_after_switch);
        let retry =
            pack_maintenance::repack(client, root, old.manifest.pack_id, new, gc, &policy).await?;
        ensure!(
            retry.resumed_after_switch && retry.excluded_entry_bytes == report.excluded_entry_bytes
        );
        ensure!(pack_maintenance::repack(
            client,
            root,
            old.manifest.pack_id,
            Uuid::new_v4(),
            gc,
            &policy
        )
        .await
        .is_err());
        ensure!(
            pack_maintenance::finish(client, root, old.manifest.pack_id, 16, 4096)
                .await
                .is_err()
        );
        old.reader()
            .verify_integrity(&old.manifest.entries[1], None, 4096)?;
        ensure!(old.path.exists());
        content_body::end_reader_epoch(observer, reader_epoch).await?;
        ensure!(
            pack_maintenance::finish(client, root, old.manifest.pack_id, 16, 4096)
                .await
                .is_err(),
            "GC authority must not advance implicitly"
        );
        client
            .execute(
                "UPDATE storage_v2_gc_epoch SET status='sweeping' WHERE id=$1",
                &[&gc],
            )
            .await?;
        let new_path = root.join(format!("{new}.pack"));
        let verified_bytes = std::fs::read(&new_path)?;
        std::fs::write(&new_path, b"damaged fixture")?;
        ensure!(
            pack_maintenance::finish(client, root, old.manifest.pack_id, 16, 4096)
                .await
                .is_err()
        );
        ensure!(old.path.exists());
        std::fs::write(&new_path, verified_bytes)?;
        let before: i64 = client
            .query_one(
                "SELECT reclaimed_bytes FROM storage_v2_content_metrics",
                &[],
            )
            .await?
            .get(0);
        client.batch_execute("CREATE FUNCTION fixture_receipt_failure() RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture receipt interruption'; END $$; CREATE TRIGGER fixture_receipt_failure BEFORE INSERT ON storage_v2_pack_removal_receipt FOR EACH ROW EXECUTE FUNCTION fixture_receipt_failure();").await?;
        ensure!(
            pack_maintenance::finish(client, root, old.manifest.pack_id, 16, 4096)
                .await
                .is_err()
        );
        ensure!(!old.path.exists() && new_path.exists());
        ensure!(
            client
                .query_one(
                    "SELECT reclaimed_bytes FROM storage_v2_content_metrics",
                    &[]
                )
                .await?
                .get::<_, i64>(0)
                == before
        );
        client.batch_execute("DROP TRIGGER fixture_receipt_failure ON storage_v2_pack_removal_receipt; DROP FUNCTION fixture_receipt_failure();").await?;
        let removed =
            pack_maintenance::finish(client, root, old.manifest.pack_id, 16, 4096).await?;
        ensure!(
            !removed.unlinked_this_call
                && !removed.receipt_already_present
                && removed.file_bytes == report.old_file_bytes
        );
        ensure!(
            pack_maintenance::finish(client, root, old.manifest.pack_id, 16, 4096)
                .await?
                .receipt_already_present
        );
        ensure!(
            client
                .query_one(
                    "SELECT reclaimed_bytes FROM storage_v2_content_metrics",
                    &[]
                )
                .await?
                .get::<_, i64>(0)
                == before + i64::try_from(report.old_file_bytes)?
        );
        ensure!(
            client
                .query_one(
                    "SELECT count(*) FROM artifact_version WHERE raw_body_id=$1",
                    &[&bodies[0]]
                )
                .await?
                .get::<_, i64>(0)
                == 8
        );
        ensure!(
            find_and_verify_existing_body(client, root, &live, 4096)
                .await?
                .context("moved body missing")?
                .id
                == bodies[0]
        );
        ensure!(
            find_and_verify_existing_body(client, root, &dead, 4096)
                .await?
                .context("retained body missing")?
                .id
                == bodies[1]
        );
        ensure!(open_epochs(client).await? == 0);
    }
    println!("pack maintenance: policy/lock rejection, dead-entry exclusion, publication/switch rollback retry, reader and GC gates, corruption retention, unlink/receipt recovery and stable 1:n anchors PASS");
    Ok(())
}

async fn exercise(client: &mut Client, observer: &Client, root: &Path) -> Result<()> {
    let bytes = vec![b'x'; 256 * 1024];
    let old = pack(root, &bytes, BodyCodec::Identity)?;
    let body = register(client, &old, None).await?;
    // The real existing-body consumer verifies both identity and exact bytes.
    ensure!(
        find_and_verify_existing_body(client, root, &bytes, 4096)
            .await?
            .context("body missing")?
            .id
            == body
    );
    ensure!(open_epochs(client).await? == 0);

    let before: i64 = client
        .query_one("SELECT count(*) FROM content_reader_epoch", &[])
        .await?
        .get(0);
    content_body::with_reader_epoch(&*client, async {
        for _ in 0..8 {
            ensure!(
                find_and_verify_existing_body_in_epoch(client, root, &bytes, 4096)
                    .await?
                    .is_some()
            );
        }
        Ok(())
    })
    .await?;
    let after: i64 = client
        .query_one("SELECT count(*) FROM content_reader_epoch", &[])
        .await?
        .get(0);
    ensure!(
        after == before + 1,
        "a reuse batch must share one reader epoch"
    );

    let replacement = pack(root, &bytes, BodyCodec::Zstd)?;
    register(client, &replacement, Some(body)).await?;
    let gc: i64 = client.query_one(
        "INSERT INTO storage_v2_gc_epoch(source_id,status) VALUES(NULL,'verified') RETURNING id", &[],
    ).await?.get(0);
    let acquired = tokio::sync::Notify::new();
    let release = tokio::sync::Notify::new();
    let reader = content_body::with_reader_epoch(&*client, async {
        let selected: Uuid = client
            .query_one("SELECT pack_id FROM content_body WHERE id=$1", &[&body])
            .await?
            .get(0);
        ensure!(selected == old.manifest.pack_id);
        acquired.notify_one();
        release.notified().await;
        let verified =
            old.reader()
                .verify_to_staging(&old.manifest.entries[0], None, root, 4096)?;
        let mut actual = Vec::new();
        verified.copy_to(&mut actual)?;
        ensure!(actual == bytes);
        Ok(())
    });
    let switch = async {
        acquired.notified().await;
        let result: Result<()> = async {
            ensure!(
                content_body::switch_pack(
                    observer,
                    old.manifest.pack_id,
                    replacement.manifest.pack_id,
                    gc
                )
                .await?
                    == 1
            );
            ensure!(
                content_body::mark_pack_readers_drained(observer, old.manifest.pack_id)
                    .await
                    .is_err()
            );
            ensure!(content_body::reclaim_pack(observer, old.manifest.pack_id)
                .await
                .is_err());
            ensure!(old.path.exists());
            // This is the production packed-reuse consumer after the switch.
            let found = find_and_verify_existing_body(observer, root, &bytes, 4096)
                .await?
                .context("replacement missing")?;
            ensure!(found.id == body && found.pack_id == Some(replacement.manifest.pack_id));
            Ok(())
        }
        .await;
        release.notify_one();
        result
    };
    let (read, switched) = tokio::join!(reader, switch);
    read?;
    switched?;
    ensure!(open_epochs(client).await? == 0);
    content_body::mark_pack_readers_drained(client, old.manifest.pack_id).await?;
    client
        .execute(
            "UPDATE storage_v2_gc_epoch SET status='sweeping' WHERE id=$1",
            &[&gc],
        )
        .await?;
    content_body::reclaim_pack(client, old.manifest.pack_id).await?;
    let old_path = old.path.clone();
    ensure!(old.remove_after_database_reclamation()? == bytes.len() as u64);
    ensure!(!old_path.exists() && replacement.path.exists());
    ensure!(find_and_verify_existing_body(client, root, &bytes, 4096)
        .await?
        .is_some());

    // Failed verification must close the epoch, but return no trusted body.
    std::fs::write(&replacement.path, b"corrupt")?;
    ensure!(find_and_verify_existing_body(client, root, &bytes, 4096)
        .await
        .is_err());
    ensure!(open_epochs(client).await? == 0);

    // Cancellation after registration must NOT mark unfinished I/O drained.
    let started = tokio::sync::Notify::new();
    {
        let mut pending = Box::pin(content_body::with_reader_epoch(&*client, async {
            started.notify_one();
            std::future::pending::<Result<()>>().await
        }));
        tokio::select! {
            _ = started.notified() => {},
            _ = &mut pending => anyhow::bail!("fixture reader unexpectedly completed"),
        }
    }
    ensure!(open_epochs(client).await? == 1);
    // Only the fixture knows its cancelled future is gone and no file reader
    // survives. Production must establish that fact before equivalent recovery.
    let epoch: Uuid = client
        .query_one(
            "SELECT id FROM content_reader_epoch WHERE finished_at IS NULL",
            &[],
        )
        .await?
        .get(0);
    content_body::end_reader_epoch(client, epoch).await?;
    ensure!(open_epochs(client).await? == 0);
    println!("pack readers: real old/new bytes, concurrent switch, drain rejection, guarded reuse, corruption failure and cancellation retention PASS");
    exercise_maintenance(client, observer, root).await?;
    Ok(())
}

#[tokio::test]
#[ignore = "requires isolated PostgreSQL fixture; executed by protected CI"]
async fn postgres_pack_readers_retain_real_bytes_through_switch() -> Result<()> {
    let url = std::env::var("MAINRAG_INDEX_TEST_DATABASE_URL")
        .context("explicit fixture URL required")?;
    let mut config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("mainrag_index_fixture"),
        "refusing non-fixture database"
    );
    let (admin, admin_task) = open(&config).await?;
    let database = format!("pack_readers_{}", Uuid::new_v4().simple());
    admin
        .batch_execute(&format!("CREATE DATABASE {database}"))
        .await?;
    config.dbname(&database);
    let directory = Directory(std::env::temp_dir().join(format!("mainrag-{database}")));
    let result = AssertUnwindSafe(tokio::time::timeout(Duration::from_secs(60), async {
        let (mut client, task) = open(&config).await?;
        let (observer, observer_task) = open(&config).await?;
        install(&client).await?;
        observer
            .batch_execute(&format!("SET app.user_id='{PRINCIPAL}'"))
            .await?;
        let result = exercise(&mut client, &observer, &directory.0).await;
        drop(client);
        drop(observer);
        task.await??;
        observer_task.await??;
        result
    }))
    .catch_unwind()
    .await;
    let cleanup = admin
        .batch_execute(&format!("DROP DATABASE {database} WITH (FORCE)"))
        .await;
    drop(admin);
    admin_task.await??;
    cleanup?;
    result
        .map_err(|_| anyhow::anyhow!("pack fixture panicked"))?
        .context("pack fixture exceeded time budget")?
}
