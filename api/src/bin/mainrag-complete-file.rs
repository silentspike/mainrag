//! Local native complete-file operator and verified source-authorized reader.
//! Root admission owns the one global manifest/account and physical budgets.
use anyhow::{ensure, Context, Result};
use clap::{Parser, ValueEnum};
use mainrag_api::services::legacy_complete_file::{
    self as complete, ExistingFragment, Registration,
};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use tokio_postgres::NoTls;
use uuid::Uuid;

#[derive(Clone, Copy, Debug, ValueEnum)]
enum Operation {
    RegisterExisting,
    RegisterZstd,
    Read,
    Reconcile,
}
#[derive(Parser)]
struct Args {
    #[arg(long, value_enum)]
    operation: Operation,
    #[arg(long)]
    connection_file: PathBuf,
    #[arg(long)]
    request_file: PathBuf,
    #[arg(long)]
    pack_root: PathBuf,
    #[arg(long)]
    output: PathBuf,
    /// Verified data is a separate private file; never printed in receipts.
    #[arg(long)]
    data_output: Option<PathBuf>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    operation_id: Uuid,
    manifest_sha256: String,
    work_origin_sha256: String,
    principal: Uuid,
    registration: Registration,
    #[serde(default)]
    fragments: Vec<ExistingFragment>,
    maximum_encoded_input_bytes: u64,
}
fn protected_bytes(path: &Path, limit: u64) -> Result<Vec<u8>> {
    ensure!(
        path.is_absolute() && path.canonicalize()? == path,
        "canonical protected local file required"
    );
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let before = file.metadata()?;
    ensure!(
        before.is_file()
            && before.uid() == unsafe { libc::geteuid() }
            && before.nlink() == 1
            && before.mode() & 0o777 == 0o600
            && before.len() <= limit,
        "private owned bounded file required"
    );
    let mut bytes = Vec::with_capacity(usize::try_from(before.len())?);
    (&mut file).take(limit + 1).read_to_end(&mut bytes)?;
    let after = file.metadata()?;
    ensure!(
        bytes.len() as u64 <= limit
            && (
                before.dev(),
                before.ino(),
                before.len(),
                before.mtime(),
                before.mtime_nsec(),
                before.ctime(),
                before.ctime_nsec()
            ) == (
                after.dev(),
                after.ino(),
                after.len(),
                after.mtime(),
                after.mtime_nsec(),
                after.ctime(),
                after.ctime_nsec()
            ),
        "protected input changed"
    );
    Ok(bytes)
}
fn private_new(path: &Path) -> Result<std::fs::File> {
    let parent = path.parent().context("owned output parent required")?;
    let metadata = fs::symlink_metadata(parent)?;
    ensure!(
        parent.canonicalize()? == parent
            && metadata.is_dir()
            && metadata.uid() == unsafe { libc::geteuid() }
            && metadata.mode() & 0o077 == 0,
        "canonical private owned output directory required"
    );
    Ok(OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?)
}
fn persist(path: &Path, value: &Value) -> Result<()> {
    let mut file = private_new(path)?;
    serde_json::to_writer(&mut file, value)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    fs::File::open(path.parent().context("output parent required")?)?.sync_all()?;
    Ok(())
}
fn local(config: &tokio_postgres::Config) -> bool {
    !config.get_hosts().is_empty()
        && config.get_hosts().iter().all(|host| match host {
            tokio_postgres::config::Host::Unix(_) => true,
            tokio_postgres::config::Host::Tcp(host) => host
                .parse::<std::net::IpAddr>()
                .is_ok_and(|ip| ip.is_loopback()),
        })
}
async fn run() -> Result<()> {
    let args = Args::parse();
    let request_raw = protected_bytes(&args.request_file, 4 * 1024 * 1024)?;
    let request: Request = serde_json::from_slice(&request_raw)?;
    request.registration.identity.validate()?;
    for hash in [&request.manifest_sha256, &request.work_origin_sha256] {
        let bytes = hex::decode(hash)?;
        ensure!(
            bytes.len() == 32 && hex::encode(bytes) == *hash,
            "exact manifest and original account required"
        );
    }
    ensure!(
        request.registration.maximum_scratch_bytes
            >= request
                .registration
                .identity
                .logical_bytes
                .checked_mul(2)
                .and_then(|n| n.checked_add(65536))
                .context("scratch bound overflow")?,
        "Root must reserve complete plus largest-component reader scratch"
    );
    let connection_raw = protected_bytes(&args.connection_file, 65536)?;
    let config: tokio_postgres::Config = std::str::from_utf8(&connection_raw)?.trim().parse()?;
    ensure!(
        local(&config),
        "local socket or loopback transport required"
    );
    ensure!(
        args.pack_root.canonicalize()? == args.pack_root,
        "canonical native pack root required"
    );
    let ack = args.output.with_file_name(format!(
        "{}.committed.json",
        args.output
            .file_name()
            .context("named output required")?
            .to_string_lossy()
    ));
    ensure!(
        !ack.try_exists()?,
        "existing outcome requires same-intent reconciliation"
    );
    // Create-only durable intent precedes transaction, input streaming and any
    // pack publication. A missing ACK never authorizes redispatch.
    persist(
        &args.output,
        &json!({"schema_version":"mainrag.storage-v2.native-complete-file-attempt.v1",
        "operation_id":request.operation_id,"manifest_sha256":request.manifest_sha256,
        "work_origin_sha256":request.work_origin_sha256,
        "request_sha256":hex::encode(Sha256::digest(&request_raw)),
        "identity":request.registration.identity,"status":"DISPATCH_REQUIRES_READBACK",
        "operation":format!("{:?}",args.operation)}),
    )?;
    let (mut client, connection) = config.connect(NoTls).await?;
    let connection_task = tokio::spawn(connection);
    let transaction = client
        .build_transaction()
        .isolation_level(tokio_postgres::IsolationLevel::RepeatableRead)
        .start()
        .await?;
    transaction
        .execute(
            "SELECT set_config('app.user_id',$1,true)",
            &[&request.principal.to_string()],
        )
        .await?;
    transaction
        .batch_execute("SET LOCAL lock_timeout='1s';SET LOCAL statement_timeout='60s'")
        .await?;
    let mut held_lease = None;
    let result = match args.operation {
        Operation::RegisterExisting => {
            held_lease = Some(complete::registration_lease(&args.pack_root)?);
            complete::register_existing(
                &transaction,
                &request.registration,
                &request.fragments,
                &args.pack_root,
                65536,
            )
            .await?
        }
        Operation::RegisterZstd => {
            ensure!(
                request.maximum_encoded_input_bytes > 0,
                "finite encoded input budget required"
            );
            held_lease = Some(complete::registration_lease(&args.pack_root)?);
            // Root can open its protected exact blob as stdin before changing
            // uid. Native packs are created by the actual service uid, so the
            // application can read them; no Root600 blob access is granted.
            let decoder = complete::LazyZstd::new(complete::BoundedEncoded::new(
                std::io::stdin(),
                request.maximum_encoded_input_bytes,
            )?);
            complete::register_payload(
                &transaction,
                &request.registration,
                decoder,
                &args.pack_root,
                65536,
            )
            .await?
        }
        Operation::Read => {
            let mut verified = complete::read_verified(
                &transaction,
                &request.registration.identity,
                &request.registration.generation,
                request.registration.include_test,
                request.registration.maximum_logical_bytes,
                &args.pack_root,
                65536,
            )
            .await?;
            let path = args
                .data_output
                .as_ref()
                .context("private verified data output required")?;
            let mut output = private_new(path)?;
            verified.copy_to(&mut output)?;
            output.sync_all()?;
            fs::File::open(path.parent().context("data parent required")?)?.sync_all()?;
            verified.proof
        }
        Operation::Reconcile => {
            complete::reconcile(
                &transaction,
                &request.registration.identity,
                &request.registration.generation,
                request.registration.include_test,
            )
            .await?
        }
    };
    transaction.commit().await?;
    drop(held_lease);
    persist(
        &ack,
        &json!({"schema_version":"mainrag.storage-v2.native-complete-file-receipt.v1",
        "operation_id":request.operation_id,"manifest_sha256":request.manifest_sha256,
        "work_origin_sha256":request.work_origin_sha256,"request_sha256":hex::encode(Sha256::digest(&request_raw)),
        "status":"COMMITTED_REQUIRES_ROOT_ACCOUNT_AND_CALLER_ACCEPTANCE","result":result}),
    )?;
    drop(client);
    connection_task.await??;
    Ok(())
}
#[tokio::main]
async fn main() {
    if run().await.is_err() {
        eprintln!("native complete-file operation stopped; retain original intent and reconcile before retry");
        std::process::exit(1);
    }
}
