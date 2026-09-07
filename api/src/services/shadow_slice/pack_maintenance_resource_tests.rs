//! Fresh-process measurements of the complete PostgreSQL/file maintenance API.
use super::*;
use crate::services::pack_maintenance::{self, RepackPolicy};
use std::process::Stdio;
use std::time::Instant;

fn peak_rss() -> Result<u64> {
    let status = std::fs::read_to_string("/proc/self/status")?;
    let value = status
        .lines()
        .find_map(|line| line.strip_prefix("VmHWM:"))
        .context("VmHWM required")?;
    let fields: Vec<_> = value.split_whitespace().collect();
    ensure!(fields.len() == 2 && fields[1] == "kB");
    fields[0]
        .parse::<u64>()?
        .checked_mul(1024)
        .context("RSS overflow")
}

#[tokio::test]
#[ignore = "owned integrated maintenance measurement child"]
async fn maintenance_resource_child() -> Result<()> {
    let mut config: tokio_postgres::Config =
        std::env::var("MAINRAG_INDEX_TEST_DATABASE_URL")?.parse()?;
    ensure!(config.get_dbname() == Some("mainrag_index_fixture"));
    let database = std::env::var("MAINRAG_MAINTENANCE_DATABASE")?;
    let suffix = database
        .strip_prefix("pack_readers_")
        .context("fixture database required")?;
    let owner = Uuid::parse_str(suffix)?;
    ensure!(database == format!("pack_readers_{}", owner.simple()));
    let old: Uuid = std::env::var("MAINRAG_MAINTENANCE_OLD")?.parse()?;
    let new: Uuid = std::env::var("MAINRAG_MAINTENANCE_NEW")?.parse()?;
    let gc: i64 = std::env::var("MAINRAG_MAINTENANCE_GC")?.parse()?;
    let buffer: usize = std::env::var("MAINRAG_MAINTENANCE_BUFFER")?.parse()?;
    ensure!([4096, 65536].contains(&buffer));
    let root = std::env::temp_dir()
        .join(format!("mainrag-{database}"))
        .join(format!("operator-{new}"));
    ensure!(root.is_dir());
    config.dbname(&database);
    let (mut client, connection) = open(&config).await?;
    client
        .batch_execute(&format!(
            "SET app.user_id='{PRINCIPAL}'; SET statement_timeout='10s'"
        ))
        .await?;
    let baseline = peak_rss()?;
    let policy = RepackPolicy {
        minimum_dead_bytes: 1,
        minimum_dead_basis_points: 1000,
        max_entries: 16,
        max_logical_bytes: 2097152,
        reserve_free_bytes: 0,
        io_buffer_bytes: buffer,
        codec: BodyCodec::Zstd,
    };
    let start = Instant::now();
    let rewrite = pack_maintenance::repack(&mut client, &root, old, new, gc, &policy).await?;
    let repack_ms = start.elapsed().as_secs_f64() * 1000.0;
    let start = Instant::now();
    let removal = pack_maintenance::finish(&mut client, &root, old, 16, buffer).await?;
    let finish_ms = start.elapsed().as_secs_f64() * 1000.0;
    let peak = peak_rss()?;
    ensure!(peak >= baseline && peak <= 128 * 1024 * 1024);
    ensure!(
        rewrite.moved_entries == 2
            && rewrite.moved_logical_bytes == 1114112
            && rewrite.excluded_entry_bytes == 131072
    );
    ensure!(
        !rewrite.resumed_after_switch
            && removal.unlinked_this_call
            && !removal.receipt_already_present
    );
    ensure!(
        removal.file_bytes == rewrite.old_file_bytes && !root.join(format!("{old}.pack")).exists()
    );
    drop(client);
    connection.await??;
    println!(
        "PACK_MAINTENANCE {}",
        serde_json::json!({
            "schema":"pack-maintenance-resource-v1","scope":"integrated_pg_file_operator",
            "profile":if cfg!(debug_assertions){"debug"}else{"release"},"buffer_bytes":buffer,
            "repack_ms":repack_ms,"finish_ms":finish_ms,"process_peak_rss_bytes":peak,
            "process_baseline_hwm_bytes":baseline,"moved_entries":rewrite.moved_entries,
            "logical_bytes":rewrite.moved_logical_bytes,"old_file_bytes":rewrite.old_file_bytes,
            "new_file_bytes":rewrite.new_file_bytes,"dead_entry_bytes":rewrite.excluded_entry_bytes,
            "reclaimed_file_bytes":removal.file_bytes,"integrity_passed":1,
            "database_server_rss_bytes":null,"device_io_bytes":null,"sql_only_ms":null
        })
    );
    Ok(())
}

