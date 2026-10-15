//! Native complete historical files, using the existing durable hit namespace.
//! This module never allocates a generation or changes generation membership.
use crate::db::content_body;
use crate::services::content_store::{BodyCodec, PackBuilder};
use crate::services::shadow_slice::deliver_stored_body_row;
use anyhow::{ensure, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use tokio_postgres::GenericClient;
use uuid::Uuid;

/// Pack reader epochs require the existing read-committed commit fence.
pub async fn begin_complete_file_transaction(
    client: &mut tokio_postgres::Client,
) -> Result<tokio_postgres::Transaction<'_>> {
    client
        .build_transaction()
        .isolation_level(tokio_postgres::IsolationLevel::ReadCommitted)
        .start()
        .await
        .context("begin complete-file transaction")
}

/// `hostaddr` overrides `host` when PostgreSQL selects the actual transport.
pub fn local_complete_file_connection(config: &tokio_postgres::Config) -> bool {
    !config.get_hosts().is_empty()
        && config.get_hosts().iter().all(|host| match host {
            tokio_postgres::config::Host::Unix(_) => true,
            tokio_postgres::config::Host::Tcp(host) => host
                .parse::<std::net::IpAddr>()
                .is_ok_and(|ip| ip.is_loopback()),
        })
        && config
            .get_hostaddrs()
            .iter()
            .all(std::net::IpAddr::is_loopback)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CompleteFileIdentity {
    pub source_id: i64,
    pub file_id: i64,
    pub revision: i64,
    pub sha256: String,
    pub logical_bytes: u64,
    pub path_sha256: String,
}
impl CompleteFileIdentity {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.source_id > 0
                && self.file_id > 0
                && self.revision >= 0
                && self.logical_bytes < i64::MAX as u64,
            "invalid complete file identity"
        );
        for hash in [&self.sha256, &self.path_sha256] {
            let decoded = hex::decode(hash)?;
            ensure!(
                decoded.len() == 32 && hex::encode(decoded) == *hash,
                "canonical complete file digest required"
            );
        }
        Ok(())
    }
    pub fn mapping_id(&self) -> Result<String> {
        self.validate()?;
        Ok(format!(
            "legacy-file-v1:{}:{}:{}:{}:{}",
            self.source_id, self.file_id, self.revision, self.logical_bytes, self.sha256
        ))
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Registration {
    pub identity: CompleteFileIdentity,
    pub generation: String,
    pub expected_legacy_epoch: i64,
    /// The legacy metadata/content hash is a separate CAS field from raw bytes.
    pub legacy_file_hash_sha256: String,
    pub expected_mapping_sha256: String,
    pub maximum_logical_bytes: u64,
    pub maximum_pack_bytes: u64,
    /// Root reserves this worst-case simultaneous allocation in the same account.
    pub maximum_scratch_bytes: u64,
    #[serde(default)]
    pub include_test: bool,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExistingFragment {
    pub occurrence_id: i64,
    pub source_offset: i64,
    pub logical_bytes: i64,
}

pub fn registration_lease(root: &Path) -> Result<std::sync::Arc<std::fs::File>> {
    Ok(crate::services::content_store::build_recovery::writer_lease(root)?)
}

fn advance_range(
    next: u64,
    offset: u64,
    overlap: u64,
    length: u64,
    count: usize,
    total: u64,
) -> Result<u64> {
    ensure!(
        offset == next && overlap == length && (length > 0 || count == 1),
        "complete native byte ranges contain a gap, overlap or mixed empty representation"
    );
    let end = next
        .checked_add(length)
        .context("native byte range overflow")?;
    ensure!(
        end <= total,
        "native component exceeds complete-file scratch admission"
    );
    Ok(end)
}

fn generation_selector(selector: &str) -> Result<()> {
    ensure!(
        selector == "active"
            || selector
                .parse::<i64>()
                .is_ok_and(|n| n > 0 && n.to_string() == selector),
        "active or canonical positive generation selector required"
    );
    Ok(())
}

pub struct BoundedEncoded<R> {
    input: R,
    remaining: u64,
}
impl<R: Read> BoundedEncoded<R> {
    pub fn new(input: R, maximum_encoded_bytes: u64) -> Result<Self> {
        ensure!(
            maximum_encoded_bytes > 0,
            "finite encoded input bound required"
        );
        Ok(Self {
            input,
            remaining: maximum_encoded_bytes,
        })
    }
}
impl<R: Read> Read for BoundedEncoded<R> {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        if self.remaining == 0 {
            let mut extra = [0u8; 1];
            if self.input.read(&mut extra)? != 0 {
                return Err(std::io::Error::other(
                    "encoded input exceeds its original bound",
                ));
            }
            return Ok(0);
        }
        let limit = buffer
            .len()
            .min(usize::try_from(self.remaining).unwrap_or(usize::MAX));
        let count = self.input.read(&mut buffer[..limit])?;
        self.remaining -= count as u64;
        Ok(count)
    }
}
pub struct LazyZstd<R: Read> {
    input: Option<R>,
    decoder: Option<zstd::stream::read::Decoder<'static, std::io::BufReader<R>>>,
}
impl<R: Read> LazyZstd<R> {
    pub fn new(input: R) -> Self {
        Self {
            input: Some(input),
            decoder: None,
        }
    }
}
impl<R: Read> Read for LazyZstd<R> {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        if self.decoder.is_none() {
            self.decoder = Some(zstd::stream::read::Decoder::new(
                self.input
                    .take()
                    .ok_or_else(|| std::io::Error::other("encoded source already consumed"))?,
            )?);
        }
        self.decoder
            .as_mut()
            .ok_or_else(|| std::io::Error::other("decoder unavailable"))?
            .read(buffer)
    }
}

