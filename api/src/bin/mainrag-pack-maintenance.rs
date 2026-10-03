//! Explicit operator entry point for accepted native GC pack work.
use anyhow::{ensure, Context, Result};
use mainrag_api::services::content_store::BodyCodec;
use mainrag_api::services::pack_maintenance::{self, RepackPolicy};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::Path;
use tokio_postgres::NoTls;
use uuid::Uuid;

fn argument<'a>(args: &'a BTreeMap<String, String>, name: &str) -> Result<&'a str> {
    args.get(name)
        .map(String::as_str)
        .with_context(|| format!("missing {name}"))
}

async fn run() -> Result<()> {
    let mut args = BTreeMap::new();
    let mut input = std::env::args().skip(1);
    while let Some(key) = input.next() {
        ensure!(
            matches!(
                key.as_str(),
                "--connection-file"
                    | "--root"
                    | "--manifest-sha256"
                    | "--pack"
                    | "--replacement"
                    | "--operation"
                    | "--output"
            ),
            "unknown option"
        );
        let value = input.next().context("missing option value")?;
        ensure!(args.insert(key, value).is_none(), "duplicate option");
    }
    let connection_path = Path::new(argument(&args, "--connection-file")?);
    let metadata = fs::symlink_metadata(connection_path)?;
    ensure!(
        metadata.is_file()
            && metadata.permissions().mode() & 0o077 == 0
            && metadata.uid() == unsafe { libc::geteuid() }
            && metadata.len() <= 65536,
        "connection file must be private, owned and bounded"
    );
    let config: tokio_postgres::Config = fs::read_to_string(connection_path)?.trim().parse()?;
    // The local operator uses the same local transport as the application.
    // No unencrypted connection may leave the host.
    ensure!(
        !config.get_hosts().is_empty()
            && config.get_hosts().iter().all(|host| match host {
                tokio_postgres::config::Host::Unix(_) => true,
                tokio_postgres::config::Host::Tcp(host) => host
                    .parse::<std::net::IpAddr>()
                    .map(|address| address.is_loopback())
                    .unwrap_or(false),
            }),
        "maintenance connection must use a Unix socket or loopback address"
    );
    let root = Path::new(argument(&args, "--root")?);
    let manifest = argument(&args, "--manifest-sha256")?;
    ensure!(
        manifest.len() == 64
            && manifest
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "exact GC manifest digest required"
    );
    let pack = Uuid::parse_str(argument(&args, "--pack")?)?;
    let output = Path::new(argument(&args, "--output")?);
    let parent = output
        .parent()
        .context("protected output directory required")?;
    ensure!(
        fs::metadata(parent)?.permissions().mode() & 0o077 == 0,
        "output directory must be private"
    );
    // Create before mutation: an existing attempt must be reconciled, not
    // overwritten. Connection details and raw database errors never print.
    let mut receipt_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(output)?;
    receipt_file.write_all(b"{\"status\":\"DISPATCHED_REQUIRES_READBACK\"}\n")?;
    receipt_file.sync_all()?;
    let (mut client, connection) = config.connect(NoTls).await?;
    let task = tokio::spawn(connection);
    let accepted = client.query_one(
        "SELECT gc_epoch_id,pack_root,maintenance_binary_sha256 FROM storage_v2_gc_pack_authority($1,$2)",
        &[&manifest,&pack]).await?;
    let epoch: i64 = accepted.get(0);
    let mut executable = fs::File::open("/proc/self/exe")?;
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        let count = executable.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
    }
    ensure!(
        Some(format!("{:x}", hash.finalize())).as_deref()
            == accepted.get::<_, Option<String>>(2).as_deref(),
        "maintenance executable differs from the accepted GC manifest"
    );
    ensure!(
        root.canonicalize()?.to_str() == accepted.get::<_, Option<String>>(1).as_deref(),
        "pack root differs from the accepted GC manifest"
    );
    ensure!(
        client
            .query_one("SELECT storage_v2_is_admin()", &[])
            .await?
            .get::<_, bool>(0),
        "administrator authority required"
    );
    let report = match argument(&args, "--operation")? {
        "repack" => {
            let replacement = Uuid::parse_str(argument(&args, "--replacement")?)?;
            let policy = RepackPolicy {
                minimum_dead_bytes: 1,
                minimum_dead_basis_points: 0,
                max_entries: 65536,
                max_logical_bytes: 2 * 1024 * 1024 * 1024,
                reserve_free_bytes: 42 * 1024 * 1024 * 1024,
                io_buffer_bytes: 65536,
                codec: BodyCodec::Zstd,
            };
            serde_json::to_value(
                pack_maintenance::repack(&mut client, root, pack, replacement, epoch, &policy)
                    .await?,
            )?
        }
        "retire-empty" => {
            pack_maintenance::retire_empty(&mut client, root, pack, epoch, 65536, 65536).await?;
            serde_json::json!({"pack_id":pack,"status":"RETIRED_READER_DRAIN_AND_UNLINK_PENDING"})
        }
        "finish" => {
            ensure!(
                client
                    .query_opt(
                        "SELECT 1 FROM content_pack_retirement WHERE pack_id=$1 AND gc_epoch_id=$2",
                        &[&pack, &epoch]
                    )
                    .await?
                    .is_some(),
                "pack belongs to another GC epoch"
            );
            serde_json::to_value(
                pack_maintenance::finish(&mut client, root, pack, 65536, 65536).await?,
            )?
        }
        _ => anyhow::bail!("unknown maintenance operation"),
    };
    drop(client);
    task.await??;
    let result = serde_json::json!({"schema_version":"mainrag.storage-v2.pack-maintenance-receipt.v1",
        "manifest_sha256":manifest,"gc_epoch_id":epoch,"report":report});
    // Keep the dispatch record and publish a separate create-only result.
    let mut complete = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(output.with_extension("committed.json"))?;
    serde_json::to_writer(&mut complete, &result)?;
    complete.write_all(b"\n")?;
    complete.sync_all()?;
    fs::File::open(parent)?.sync_all()?;
    println!("PACK_MAINTENANCE_COMPLETED_PRIVATE_RECEIPT_WRITTEN");
    Ok(())
}

#[tokio::main]
async fn main() {
    if run().await.is_err() {
        eprintln!("PACK_MAINTENANCE_FAILED_OR_OUTCOME_UNKNOWN_RECONCILE_DATABASE_AND_FILES");
        std::process::exit(1);
    }
}