pub(super) async fn exercise(client: &mut Client, root: &Path) -> Result<()> {
    let database: String = client
        .query_one("SELECT current_database()", &[])
        .await?
        .get(0);
    let mut completed = 0;
    for repetition in 1..=3 {
        for buffer in if repetition % 2 == 1 {
            [4096, 65536]
        } else {
            [65536, 4096]
        } {
            let new = Uuid::new_v4();
            let case_root = root.join(format!("operator-{new}"));
            let live = [
                vec![b'Q' + completed; 65536],
                vec![b'Q' + completed; 1048576],
            ];
            let dead = vec![b'j' + completed; 131072];
            let mut builder = PackBuilder::new(&case_root, Uuid::new_v4(), Uuid::new_v4(), 4096)?;
            for bytes in [&live[0], &live[1], &dead] {
                builder.add_reader(Cursor::new(bytes), BodyCodec::Identity, None)?;
            }
            let old = builder.seal()?.publish()?;
            let ids = register_multiple(client, &old).await?;
            let other = pack(&case_root, &dead, BodyCodec::Zstd)?;
            register(client, &other, Some(ids[2])).await?;
            client
                .execute(
                    "UPDATE content_body SET pack_id=$1 WHERE id=$2",
                    &[&other.manifest.pack_id, &ids[2]],
                )
                .await?;
            for (index, id) in ids[..2].iter().enumerate() {
                let anchor = 10000 + i64::from(completed) * 100 + i64::try_from(index)? * 10;
                client.execute("INSERT INTO artifact_version(id,raw_body_id) SELECT $1::BIGINT+value,$2 FROM generate_series(1,4) value",&[&anchor,id]).await?;
            }
            let gc: i64 = client.query_one("INSERT INTO storage_v2_gc_epoch(source_id,status) VALUES(NULL,'sweeping') RETURNING id",&[]).await?.get(0);
            let child = tokio::process::Command::new(std::env::current_exe()?)
                .args(["services::shadow_slice::pack_reader_tests::maintenance_resources::maintenance_resource_child","--ignored","--exact","--nocapture"])
                .env("MAINRAG_MAINTENANCE_DATABASE",&database).env("MAINRAG_MAINTENANCE_OLD",old.manifest.pack_id.to_string())
                .env("MAINRAG_MAINTENANCE_NEW",new.to_string()).env("MAINRAG_MAINTENANCE_GC",gc.to_string())
                .env("MAINRAG_MAINTENANCE_BUFFER",buffer.to_string())
                .stdin(Stdio::null()).stderr(Stdio::null()).stdout(Stdio::piped()).kill_on_drop(true).spawn()?;
            let output = tokio::time::timeout(Duration::from_secs(20), child.wait_with_output())
                .await
                .context("maintenance child timed out")??;
            ensure!(output.status.success(), "maintenance child failed");
            let stdout = String::from_utf8(output.stdout)?;
            ensure!(stdout.contains("1 passed; 0 failed; 0 ignored;"));
            let lines: Vec<_> = stdout
                .lines()
                .filter_map(|line| line.strip_prefix("PACK_MAINTENANCE "))
                .collect();
            ensure!(lines.len() == 1);
            let mut row: serde_json::Value = serde_json::from_str(lines[0])?;
            ensure!(row["buffer_bytes"] == buffer);
            for (bytes, id) in [&live[0], &live[1], &dead].into_iter().zip(&ids) {
                ensure!(
                    find_and_verify_existing_body(client, &case_root, bytes, 4096)
                        .await?
                        .context("maintenance lost bytes")?
                        .id
                        == *id
                );
            }
            for id in &ids[..2] {
                ensure!(
                    client
                        .query_one("SELECT pack_id FROM content_body WHERE id=$1", &[id])
                        .await?
                        .get::<_, Uuid>(0)
                        == new
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
            ensure!(
                client
                    .query_one(
                        "SELECT file_bytes FROM storage_v2_pack_removal_receipt WHERE pack_id=$1",
                        &[&old.manifest.pack_id]
                    )
                    .await?
                    .get::<_, i64>(0)
                    == 1245184
            );
            ensure!(
                pack_maintenance::finish(client, &case_root, old.manifest.pack_id, 16, buffer)
                    .await?
                    .receipt_already_present
            );
            ensure!(open_epochs(client).await? == 0);
            row["repetition"] = repetition.into();
            println!("PACK_MAINTENANCE {row}");
            completed += 1;
        }
    }
    ensure!(completed == 6);
    Ok(())
}