async fn source_read<C: GenericClient + Sync>(
    client: &C,
    identity: &CompleteFileIdentity,
    generation: &str,
    include_test: bool,
) -> Result<Value> {
    identity.validate()?;
    generation_selector(generation)?;
    client
        .execute(
            "SELECT storage_v2_require_test_scope($1,$2)",
            &[&Some(identity.source_id), &include_test],
        )
        .await?;
    let result: Value = client
        .query_one(
            "SELECT storage_v2_resolve_legacy_hit($1,$2,$3)",
            &[&identity.source_id, &generation, &identity.mapping_id()?],
        )
        .await?
        .get(0);
    ensure!(
        result["source_id"].as_i64() == Some(identity.source_id),
        "source-scoped native resolution differs"
    );
    Ok(result)
}

struct DigestWriter<'a> {
    output: &'a mut std::fs::File,
    hash: Sha256,
    bytes: u64,
    limit: u64,
}
impl Write for DigestWriter<'_> {
    fn write(&mut self, value: &[u8]) -> std::io::Result<usize> {
        if self.bytes.saturating_add(value.len() as u64) > self.limit {
            return Err(std::io::Error::other(
                "complete native file exceeded admitted length",
            ));
        }
        self.output.write_all(value)?;
        self.hash.update(value);
        self.bytes += value.len() as u64;
        Ok(value.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.output.flush()
    }
}

/// The protected temporary file is returned only after every body, ordered byte
/// range, complete SHA and length has passed. No unverified bytes escape.
pub struct VerifiedCompleteFile {
    temporary: tempfile::NamedTempFile,
    pub proof: Value,
}
impl VerifiedCompleteFile {
    pub fn copy_to(&mut self, writer: &mut impl Write) -> Result<u64> {
        self.temporary.as_file_mut().seek(SeekFrom::Start(0))?;
        let count = std::io::copy(self.temporary.as_file_mut(), writer)?;
        ensure!(
            Some(count) == self.proof["logical_bytes"].as_u64(),
            "verified complete file staging changed"
        );
        Ok(count)
    }
}

pub async fn read_verified<C: GenericClient + Sync>(
    client: &C,
    identity: &CompleteFileIdentity,
    generation: &str,
    include_test: bool,
    maximum_logical_bytes: u64,
    root: &Path,
    buffer: usize,
) -> Result<VerifiedCompleteFile> {
    ensure!(
        maximum_logical_bytes > 0 && identity.logical_bytes <= maximum_logical_bytes,
        "bounded complete-file scratch admission required"
    );
    let resolved = source_read(client, identity, generation, include_test).await?;
    let targets = resolved["targets"]
        .as_array()
        .context("native targets omitted")?;
    ensure!(
        !targets.is_empty() && targets.len() <= 2048,
        "complete file has no bounded durable native mapping"
    );
    // Register before fetching placement; keep the epoch through all pack I/O.
    content_body::with_reader_epoch(client, async {
        let mut temporary = tempfile::NamedTempFile::new_in(root)?;
        let mut writer = DigestWriter {
            output: temporary.as_file_mut(),
            hash: Sha256::new(),
            bytes: 0,
            limit: identity.logical_bytes,
        };
        let mut next = 0u64;
        let mut cursor = -1i64;
        let mut count = 0usize;
        loop {
            let rows = client
                .query(
                    "SELECT * FROM storage_v2_complete_file_components($1,$2,$3,$4,$5)",
                    &[
                        &identity.source_id,
                        &generation,
                        &identity.mapping_id()?,
                        &cursor,
                        &include_test,
                    ],
                )
                .await?;
            if rows.is_empty() {
                break;
            }
            for row in rows {
                let offset = u64::try_from(row.get::<_, i64>("source_offset"))?;
                let overlap = u64::try_from(row.get::<_, i64>("byte_overlap"))?;
                let length = u64::try_from(row.get::<_, i64>("logical_length"))?;
                let path: String = row.get("source_path");
                let end = advance_range(
                    next,
                    offset,
                    overlap,
                    length,
                    targets.len(),
                    identity.logical_bytes,
                )?;
                ensure!(
                    hex::encode(Sha256::digest(path.as_bytes())) == identity.path_sha256,
                    "complete native ranges or original source path differ"
                );
                let delivered = deliver_stored_body_row(&row, root, buffer, Some(&mut writer))?;
                ensure!(delivered == length, "native file component length differs");
                next = end;
                cursor = i64::try_from(offset)?;
                count += 1;
                ensure!(
                    count <= targets.len(),
                    "native mapping changed during delivery"
                );
            }
        }
        ensure!(
            count == targets.len()
                && next == identity.logical_bytes
                && writer.bytes == identity.logical_bytes
                && hex::encode(writer.hash.finalize()) == identity.sha256,
            "complete native file byte SHA or coverage differs"
        );
        temporary.as_file().sync_all()?;
        Ok(VerifiedCompleteFile {
            temporary,
            proof: json!({
            "schema_version":"mainrag.storage-v2.native-complete-file-read.v1",
            "source_id":identity.source_id,"file_id":identity.file_id,
            "revision":identity.revision,"sha256":identity.sha256,
            "logical_bytes":identity.logical_bytes,"mapping_id":identity.mapping_id()?,
            "path_sha256":identity.path_sha256,"fragments":count,
            "source_authorized":true,"full_raw_integrity_verified":true,
            "reads_legacy_files_or_chunks":false}),
        })
    })
    .await
}

fn pack_allocation_bound(raw: u64) -> Result<(u64, u64)> {
    // PackBuilder uses one zstd level3 frame, no dictionary, explicit flush or
    // per-input-chunk framing. Retain64KiB beyond the native compression bound
    // for streaming/frame/allocator rounding; there are no pack payload headers.
    let bound = u64::try_from(zstd::zstd_safe::compress_bound(usize::try_from(raw)?))?
        .checked_add(65536)
        .context("native pack bound overflow")?;
    // Raw entry staging, compressed candidate, verified raw staging and local
    // filesystem rounding must all fit before the builder creates any files.
    let scratch = raw
        .checked_mul(2)
        .and_then(|n| n.checked_add(bound))
        .and_then(|n| n.checked_add(65536))
        .context("native scratch bound overflow")?;
    Ok((bound, scratch))
}

/// Maintenance-only CAS read through the existing source/write-authorized API.
/// The unscoped scalar helper intentionally remains unavailable to mainrag.
async fn maintenance_mapping_state<C: GenericClient + Sync>(
    client: &C,
    source: i64,
    mapping_id: &str,
) -> Result<String> {
    let ids = vec![mapping_id.to_owned()];
    let observed: Value = client
        .query_one(
            "SELECT storage_v2_legacy_hit_mapping_states($1,$2)",
            &[&source, &ids],
        )
        .await?
        .get(0);
    let mappings = observed["mappings"]
        .as_array()
        .context("mapping states omitted")?;
    ensure!(
        observed["source_id"].as_i64() == Some(source)
            && mappings.len() == 1
            && mappings[0]["old_hit_id"].as_str() == Some(mapping_id),
        "source-authorized mapping state identity differs"
    );
    let digest = mappings[0]["expected_mapping_sha256"]
        .as_str()
        .context("mapping state digest omitted")?;
    let decoded = hex::decode(digest)?;
    ensure!(
        decoded.len() == 32 && hex::encode(decoded) == digest,
        "canonical mapping state digest required"
    );
    Ok(digest.to_owned())
}

async fn registration_source<C: GenericClient + Sync>(
    client: &C,
    request: &Registration,
) -> Result<String> {
    request.identity.validate()?;
    generation_selector(&request.generation)?;
    let h = hex::decode(&request.legacy_file_hash_sha256)?;
    ensure!(
        h.len() == 32 && hex::encode(&h) == request.legacy_file_hash_sha256,
        "canonical original legacy CAS hash required"
    );
    ensure!(
        request.expected_legacy_epoch >= 0
            && request.maximum_logical_bytes > 0
            && request.identity.logical_bytes <= request.maximum_logical_bytes
            && request.maximum_pack_bytes > 0
            && request.maximum_scratch_bytes
                >= request
                    .identity
                    .logical_bytes
                    .checked_mul(2)
                    .and_then(|n| n.checked_add(65536))
                    .context("reader scratch overflow")?,
        "finite source snapshot and pack bounds required"
    );
    client
        .execute(
            "SELECT storage_v2_require_test_scope($1,$2)",
            &[&Some(request.identity.source_id), &request.include_test],
        )
        .await?;
    let locked: Value = client
        .query_one(
            "SELECT storage_v2_lock_legacy_hit_source($1,$2,$3)",
            &[&request.identity.source_id, &request.identity.file_id, &h],
        )
        .await?
        .get(0);
    ensure!(
        locked["file_revision"].as_i64() == Some(request.identity.revision)
            && locked["legacy_epoch"].as_i64() == Some(request.expected_legacy_epoch),
        "original file revision or epoch changed"
    );
    let path: String = client
        .query_one(
            "SELECT path FROM files WHERE id=$1 AND source_id=$2 AND hash=$3",
            &[&request.identity.file_id, &request.identity.source_id, &h],
        )
        .await?
        .get(0);
    ensure!(
        hex::encode(Sha256::digest(path.as_bytes())) == request.identity.path_sha256,
        "original source path changed"
    );
    client
        .execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('mainrag.legacy-hit:'||$1,0))",
            &[&request.identity.mapping_id()?],
        )
        .await?;
    let actual = maintenance_mapping_state(
        client,
        request.identity.source_id,
        &request.identity.mapping_id()?,
    )
    .await?;
    ensure!(
        actual == request.expected_mapping_sha256,
        "complete file mapping CAS changed"
    );
    Ok(path)
}

/// Caller owns the enclosing transaction and pack writer lease through COMMIT.
/// Register first, then verify through the exact public reader. Failure rolls
/// back the mapping and cannot create a generation or advance any cursor.
pub async fn register_existing<C: GenericClient + Sync>(
    client: &C,
    request: &Registration,
    fragments: &[ExistingFragment],
    root: &Path,
    buffer: usize,
) -> Result<Value> {
    registration_source(client, request).await?;
    ensure!(
        !fragments.is_empty() && fragments.len() <= 2048,
        "bounded existing native fragment list required"
    );
    let records = json!([{"old_hit_id":request.identity.mapping_id()?,
        "expected_mapping_sha256":request.expected_mapping_sha256,
        "occurrence_ids":fragments.iter().map(|f|f.occurrence_id).collect::<Vec<_>>(),
        "source_offsets":fragments.iter().map(|f|f.source_offset).collect::<Vec<_>>(),
        "byte_overlaps":fragments.iter().map(|f|f.logical_bytes).collect::<Vec<_>>(),
        "relation_kind":if fragments.len()==1 {"exact"} else {"split"}}]);
    client
        .query_one(
            "SELECT storage_v2_replace_legacy_hit_mappings($1,$2)",
            &[&request.identity.source_id, &records],
        )
        .await?;
    let verified = read_verified(
        client,
        &request.identity,
        &request.generation,
        request.include_test,
        request.maximum_logical_bytes,
        root,
        buffer,
    )
    .await?;
    let mapping_sha256 = maintenance_mapping_state(
        client,
        request.identity.source_id,
        &request.identity.mapping_id()?,
    )
    .await?;
    Ok(
        json!({"schema_version":"mainrag.storage-v2.native-complete-file-registration.v1",
        "identity":request.identity,"legacy_file_hash_sha256":request.legacy_file_hash_sha256,"read_proof":verified.proof,"mapping_sha256":mapping_sha256,
        "previous_mapping_sha256":request.expected_mapping_sha256,
        "new_payload_bytes":0,"new_generations":0,"new_memberships":0}),
    )
}

/// Missing payload enters the canonical pack format with bounded streaming.
/// Identical existing native bodies are verified/reused before consuming input.
pub async fn register_payload<C: GenericClient + Sync, R: Read>(
    client: &C,
    request: &Registration,
    reader: R,
    root: &Path,
    buffer: usize,
) -> Result<Value> {
    let path = registration_source(client, request).await?;
    let digest = hex::decode(&request.identity.sha256)?;
    let length = i64::try_from(request.identity.logical_bytes)?;
    let existing = client.query_opt("SELECT id FROM content_body WHERE digest_algorithm='sha256-v1' AND digest=$1 AND logical_length=$2",
        &[&digest, &length]).await?;
    let mut stored = 0;
    let body: i64;
    if let Some(row) = existing {
        body = row.get(0);
    } else {
        let (pack_bound, scratch_bound) = pack_allocation_bound(request.identity.logical_bytes)?;
        ensure!(
            pack_bound <= request.maximum_pack_bytes
                && scratch_bound <= request.maximum_scratch_bytes,
            "worst-case native pack and simultaneous scratch exceed Root reservation"
        );
        let pack = Uuid::new_v4();
        let nonce = Uuid::new_v4();
        let mut builder = PackBuilder::new(root, pack, nonce, buffer)?;
        let entry = builder.add_reader(
            reader.take(request.identity.logical_bytes + 1),
            BodyCodec::Zstd,
            None,
        )?;
        ensure!(
            entry.body.logical_length == request.identity.logical_bytes
                && hex::encode(entry.body.digest) == request.identity.sha256,
            "missing original bytes do not match the exact full-file identity"
        );
        let sealed = builder.seal()?;
        stored = sealed.manifest.stored_bytes;
        ensure!(
            stored <= request.maximum_pack_bytes,
            "canonical complete-file pack exceeds admission"
        );
        sealed.verify_entry(&entry, None)?;
        content_body::create_pack(client, pack, &format!("{pack}.pack"), nonce).await?;
        body = content_body::put_packed_body(
            client,
            pack,
            i64::try_from(entry.ordinal)?,
            &entry.body.digest,
            length,
            i64::try_from(entry.pack_offset)?,
            i64::try_from(entry.stored_length)?,
            entry.codec.database_name(),
            &entry.entry_digest,
        )
        .await?
        .id;
        content_body::verify_pack(
            client,
            pack,
            &sealed.manifest.sha256,
            i64::try_from(stored)?,
        )
        .await?;
        let published = sealed.publish()?;
        published.reader().verify_integrity(&entry, None, buffer)?;
        content_body::publish_pack(client, pack).await?;
        // A published pack is retained on a failed or unknown COMMIT; only the
        // existing explicit orphan/GC contracts may reclaim it later.
    }
    let proof = json!({"chunk_sha256":request.identity.sha256,
        "file_sha256":request.legacy_file_hash_sha256,"logical_bytes":request.identity.logical_bytes,
        "source_path":path,"start_line":0,"end_line":0});
    client
        .query_one(
            "SELECT storage_v2_preserve_legacy_hit($1,$2,$3,$4,$5)",
            &[
                &request.identity.source_id,
                &request.identity.mapping_id()?,
                &body,
                &proof,
                &request.expected_mapping_sha256,
            ],
        )
        .await?;
    let verified = read_verified(
        client,
        &request.identity,
        &request.generation,
        request.include_test,
        request.maximum_logical_bytes,
        root,
        buffer,
    )
    .await?;
    let mapping_sha256 = maintenance_mapping_state(
        client,
        request.identity.source_id,
        &request.identity.mapping_id()?,
    )
    .await?;
    Ok(
        json!({"schema_version":"mainrag.storage-v2.native-complete-file-registration.v1",
        "identity":request.identity,"legacy_file_hash_sha256":request.legacy_file_hash_sha256,"read_proof":verified.proof,"mapping_sha256":mapping_sha256,
        "previous_mapping_sha256":request.expected_mapping_sha256,
        "new_payload_bytes":stored,"native_body_id":body,
        "new_generations":0,"new_memberships":0}),
    )
}

/// No pack reads, source reads, write dispatch or retry. This only distinguishes
/// visible committed mapping metadata from absence/drift after a lost ACK.
/// Byte acceptance still requires the supported verified read above.
pub async fn reconcile<C: GenericClient + Sync>(
    client: &C,
    identity: &CompleteFileIdentity,
    generation: &str,
    include_test: bool,
) -> Result<Value> {
    let resolved = source_read(client, identity, generation, include_test).await?;
    let count = resolved["targets"]
        .as_array()
        .context("targets omitted")?
        .len();
    Ok(
        json!({"schema_version":"mainrag.storage-v2.native-complete-file-reconciliation.v1",
        "mapping_id":identity.mapping_id()?,"committed_visible_targets":count,
        "resolution":resolved,"new_dispatches":0,"automatic_retry_allowed":false,
        "full_raw_integrity_verified":false,"byte_acceptance_pending":true}),
    )
}

#[cfg(test)]
#[path = "legacy_complete_file_tests.rs"]
mod tests;
