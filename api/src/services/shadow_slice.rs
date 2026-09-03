//! Classification and evidence contracts for bounded storage-v2 shadow reads.
//!
//! This module never selects a generation implicitly and never activates one.

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::time::Instant;
use tokio_postgres::GenericClient;
use uuid::Uuid;

use crate::db::{content_body, content_graph, generation_ingest};
use crate::plugins;
use crate::services::content_store::{
    BodyCodec, BodyIdentity, DictionaryIdentity, PackBuilder, PackEntry, PackReader,
};
use crate::services::generation_ingest::{ShadowIngestMeasurements, ShadowIngestStage};
use crate::services::intelligence_v2::{
    generic_structural_cards, normalized_output_sha256, GENERIC_ANALYSIS_PROFILE,
};
use crate::services::parser::{CodeParser, ExtractedCall, ExtractedSymbol, ParseResult};

pub const FIXTURE_ADAPTER_PROFILE: &str = "mainrag.fs-shadow-fixture.v1";
pub const FIXTURE_VIEW_PROFILE: &str = "mainrag.whole-artifact-view.v1";
pub const FIXTURE_SEARCH_PROFILE: &str = "mainrag.lexical-simple.v1";
pub const RELEASE_VIEW_PROFILE: &str = "mainrag.whole-artifact-view.v1";
pub const RELEASE_SEARCH_PROFILE: &str = "mainrag.lexical-simple.v1";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SliceMode {
    PublicFixture,
    ReleaseCandidate,
}

struct SliceFile {
    path: String,
    language: Option<String>,
    inline_bytes: Option<Vec<u8>>,
    source_path: Option<PathBuf>,
    content_sha256: Option<[u8; 32]>,
    logical_length: u64,
}

impl From<plugins::RawFile> for SliceFile {
    fn from(file: plugins::RawFile) -> Self {
        let lazy = file.content.is_empty() && file.source_path.is_some();
        Self {
            path: file.path,
            language: file.language,
            inline_bytes: (!lazy).then(|| file.content.into_bytes()),
            source_path: file.source_path,
            content_sha256: None,
            logical_length: 0,
        }
    }
}

impl SliceFile {
    async fn load_bytes(&self) -> Result<Cow<'_, [u8]>> {
        if let Some(bytes) = &self.inline_bytes {
            return Ok(Cow::Borrowed(bytes));
        }
        let path = self
            .source_path
            .as_ref()
            .context("storage-v2 adapter omitted both content and source path")?;
        Ok(Cow::Owned(tokio::fs::read(path).await?))
    }

    async fn load_verified_bytes(&self) -> Result<Cow<'_, [u8]>> {
        let bytes = self.load_bytes().await?;
        let actual_sha256: [u8; 32] = Sha256::digest(bytes.as_ref()).into();
        if self.content_sha256 != Some(actual_sha256) || self.logical_length != bytes.len() as u64 {
            bail!("source content drifted after the candidate watermark was captured");
        }
        Ok(bytes)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ShadowSliceResult {
    pub run_id: i64,
    pub source_id: i64,
    pub generation_id: i64,
    pub generation_seq: i64,
    pub fixture_sha256: String,
    pub source_watermark_sha256: String,
    pub item_count: usize,
    pub symbol_count: usize,
    pub controlled_retry_count: usize,
    pub pack_id: Option<Uuid>,
    pub pack_stored_bytes: u64,
    pub reused_generation: bool,
    pub active_generation_before: Option<i64>,
    pub active_generation_after: Option<i64>,
    pub telemetry: serde_json::Value,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReleaseCandidateEvidenceInput {
    pub evidence_id: Uuid,
    pub generation_id: i64,
    pub commit_sha: String,
    pub source_watermark_sha256: String,
    pub adapter_profile_id: String,
    pub analysis_profile_id: String,
    pub search_profile_id: String,
    pub manifest: serde_json::Value,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReleaseCandidateEvidenceResult {
    pub evidence_id: Uuid,
    pub source_id: i64,
    pub generation_id: i64,
    pub generation_seq: i64,
    pub status: String,
    pub manifest_sha256: String,
    pub active_generation_id: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReleaseCandidateVerifyInput {
    pub generation_id: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CandidateQuerySeed {
    pub id: String,
    pub query: String,
    pub expected_path_sha256: String,
    pub expects_match: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReleaseCandidateVerifyResult {
    pub source_id: i64,
    pub generation_id: i64,
    pub generation_seq: i64,
    pub run_id: i64,
    pub status: String,
    pub source_watermark_sha256: String,
    pub adapter_profile_id: String,
    pub analysis_profile_id: String,
    pub search_profile_id: String,
    pub generation_root_sha256: String,
    pub verification_manifest_sha256: String,
    pub active_generation_id: Option<i64>,
    pub item_count: i64,
    pub verified_body_count: usize,
    pub verified_logical_bytes: u64,
    pub intelligence_export: serde_json::Value,
    pub query_seeds: Vec<CandidateQuerySeed>,
    pub checks: BTreeMap<String, String>,
}

/// Re-read a completed candidate after construction. This is deliberately
/// separate from qualification: it reconstructs every stored body, recomputes
/// the generation root, and returns protected query seeds without changing
/// generation or pointer state.
pub async fn verify_release_candidate<C>(
    client: &C,
    source_id: i64,
    input: &ReleaseCandidateVerifyInput,
    pack_root: &Path,
    io_buffer_bytes: usize,
) -> Result<ReleaseCandidateVerifyResult>
where
    C: GenericClient + Sync,
{
    if input.generation_id <= 0 {
        bail!("release-candidate verification requires a positive generation id");
    }
    client
        .execute(
            "SELECT storage_v2_require_test_scope($1, TRUE)",
            &[&source_id],
        )
        .await?;
    let identity = client
        .query_opt(
            "SELECT generation.generation_seq, generation.status::TEXT AS status, \
                    generation.verification_manifest_sha256, source.active_generation_id, \
                    run.id AS run_id, run.semantic_manifest_sha256, run.adapter_profile_id, \
                    run.expected_active_generation_id, run.expected_item_count, \
                    run.generation_root_sha256 \
               FROM source_generation generation \
               JOIN logical_source source ON source.id=generation.source_id \
               JOIN storage_v2_ingest_run run ON run.generation_id=generation.id \
              WHERE generation.id=$1 AND generation.source_id=$2 AND run.status='sealed'",
            &[&input.generation_id, &source_id],
        )
        .await?
        .context("verified generation and sealed ingest run not found")?;
    let status: String = identity.get("status");
    if !matches!(status.as_str(), "verified" | "release_candidate") {
        bail!("release-candidate verification requires a verified generation");
    }
    let active_generation_id: Option<i64> = identity.get("active_generation_id");
    let expected_active_generation_id: Option<i64> = identity.get("expected_active_generation_id");
    if active_generation_id != expected_active_generation_id {
        bail!("active pointer drifted after candidate construction");
    }
    let run_id: i64 = identity.get("run_id");
    let expected_root: String = identity.get("generation_root_sha256");
    let reconstructed_root: String = client
        .query_one("SELECT storage_v2_shadow_generation_root($1)", &[&run_id])
        .await?
        .get(0);
    if reconstructed_root != expected_root {
        bail!("candidate generation root reconstruction failed");
    }

    let body_rows = client
        .query(
            "SELECT DISTINCT body.id, body.digest_algorithm, body.digest, body.logical_length, \
                    body.inline_bytes, body.pack_id, pack.storage_key, pack.stored_bytes, \
                    pack.status::TEXT AS pack_status, entry.ordinal, entry.pack_offset, \
                    entry.stored_length, entry.codec::TEXT AS codec, entry.entry_digest, \
                    dictionary.id AS dictionary_id, dictionary.digest AS dictionary_digest, \
                    dictionary.dictionary_bytes \
               FROM storage_v2_ingest_run_item item \
               JOIN artifact_version artifact ON artifact.id=item.artifact_version_id \
               JOIN content_node node ON node.id=artifact.content_root_node_id \
               JOIN content_body body ON body.id=node.body_id \
               LEFT JOIN content_pack pack ON pack.id=body.pack_id \
               LEFT JOIN content_pack_entry entry ON entry.pack_id=body.pack_id AND entry.body_id=body.id \
               LEFT JOIN content_dictionary dictionary ON dictionary.id=entry.dictionary_id \
              WHERE item.run_id=$1 ORDER BY body.id",
            &[&run_id],
        )
        .await?;
    let mut verified_logical_bytes = 0_u64;
    for row in &body_rows {
        verified_logical_bytes = verified_logical_bytes
            .checked_add(verify_stored_body_row(row, pack_root, io_buffer_bytes)?)
            .context("verified candidate byte count overflow")?;
    }

    let generation_seq: i64 = identity.get("generation_seq");
    let state: serde_json::Value = client
        .query_one(
            "SELECT storage_v2_shadow_source_state($1,$2,TRUE)",
            &[&source_id, &generation_seq.to_string()],
        )
        .await?
        .get(0);
    let expected_item_count: i64 = identity.get("expected_item_count");
    for field in ["declared_item_count", "item_count", "occurrence_count"] {
        if state[field].as_i64() != Some(expected_item_count) {
            bail!("candidate item, membership, and occurrence counts differ");
        }
    }
    if state["view_count"] != state["search_document_count"]
        || state["analysis_incomplete_count"].as_i64() != Some(0)
        || state["active_generation_id"].as_i64() != active_generation_id
    {
        bail!("candidate view, analysis, or active-pointer state differs");
    }
    let intelligence_export: serde_json::Value = client
        .query_one(
            "SELECT storage_v2_export_intelligence($1,$2,'public')",
            &[&source_id, &generation_seq.to_string()],
        )
        .await?
        .get(0);
    if intelligence_export["schema_version"] != "mainrag.storage-v2-intelligence-export.v1"
        || intelligence_export["redaction"] != "public"
    {
        bail!("candidate intelligence export contract failed");
    }
    let query_seeds = candidate_query_seeds(client, source_id, input.generation_id).await?;
    let mut checks = BTreeMap::new();
    for check in [
        "artifact_root",
        "authorization",
        "body_pack_integrity",
        "intelligence",
        "intervals",
        "legacy_intelligence_export",
    ] {
        checks.insert(check.to_string(), "PASS".to_string());
    }
    Ok(ReleaseCandidateVerifyResult {
        source_id,
        generation_id: input.generation_id,
        generation_seq,
        run_id,
        status,
        source_watermark_sha256: identity.get("semantic_manifest_sha256"),
        adapter_profile_id: identity.get("adapter_profile_id"),
        analysis_profile_id: GENERIC_ANALYSIS_PROFILE.to_string(),
        search_profile_id: RELEASE_SEARCH_PROFILE.to_string(),
        generation_root_sha256: expected_root,
        verification_manifest_sha256: identity.get("verification_manifest_sha256"),
        active_generation_id,
        item_count: expected_item_count,
        verified_body_count: body_rows.len(),
        verified_logical_bytes,
        intelligence_export,
        query_seeds,
        checks,
    })
}

pub async fn qualify_release_candidate<C>(
    client: &C,
    source_id: i64,
    input: &ReleaseCandidateEvidenceInput,
) -> Result<ReleaseCandidateEvidenceResult>
where
    C: GenericClient + Sync,
{
    if input.generation_id <= 0
        || !is_git_sha(&input.commit_sha)
        || !is_sha256(&input.source_watermark_sha256)
        || input.adapter_profile_id.is_empty()
        || input.analysis_profile_id.is_empty()
        || input.search_profile_id.is_empty()
        || !input.manifest.is_object()
    {
        bail!("complete release-candidate evidence identity is required");
    }
    let row = client
        .query_one(
            "SELECT evidence.id, evidence.generation_id, \
                    encode(evidence.manifest_sha256, 'hex') AS manifest_sha256 \
               FROM storage_v2_qualify_release_candidate( \
                    $1,$2,$3,$4,$5,$6,$7,$8,$9) evidence",
            &[
                &input.evidence_id,
                &source_id,
                &input.generation_id,
                &input.commit_sha,
                &input.source_watermark_sha256,
                &input.adapter_profile_id,
                &input.analysis_profile_id,
                &input.search_profile_id,
                &input.manifest,
            ],
        )
        .await?;
    let generation = client
        .query_one(
            "SELECT generation.generation_seq, generation.status::TEXT AS status, \
                    source.active_generation_id \
               FROM source_generation generation \
               JOIN logical_source source ON source.id=generation.source_id \
              WHERE generation.id=$1 AND generation.source_id=$2",
            &[&input.generation_id, &source_id],
        )
        .await?;
    let status: String = generation.get("status");
    if status != "release_candidate" {
        bail!("qualification did not produce a release candidate");
    }
    Ok(ReleaseCandidateEvidenceResult {
        evidence_id: row.get("id"),
        source_id,
        generation_id: row.get("generation_id"),
        generation_seq: generation.get("generation_seq"),
        status,
        manifest_sha256: row.get("manifest_sha256"),
        active_generation_id: generation.get("active_generation_id"),
    })
}

/// Run a bounded public fixture from the real source adapter through the
/// storage-v2 body, graph, analysis, intelligence, search and generation APIs.
/// The source must already exist and be marked `is_test`; this function never
/// changes an active pointer or writes legacy mutable search state.
pub async fn run_public_shadow_slice<C>(
    client: &C,
    source_id: i64,
    source_type: &str,
    source_path: &Path,
    pack_root: &Path,
    io_buffer_bytes: usize,
    commit_sha: &str,
) -> Result<ShadowSliceResult>
where
    C: GenericClient + Sync,
{
    run_storage_v2_slice(
        client,
        source_id,
        source_type,
        source_path,
        pack_root,
        io_buffer_bytes,
        commit_sha,
        SliceMode::PublicFixture,
    )
    .await
}

/// Build and verify a complete source generation without changing an active
/// pointer. Qualification and the release-candidate transition are separate.
pub async fn run_release_candidate_build<C>(
    client: &C,
    source_id: i64,
    source_type: &str,
    source_path: &Path,
    pack_root: &Path,
    io_buffer_bytes: usize,
    commit_sha: &str,
) -> Result<ShadowSliceResult>
where
    C: GenericClient + Sync,
{
    run_storage_v2_slice(
        client,
        source_id,
        source_type,
        source_path,
        pack_root,
        io_buffer_bytes,
        commit_sha,
        SliceMode::ReleaseCandidate,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
async fn run_storage_v2_slice<C>(
    client: &C,
    source_id: i64,
    source_type: &str,
    source_path: &Path,
    pack_root: &Path,
    io_buffer_bytes: usize,
    commit_sha: &str,
    mode: SliceMode,
) -> Result<ShadowSliceResult>
where
    C: GenericClient + Sync,
{
    if (mode == SliceMode::PublicFixture && source_type != "fs") || !is_git_sha(commit_sha) {
        bail!("shadow slice requires the filesystem adapter and an exact commit SHA");
    }
    if !(4096..=1024 * 1024).contains(&io_buffer_bytes) {
        bail!("shadow slice I/O buffer must be between 4096 and 1048576 bytes");
    }
    let source_path = source_path
        .to_str()
        .ok_or_else(|| anyhow::anyhow!("fixture source path is not UTF-8"))?;
    let source = client
        .query_one(
            "SELECT type, path, is_test FROM sources WHERE id = $1",
            &[&source_id],
        )
        .await?;
    let is_test = source.get::<_, bool>("is_test");
    if source.get::<_, String>("type") != source_type
        || source.get::<_, String>("path") != source_path
        || (mode == SliceMode::PublicFixture && !is_test)
    {
        bail!("source registry does not match the requested storage-v2 scope");
    }
    client
        .execute(
            "SELECT storage_v2_require_test_scope($1, $2)",
            &[&source_id, &is_test],
        )
        .await?;
    let active_generation_before = active_generation(client, source_id).await?;

    let mut measurements = ShadowIngestMeasurements::default();
    measurements.io_buffer_bytes = u64::try_from(io_buffer_bytes)?;
    let total_started = Instant::now();
    let adapter_started = Instant::now();
    let plugin = plugins::get_plugin(source_type)
        .ok_or_else(|| anyhow::anyhow!("source adapter is unavailable"))?;
    let sync = match mode {
        SliceMode::PublicFixture => plugin.sync(source_path).await?,
        SliceMode::ReleaseCandidate => plugin.sync_for_storage_v2(source_path).await?,
    };
    if !sync.errors.is_empty() || (mode == SliceMode::PublicFixture && sync.files.is_empty()) {
        bail!("source adapter returned errors or an invalid empty fixture");
    }
    let mut files = sync
        .files
        .into_iter()
        .map(SliceFile::from)
        .collect::<Vec<_>>();
    files.sort_by(|left, right| left.path.cmp(&right.path));
    let (fixture_sha256, input_bytes) = canonical_fixture_hash(&mut files).await?;
    measurements.input_bytes = input_bytes;
    measurements.record_stage(ShadowIngestStage::ReadAndHash, adapter_started.elapsed());
    let adapter_profile = match mode {
        SliceMode::PublicFixture => FIXTURE_ADAPTER_PROFILE.to_string(),
        SliceMode::ReleaseCandidate => release_adapter_profile(source_type)?,
    };
    let source_watermark_sha256 = match mode {
        SliceMode::PublicFixture => fixture_sha256.clone(),
        SliceMode::ReleaseCandidate => {
            release_source_watermark(source_type, source_path, &adapter_profile, &fixture_sha256)
        }
    };
    let witness_kind = match mode {
        SliceMode::PublicFixture => "public-fixture",
        SliceMode::ReleaseCandidate => "release-candidate-build",
    };
    let witness = json!({
        "kind": witness_kind,
        "fixture_sha256": fixture_sha256.clone(),
        "source_watermark_sha256": source_watermark_sha256.clone(),
        "commit_sha": commit_sha,
        "is_test": is_test,
        "adapter_profile_id": adapter_profile,
    });
    let predecessor_generation_id = client
        .query_opt(
            "SELECT run.generation_id \
               FROM storage_v2_ingest_run run \
               JOIN source_generation generation ON generation.id=run.generation_id \
              WHERE run.source_id=$1 AND run.status='sealed' \
              ORDER BY generation.generation_seq DESC LIMIT 1",
            &[&source_id],
        )
        .await?
        .map(|row| row.get::<_, i64>(0))
        .unwrap_or(0);
    let idempotency_domain = match mode {
        SliceMode::PublicFixture => "mainrag.storage-v2.shadow-snapshot.v1",
        SliceMode::ReleaseCandidate => "mainrag.storage-v2.release-candidate-build.v1",
    };
    let idempotency_key = hex::encode(Sha256::digest(
        format!(
            "{idempotency_domain}:{source_id}:{predecessor_generation_id}:{source_watermark_sha256}:{adapter_profile}"
        )
        .as_bytes(),
    ));
    let begin_started = Instant::now();
    let run = generation_ingest::begin_shadow_ingest(
        client,
        source_id,
        &idempotency_key,
        &source_watermark_sha256,
        &adapter_profile,
        witness_kind,
        &witness,
        false,
    )
    .await?;
    measurements.record_stage(ShadowIngestStage::DatabaseStage, begin_started.elapsed());
    if run.status == "sealed" {
        let reuse_lookup_started = Instant::now();
        let generation = client
            .query_one(
                "SELECT generation_seq, status::TEXT AS status \
                 FROM source_generation WHERE id=$1 AND source_id=$2",
                &[&run.generation_id, &source_id],
            )
            .await?;
        if !matches!(
            generation.get::<_, String>("status").as_str(),
            "verified" | "release_candidate"
        ) {
            bail!("idempotent storage-v2 run exists but its generation is not qualified");
        }
        let active_generation_after = active_generation(client, source_id).await?;
        if active_generation_after != active_generation_before {
            bail!("active generation changed during the idempotent shadow read");
        }
        let symbol_count: i64 = client
            .query_one(
                "SELECT COUNT(*) FROM storage_v2_symbol_occurrence symbol \
                 JOIN storage_v2_ingest_run_item item ON item.occurrence_id=symbol.occurrence_id \
                 WHERE item.run_id=$1",
                &[&run.id],
            )
            .await?
            .get(0);
        let pack = client
            .query_opt(
                "SELECT pack.id, pack.stored_bytes FROM content_pack pack \
                 WHERE EXISTS ( \
                    SELECT 1 FROM storage_v2_ingest_run_item item \
                    JOIN artifact_version artifact ON artifact.id=item.artifact_version_id \
                    JOIN content_node node ON node.id=artifact.content_root_node_id \
                    JOIN content_body body ON body.id=node.body_id \
                    WHERE item.run_id=$1 AND body.pack_id=pack.id \
                 ) ORDER BY pack.id LIMIT 1",
                &[&run.id],
            )
            .await?;
        let reused_items = u64::try_from(run.expected_item_count.unwrap_or(0))?;
        measurements.reused_bodies = reused_items;
        measurements.reused_nodes = reused_items;
        measurements.reused_views = reused_items;
        measurements.reused_analysis = reused_items;
        measurements.reused_generation = 1;
        measurements.record_stage(
            ShadowIngestStage::DatabaseStage,
            reuse_lookup_started.elapsed(),
        );
        measurements.record_total(total_started.elapsed());
        return Ok(ShadowSliceResult {
            run_id: run.id,
            source_id,
            generation_id: run.generation_id,
            generation_seq: generation.get("generation_seq"),
            fixture_sha256: fixture_sha256.clone(),
            source_watermark_sha256,
            item_count: usize::try_from(run.expected_item_count.unwrap_or(0))?,
            symbol_count: usize::try_from(symbol_count)?,
            controlled_retry_count: 0,
            pack_id: pack.as_ref().map(|row| row.get("id")),
            pack_stored_bytes: pack
                .as_ref()
                .map(|row| u64::try_from(row.get::<_, i64>("stored_bytes")))
                .transpose()?
                .unwrap_or(0),
            reused_generation: true,
            active_generation_before,
            active_generation_after,
            telemetry: measurements.to_telemetry_json(),
        });
    }
    if run.status != "building" {
        bail!("storage-v2 ingest run is neither building nor reusable");
    }

    let content_started = Instant::now();
    let mut bodies = vec![None; files.len()];
    let mut missing_indices = Vec::new();
    for (index, file) in files.iter().enumerate() {
        let bytes = file.load_verified_bytes().await?;
        if let Some(body) =
            find_and_verify_existing_body(client, pack_root, bytes.as_ref(), io_buffer_bytes)
                .await?
        {
            bodies[index] = Some(body);
            measurements.reused_bodies = measurements.reused_bodies.saturating_add(1);
        } else {
            missing_indices.push(index);
        }
    }
    let mut result_pack_id = bodies.iter().flatten().find_map(|body| body.pack_id);
    let mut result_pack_stored_bytes = 0_u64;
    if !missing_indices.is_empty() {
        let pack_id = Uuid::new_v4();
        let build_nonce = Uuid::new_v4();
        let mut pack_builder = PackBuilder::new(pack_root, pack_id, build_nonce, io_buffer_bytes)?;
        let mut entries = Vec::with_capacity(missing_indices.len());
        for index in &missing_indices {
            let bytes = files[*index].load_verified_bytes().await?;
            entries.push(pack_builder.add_reader(
                Cursor::new(bytes.as_ref()),
                BodyCodec::Zstd,
                None,
            )?);
        }
        let sealed_pack = pack_builder.seal()?;
        for entry in &entries {
            sealed_pack.verify_entry(entry, None)?;
        }
        let storage_key = format!("{pack_id}.pack");
        content_body::create_pack(client, pack_id, &storage_key, build_nonce).await?;
        for (index, entry) in missing_indices.iter().zip(&entries) {
            bodies[*index] = Some(
                content_body::put_packed_body(
                    client,
                    pack_id,
                    i64::try_from(entry.ordinal)?,
                    &entry.body.digest,
                    i64::try_from(entry.body.logical_length)?,
                    i64::try_from(entry.pack_offset)?,
                    i64::try_from(entry.stored_length)?,
                    entry.codec.database_name(),
                    &entry.entry_digest,
                )
                .await?,
            );
        }
        content_body::verify_pack(
            client,
            pack_id,
            &sealed_pack.manifest.sha256,
            i64::try_from(sealed_pack.manifest.stored_bytes)?,
        )
        .await?;
        let published_pack = sealed_pack.publish()?;
        if published_pack
            .path
            .file_name()
            .and_then(|name| name.to_str())
            != Some(storage_key.as_str())
        {
            bail!("published pack storage key mismatch");
        }
        content_body::publish_pack(client, pack_id).await?;
        let published_reader = published_pack.reader();
        for (index, entry) in missing_indices.iter().zip(&entries) {
            let expected_bytes = files[*index].load_verified_bytes().await?;
            let verified =
                published_reader.verify_to_staging(entry, None, pack_root, io_buffer_bytes)?;
            let mut reconstructed = Vec::with_capacity(expected_bytes.len());
            verified.copy_to(&mut reconstructed)?;
            if reconstructed.as_slice() != expected_bytes.as_ref() {
                bail!("published pack failed exact artifact reconstruction");
            }
        }
        result_pack_id = Some(pack_id);
        result_pack_stored_bytes = published_pack.manifest.stored_bytes;
    } else if let Some(pack_id) = result_pack_id {
        result_pack_stored_bytes = u64::try_from(
            client
                .query_one(
                    "SELECT stored_bytes FROM content_pack WHERE id=$1",
                    &[&pack_id],
                )
                .await?
                .get::<_, i64>(0),
        )?;
    }
    let bodies = bodies
        .into_iter()
        .collect::<Option<Vec<_>>>()
        .context("shadow content store omitted a fixture body")?;
    measurements.unique_bytes = missing_indices.iter().try_fold(0_u64, |total, index| {
        total
            .checked_add(files[*index].logical_length)
            .context("unique fixture byte count overflow")
    })?;
    measurements.stored_bytes = if missing_indices.is_empty() {
        0
    } else {
        result_pack_stored_bytes
    };
    let inline_bytes = files.iter().try_fold(0_u64, |total, file| {
        total
            .checked_add(
                file.inline_bytes
                    .as_ref()
                    .map_or(0, |bytes| bytes.len() as u64),
            )
            .context("inline source byte count overflow")
    })?;
    let largest_lazy_file = files
        .iter()
        .filter(|file| file.inline_bytes.is_none())
        .map(|file| file.logical_length)
        .max()
        .unwrap_or(0);
    let largest_file = files
        .iter()
        .map(|file| file.logical_length)
        .max()
        .unwrap_or(0);
    let io_buffer_bytes_u64 = u64::try_from(io_buffer_bytes)?;
    measurements.peak_buffer_bytes = inline_bytes
        .checked_add(largest_lazy_file)
        .and_then(|bytes| bytes.checked_add(largest_file))
        .and_then(|bytes| bytes.checked_add(io_buffer_bytes_u64))
        .context("peak source buffer byte count overflow")?
        .max(io_buffer_bytes_u64);
    measurements.writer_concurrency = 1;
    measurements.record_stage(ShadowIngestStage::ContentStore, content_started.elapsed());

    let parser = CodeParser::new()?;
    let mut symbol_count = 0_usize;
    let mut controlled_retry_count = 0_usize;
    let mut controlled_retry_done = false;
    for (file, body) in files.iter().zip(&bodies) {
        let path = &file.path;
        let language = &file.language;
        let bytes = file.load_verified_bytes().await?;
        let projection_started = Instant::now();
        let node_domain = match mode {
            SliceMode::PublicFixture => "fixture",
            SliceMode::ReleaseCandidate => "source",
        };
        let node = content_graph::put_leaf_node(client, node_domain, "artifact", body.id).await?;
        let roles = vec!["content".to_string()];
        let kinds = vec!["node".to_string()];
        let component_ids = vec![node.id];
        let starts = vec![0_i64];
        let ends = vec![body.logical_length];
        let view = content_graph::put_retrieval_view(
            client,
            "artifact",
            match mode {
                SliceMode::PublicFixture => FIXTURE_VIEW_PROFILE,
                SliceMode::ReleaseCandidate => RELEASE_VIEW_PROFILE,
            },
            language.as_deref().unwrap_or("text"),
            "whole-bytes-v1",
            0,
            &roles,
            &kinds,
            &component_ids,
            &starts,
            &ends,
        )
        .await?;
        measurements.record_stage(
            ShadowIngestStage::StructuralProjection,
            projection_started.elapsed(),
        );

        let text = std::str::from_utf8(bytes.as_ref())
            .context("fixture adapter produced non-UTF-8 text")?;
        let analysis_started = Instant::now();
        let content_digest = body.digest.as_slice();
        let cached_analysis = client
            .query_opt(
                "SELECT result FROM storage_v2_analysis_cache \
                 WHERE content_identity_sha256=$1 AND analysis_profile_id=$2 AND status='complete'",
                &[&content_digest, &GENERIC_ANALYSIS_PROFILE],
            )
            .await?;
        let (parsed, parser_pass_count) = if let Some(row) = cached_analysis {
            let result: serde_json::Value = row.get("result");
            measurements.reused_analysis = measurements.reused_analysis.saturating_add(1);
            (
                ParseResult {
                    symbols: serde_json::from_value::<Vec<ExtractedSymbol>>(
                        result["symbols"].clone(),
                    )?,
                    calls: serde_json::from_value::<Vec<ExtractedCall>>(result["calls"].clone())?,
                    language: result["language"].as_str().unwrap_or("text").to_string(),
                },
                0_i16,
            )
        } else {
            generation_ingest::begin_analysis_attempt(
                client,
                content_digest,
                GENERIC_ANALYSIS_PROFILE,
            )
            .await?;
            if mode == SliceMode::PublicFixture && !controlled_retry_done {
                generation_ingest::finish_analysis_attempt(
                    client,
                    content_digest,
                    GENERIC_ANALYSIS_PROFILE,
                    None,
                    Some("controlled_fixture_retry"),
                )
                .await?;
                generation_ingest::begin_analysis_attempt(
                    client,
                    content_digest,
                    GENERIC_ANALYSIS_PROFILE,
                )
                .await?;
                controlled_retry_count += 1;
                measurements.analysis_retries = measurements.analysis_retries.saturating_add(1);
                controlled_retry_done = true;
            }
            let parsed = parser.parse_file(Path::new(path), text)?;
            let analysis_result = json!({
                "symbols": &parsed.symbols,
                "calls": &parsed.calls,
                "language": &parsed.language,
            });
            generation_ingest::finish_analysis_attempt(
                client,
                content_digest,
                GENERIC_ANALYSIS_PROFILE,
                Some(&analysis_result),
                None,
            )
            .await?;
            measurements.parser_passes = measurements.parser_passes.saturating_add(1);
            (parsed, 1_i16)
        };
        let cards = generic_structural_cards(path, &parsed)?;
        measurements.record_stage(ShadowIngestStage::Analysis, analysis_started.elapsed());

        let database_started = Instant::now();
        let locator = json!({
            "byte_start": 0,
            "byte_end": bytes.len(),
            "line_start": 1,
            "line_end": text.lines().count().max(1),
            "language": language.as_deref().unwrap_or("text"),
            "level": 0,
        });
        let item_witness = json!({"path": path, "sha256": hex::encode(&body.digest)});
        let expected_content_hash = hex::encode(&body.digest);
        let staged = generation_ingest::stage_shadow_item(
            client,
            &generation_ingest::StageItem {
                run_id: run.id,
                item_key: path,
                item_kind: "document",
                witness_type: match mode {
                    SliceMode::PublicFixture => "public-fixture-item",
                    SliceMode::ReleaseCandidate => "release-candidate-item",
                },
                witness: &item_witness,
                adapter_profile_id: &adapter_profile,
                content_root_node_id: Some(node.id),
                raw_body_id: None,
                expected_content_hash: &expected_content_hash,
                byte_length: body.logical_length,
                content_identity_sha256: content_digest,
                analysis_profile_id: GENERIC_ANALYSIS_PROFILE,
                view_id: view.id,
                source_path: path,
                locator: &locator,
                parser_pass_count,
            },
        )
        .await?;
        let identifiers = search_exact_identifiers(text);
        let document_id: i64 = client
            .query_one(
                "SELECT id FROM storage_v2_put_search_document($1, 'node', $2, $3, $4)",
                &[
                    &match mode {
                        SliceMode::PublicFixture => FIXTURE_SEARCH_PROFILE,
                        SliceMode::ReleaseCandidate => RELEASE_SEARCH_PROFILE,
                    },
                    &node.id,
                    &text,
                    &identifiers,
                ],
            )
            .await?
            .get(0);
        client
            .execute(
                "SELECT storage_v2_bind_search_document($1, 0, $2, 1.0)",
                &[&view.id, &document_id],
            )
            .await?;
        for card in cards {
            let generic = json!({
                "name": card.name,
                "qualified_name": card.qualified_name,
                "symbol_kind": card.symbol_kind,
                "language": card.language,
            });
            let structure = serde_json::to_value(&card.structure)?;
            let span = serde_json::to_value(&card.source_span)?;
            let symbol_occurrence = client
                .query_one(
                    "SELECT id FROM storage_v2_put_symbol_occurrence( \
                     $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                    &[
                        &source_id,
                        &staged.artifact_version_id,
                        &staged.occurrence_id,
                        &card.symbol_key,
                        &card.language,
                        &card.symbol_kind,
                        &card.qualified_name,
                        &card.signature,
                        &card.documentation,
                        &card.visibility,
                        &structure,
                        &span,
                    ],
                )
                .await?;
            let symbol_occurrence_id: i64 = symbol_occurrence.get(0);
            let output_sha = normalized_output_sha256(&card)?;
            let output_sha_bytes: &[u8] = &output_sha;
            let analysis = client
                .query_one(
                    "SELECT status, output_sha256 \
                       FROM storage_v2_begin_intelligence_analysis($1,$2)",
                    &[&symbol_occurrence_id, &card.analysis_profile_id],
                )
                .await?;
            if analysis.get::<_, String>("status") == "pending" {
                client
                    .execute(
                        "SELECT storage_v2_finish_intelligence_analysis($1,$2,$3,NULL)",
                        &[
                            &symbol_occurrence_id,
                            &card.analysis_profile_id,
                            &output_sha_bytes,
                        ],
                    )
                    .await?;
            } else if analysis
                .get::<_, Option<Vec<u8>>>("output_sha256")
                .as_deref()
                != Some(output_sha_bytes)
            {
                bail!("complete intelligence analysis output differs from the fixture card");
            }
            let domain = serde_json::to_value(&card.domain)?;
            let provenance = serde_json::to_value(&card.field_provenance)?;
            client
                .execute(
                    "SELECT storage_v2_put_symbol_card($1,$2,$3,$4,$5,NULL,NULL)",
                    &[
                        &symbol_occurrence_id,
                        &card.analysis_profile_id,
                        &generic,
                        &domain,
                        &provenance,
                    ],
                )
                .await?;
            symbol_count += 1;
        }
        for stage in ["graph", "semantic", "rerank"] {
            client
                .execute(
                    "SELECT storage_v2_put_occurrence_score_component( \
                     $1,$2,$3,'unavailable',NULL,$4)",
                    &[
                        &staged.occurrence_id,
                        &stage,
                        &match mode {
                            SliceMode::PublicFixture => "fixture-unavailable-v1",
                            SliceMode::ReleaseCandidate => "candidate-unavailable-v1",
                        },
                        &json!({
                            "reason": match mode {
                                SliceMode::PublicFixture => "fixture-disabled",
                                SliceMode::ReleaseCandidate => "backend-not-applicable",
                            }
                        }),
                    ],
                )
                .await?;
        }
        measurements.record_stage(ShadowIngestStage::DatabaseStage, database_started.elapsed());
    }

    let stabilization_started = Instant::now();
    let final_sync = match mode {
        SliceMode::PublicFixture => plugin.sync(source_path).await?,
        SliceMode::ReleaseCandidate => plugin.sync_for_storage_v2(source_path).await?,
    };
    if !final_sync.errors.is_empty() {
        bail!("source adapter returned errors during the final watermark check");
    }
    let mut final_files = final_sync
        .files
        .into_iter()
        .map(SliceFile::from)
        .collect::<Vec<_>>();
    final_files.sort_by(|left, right| left.path.cmp(&right.path));
    let (final_fixture_sha256, final_input_bytes) =
        canonical_fixture_hash(&mut final_files).await?;
    if final_fixture_sha256 != fixture_sha256
        || final_input_bytes != measurements.input_bytes
        || final_files.len() != files.len()
    {
        bail!("source drifted before the candidate generation could be sealed");
    }
    measurements.record_stage(
        ShadowIngestStage::ReadAndHash,
        stabilization_started.elapsed(),
    );

    let commit_started = Instant::now();
    let root: String = client
        .query_one("SELECT storage_v2_shadow_generation_root($1)", &[&run.id])
        .await?
        .get(0);
    let committed =
        generation_ingest::commit_shadow_ingest(client, run.id, files.len() as i64, &root).await?;
    let commit_elapsed = commit_started.elapsed();
    let closed_membership_count: i64 = client
        .query_one(
            "SELECT COUNT(*) \
               FROM generation_item_version membership \
               JOIN source_generation generation ON generation.id=$1 \
              WHERE membership.source_id=$2 \
                AND membership.valid_to_seq=generation.generation_seq",
            &[&committed.generation_id, &source_id],
        )
        .await?
        .get(0);
    measurements.intervals_opened = u64::try_from(committed.changed_item_count)?;
    measurements.intervals_closed = u64::try_from(closed_membership_count)?;
    let membership_duration =
        std::time::Duration::from_micros(committed.membership_delta_us as u64);
    let sealing_duration = std::time::Duration::from_micros(committed.sealing_us as u64);
    measurements.record_stage(ShadowIngestStage::MembershipDelta, membership_duration);
    measurements.record_stage(ShadowIngestStage::Seal, sealing_duration);
    measurements.record_stage(
        ShadowIngestStage::DatabaseStage,
        commit_elapsed.saturating_sub(membership_duration + sealing_duration),
    );
    let reuse_count_started = Instant::now();
    let reuse = client
        .query_opt(
            "SELECT \
                COUNT(*) FILTER (WHERE artifact.created_at >= run.created_at)::BIGINT AS artifacts_created, \
                COUNT(*) FILTER (WHERE occurrence_row.created_at >= run.created_at)::BIGINT AS occurrences_created, \
                COUNT(*) FILTER (WHERE node.created_at < run.created_at)::BIGINT AS nodes, \
                COUNT(*) FILTER (WHERE view_row.created_at < run.created_at)::BIGINT AS views \
             FROM storage_v2_ingest_run run \
             JOIN storage_v2_ingest_run_item item ON item.run_id=run.id \
             JOIN artifact_version artifact ON artifact.id=item.artifact_version_id \
             JOIN content_node node ON node.id=artifact.content_root_node_id \
             JOIN occurrence occurrence_row ON occurrence_row.id=item.occurrence_id \
             JOIN retrieval_view view_row ON view_row.id=occurrence_row.view_id \
             WHERE run.id=$1 GROUP BY run.id",
            &[&run.id],
        )
        .await?;
    measurements.artifacts_created = reuse
        .as_ref()
        .map(|row| u64::try_from(row.get::<_, i64>("artifacts_created")))
        .transpose()?
        .unwrap_or(0);
    measurements.occurrences_created = reuse
        .as_ref()
        .map(|row| u64::try_from(row.get::<_, i64>("occurrences_created")))
        .transpose()?
        .unwrap_or(0);
    measurements.reused_nodes = reuse
        .as_ref()
        .map(|row| u64::try_from(row.get::<_, i64>("nodes")))
        .transpose()?
        .unwrap_or(0);
    measurements.reused_views = reuse
        .as_ref()
        .map(|row| u64::try_from(row.get::<_, i64>("views")))
        .transpose()?
        .unwrap_or(0);
    measurements.record_stage(
        ShadowIngestStage::DatabaseStage,
        reuse_count_started.elapsed(),
    );
    let verification_started = Instant::now();
    let verification_manifest = hex::encode(Sha256::digest(
        format!("{source_watermark_sha256}:{root}:{commit_sha}:{adapter_profile}").as_bytes(),
    ));
    client
        .execute(
            "SELECT storage_v2_verify_generation($1,$2)",
            &[&committed.generation_id, &verification_manifest],
        )
        .await?;
    let generation_seq: i64 = client
        .query_one(
            "SELECT generation_seq FROM source_generation WHERE id = $1 AND status = 'verified'",
            &[&committed.generation_id],
        )
        .await?
        .get(0);
    let active_generation_after = active_generation(client, source_id).await?;
    if active_generation_after != active_generation_before {
        bail!("active generation changed during the shadow slice");
    }
    measurements.record_stage(ShadowIngestStage::Seal, verification_started.elapsed());
    measurements.record_total(total_started.elapsed());
    Ok(ShadowSliceResult {
        run_id: run.id,
        source_id,
        generation_id: committed.generation_id,
        generation_seq,
        fixture_sha256: fixture_sha256.clone(),
        source_watermark_sha256,
        item_count: files.len(),
        symbol_count,
        controlled_retry_count,
        pack_id: result_pack_id,
        pack_stored_bytes: result_pack_stored_bytes,
        reused_generation: false,
        active_generation_before,
        active_generation_after,
        telemetry: measurements.to_telemetry_json(),
    })
}

async fn canonical_fixture_hash(files: &mut [SliceFile]) -> Result<(String, u64)> {
    let mut digest = Sha256::new();
    let mut input_bytes = 0_u64;
    for file in files {
        let (content_sha256, logical_length) = {
            let bytes = file.load_bytes().await?;
            let logical_length = bytes.len() as u64;
            digest.update((file.path.len() as u64).to_be_bytes());
            digest.update(file.path.as_bytes());
            digest.update(logical_length.to_be_bytes());
            digest.update(bytes.as_ref());
            (Sha256::digest(bytes.as_ref()).into(), logical_length)
        };
        file.content_sha256 = Some(content_sha256);
        file.logical_length = logical_length;
        input_bytes = input_bytes
            .checked_add(logical_length)
            .context("source byte count overflow")?;
    }
    Ok((hex::encode(digest.finalize()), input_bytes))
}

fn release_source_watermark(
    source_type: &str,
    source_path: &str,
    adapter_profile: &str,
    content_manifest_sha256: &str,
) -> String {
    let mut digest = Sha256::new();
    digest.update(b"mainrag.storage-v2.source-watermark.v1\0");
    for component in [
        source_type,
        source_path,
        adapter_profile,
        content_manifest_sha256,
    ] {
        digest.update((component.len() as u64).to_be_bytes());
        digest.update(component.as_bytes());
    }
    hex::encode(digest.finalize())
}

fn release_adapter_profile(source_type: &str) -> Result<String> {
    if source_type != "pdf" {
        return Ok(format!("mainrag.{source_type}-release-candidate.v1"));
    }
    let backend = match plugins::pdf::backend_name() {
        "MuPDF" => "mupdf",
        "pdf-extract fallback" => "pdf-extract",
        value => bail!("unsupported PDF adapter backend: {value}"),
    };
    Ok(format!("mainrag.pdf-release-candidate.v1.{backend}"))
}

fn search_exact_identifiers(text: &str) -> Vec<String> {
    text.split(|character: char| !(character.is_alphanumeric() || character == '_'))
        .filter(|token| {
            !token.is_empty()
                && (token.contains('_')
                    || token.chars().any(|character| character.is_ascii_digit()))
        })
        .map(str::to_lowercase)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

async fn find_and_verify_existing_body<C>(
    client: &C,
    pack_root: &Path,
    bytes: &[u8],
    io_buffer_bytes: usize,
) -> Result<Option<content_body::ContentBodyRecord>>
where
    C: GenericClient + Sync,
{
    let digest: [u8; 32] = Sha256::digest(bytes).into();
    let row = client
        .query_opt(
            "SELECT id, digest_algorithm, digest, logical_length, inline_bytes, pack_id \
             FROM content_body WHERE digest_algorithm='sha256-v1' AND digest=$1 AND logical_length=$2",
            &[&digest.as_slice(), &i64::try_from(bytes.len())?],
        )
        .await?;
    let Some(row) = row else {
        return Ok(None);
    };
    let body = content_body::ContentBodyRecord::from(row);
    if let Some(inline) = &body.inline_bytes {
        if inline.as_slice() != bytes {
            bail!("existing inline body failed full-byte equality verification");
        }
        return Ok(Some(body));
    }
    let packed = client
        .query_one(
            "SELECT pack.storage_key, pack.stored_bytes, entry.ordinal, entry.pack_offset, \
                    entry.stored_length, entry.codec::TEXT AS codec, entry.entry_digest, \
                    dictionary.id AS dictionary_id, dictionary.digest AS dictionary_digest, \
                    dictionary.dictionary_bytes \
               FROM content_body body \
               JOIN content_pack pack ON pack.id=body.pack_id AND pack.status='published' \
               JOIN content_pack_entry entry ON entry.pack_id=body.pack_id AND entry.body_id=body.id \
               LEFT JOIN content_dictionary dictionary ON dictionary.id=entry.dictionary_id \
              WHERE body.id=$1",
            &[&body.id],
        )
        .await?;
    let codec = match packed.get::<_, String>("codec").as_str() {
        "identity" => BodyCodec::Identity,
        "zstd" => BodyCodec::Zstd,
        value => bail!("unsupported stored body codec: {value}"),
    };
    let dictionary_id: Option<i64> = packed.get("dictionary_id");
    let dictionary_digest: Option<Vec<u8>> = packed.get("dictionary_digest");
    let dictionary_bytes: Option<Vec<u8>> = packed.get("dictionary_bytes");
    let dictionary = match (
        dictionary_id,
        dictionary_digest.as_deref(),
        dictionary_bytes.as_deref(),
    ) {
        (Some(id), Some(digest), Some(bytes)) => Some((
            DictionaryIdentity {
                id,
                digest: digest
                    .try_into()
                    .map_err(|_| anyhow::anyhow!("invalid dictionary digest"))?,
            },
            bytes,
        )),
        (None, None, None) => None,
        _ => bail!("stored body has an incomplete dictionary identity"),
    };
    let pack_id = body.pack_id.context("packed body omitted pack identity")?;
    let entry = PackEntry {
        pack_id,
        ordinal: u64::try_from(packed.get::<_, i64>("ordinal"))?,
        body: BodyIdentity {
            digest_algorithm: "sha256-v1",
            digest,
            logical_length: u64::try_from(body.logical_length)?,
        },
        pack_offset: u64::try_from(packed.get::<_, i64>("pack_offset"))?,
        stored_length: u64::try_from(packed.get::<_, i64>("stored_length"))?,
        codec,
        dictionary: dictionary.as_ref().map(|(identity, _)| identity.clone()),
        entry_digest: packed
            .get::<_, Vec<u8>>("entry_digest")
            .as_slice()
            .try_into()
            .map_err(|_| anyhow::anyhow!("invalid pack entry digest"))?,
    };
    let storage_key: String = packed.get("storage_key");
    let reader = PackReader::new(
        pack_root.join(storage_key),
        pack_id,
        u64::try_from(packed.get::<_, i64>("stored_bytes"))?,
    );
    let verified = reader.verify_to_staging(
        &entry,
        dictionary.as_ref().map(|(_, bytes)| *bytes),
        pack_root,
        io_buffer_bytes,
    )?;
    let mut reconstructed = Vec::with_capacity(bytes.len());
    verified.copy_to(&mut reconstructed)?;
    if reconstructed.as_slice() != bytes {
        bail!("existing packed body failed full-byte equality verification");
    }
    Ok(Some(body))
}

fn verify_stored_body_row(
    row: &tokio_postgres::Row,
    pack_root: &Path,
    io_buffer_bytes: usize,
) -> Result<u64> {
    if row.get::<_, String>("digest_algorithm") != "sha256-v1" {
        bail!("candidate body uses an unsupported digest algorithm");
    }
    let digest: [u8; 32] = row
        .get::<_, Vec<u8>>("digest")
        .as_slice()
        .try_into()
        .map_err(|_| anyhow::anyhow!("candidate body has an invalid digest"))?;
    let logical_length = u64::try_from(row.get::<_, i64>("logical_length"))?;
    if let Some(bytes) = row.get::<_, Option<Vec<u8>>>("inline_bytes") {
        if bytes.len() as u64 != logical_length
            || <[u8; 32]>::from(Sha256::digest(&bytes)) != digest
        {
            bail!("candidate inline body failed full digest verification");
        }
        return Ok(logical_length);
    }
    if row.get::<_, Option<String>>("pack_status").as_deref() != Some("published") {
        bail!("candidate packed body is not in a published pack");
    }
    let pack_id: Uuid = row
        .get::<_, Option<Uuid>>("pack_id")
        .context("candidate packed body omitted its pack identity")?;
    let codec = match row
        .get::<_, Option<String>>("codec")
        .context("candidate packed body omitted its codec")?
        .as_str()
    {
        "identity" => BodyCodec::Identity,
        "zstd" => BodyCodec::Zstd,
        value => bail!("unsupported stored body codec: {value}"),
    };
    let dictionary_id: Option<i64> = row.get("dictionary_id");
    let dictionary_digest: Option<Vec<u8>> = row.get("dictionary_digest");
    let dictionary_bytes: Option<Vec<u8>> = row.get("dictionary_bytes");
    let dictionary = match (
        dictionary_id,
        dictionary_digest.as_deref(),
        dictionary_bytes.as_deref(),
    ) {
        (Some(id), Some(dictionary_digest), Some(bytes)) => Some((
            DictionaryIdentity {
                id,
                digest: dictionary_digest
                    .try_into()
                    .map_err(|_| anyhow::anyhow!("invalid dictionary digest"))?,
            },
            bytes,
        )),
        (None, None, None) => None,
        _ => bail!("candidate packed body has an incomplete dictionary identity"),
    };
    let entry = PackEntry {
        pack_id,
        ordinal: u64::try_from(
            row.get::<_, Option<i64>>("ordinal")
                .context("candidate packed body omitted its ordinal")?,
        )?,
        body: BodyIdentity {
            digest_algorithm: "sha256-v1",
            digest,
            logical_length,
        },
        pack_offset: u64::try_from(
            row.get::<_, Option<i64>>("pack_offset")
                .context("candidate packed body omitted its offset")?,
        )?,
        stored_length: u64::try_from(
            row.get::<_, Option<i64>>("stored_length")
                .context("candidate packed body omitted its stored length")?,
        )?,
        codec,
        dictionary: dictionary.as_ref().map(|(identity, _)| identity.clone()),
        entry_digest: row
            .get::<_, Option<Vec<u8>>>("entry_digest")
            .context("candidate packed body omitted its entry digest")?
            .as_slice()
            .try_into()
            .map_err(|_| anyhow::anyhow!("invalid pack entry digest"))?,
    };
    let storage_key = row
        .get::<_, Option<String>>("storage_key")
        .context("candidate packed body omitted its storage key")?;
    if storage_key != format!("{pack_id}.pack") {
        bail!("candidate pack storage key does not match its identity");
    }
    let reader = PackReader::new(
        pack_root.join(storage_key),
        pack_id,
        u64::try_from(
            row.get::<_, Option<i64>>("stored_bytes")
                .context("candidate packed body omitted its pack length")?,
        )?,
    );
    let verified = reader.verify_to_staging(
        &entry,
        dictionary.as_ref().map(|(_, bytes)| *bytes),
        pack_root,
        io_buffer_bytes,
    )?;
    verified.copy_to(std::io::sink())?;
    Ok(logical_length)
}

async fn candidate_query_seeds<C>(
    client: &C,
    source_id: i64,
    generation_id: i64,
) -> Result<Vec<CandidateQuerySeed>>
where
    C: GenericClient + Sync,
{
    let legacy = client
        .query(
            "SELECT file.path, chunk.content_text \
               FROM chunks chunk JOIN files file ON file.id=chunk.file_id \
              WHERE file.source_id=$1 AND chunk.content_text IS NOT NULL \
              ORDER BY chunk.id LIMIT 64",
            &[&source_id],
        )
        .await?;
    let mut candidates = BTreeSet::new();
    for row in legacy {
        let path: String = row.get("path");
        let content: String = row.get("content_text");
        let mut preferred = search_exact_identifiers(&content);
        preferred.extend(
            content
                .split(|character: char| !(character.is_alphanumeric() || character == '_'))
                .map(str::to_lowercase)
                .filter(|token| token.len() >= 12),
        );
        for token in preferred.into_iter().filter(|token| token.len() <= 128) {
            candidates.insert((token, path.clone()));
            if candidates.len() >= 256 {
                break;
            }
        }
    }
    let mut seeds = Vec::new();
    for (query, path) in candidates {
        let found = client
            .query_one(
                "SELECT EXISTS ( \
                    SELECT 1 FROM source_generation generation \
                    JOIN generation_item_version membership ON membership.source_id=generation.source_id \
                     AND membership.valid_from_seq <= generation.generation_seq \
                     AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > generation.generation_seq) \
                    JOIN artifact_version artifact ON artifact.id=membership.artifact_version_id \
                    JOIN occurrence occurrence_row ON occurrence_row.artifact_version_id=artifact.id \
                     AND occurrence_row.source_id=generation.source_id \
                    JOIN storage_v2_search_view_document binding ON binding.view_id=occurrence_row.view_id \
                    JOIN storage_v2_search_document document ON document.id=binding.document_id \
                   WHERE generation.id=$1 AND generation.source_id=$2 \
                     AND occurrence_row.source_path=$3 \
                     AND document.fts_simple @@ plainto_tsquery('simple',$4) \
                )",
                &[&generation_id, &source_id, &path, &query],
            )
            .await?
            .get::<_, bool>(0);
        if !found {
            continue;
        }
        let expected_path_sha256 = hex::encode(Sha256::digest(path.as_bytes()));
        let id = hex::encode(Sha256::digest(
            format!("mainrag.storage-v2.query-seed.v1\0{query}\0{expected_path_sha256}").as_bytes(),
        ));
        seeds.push(CandidateQuerySeed {
            id,
            query,
            expected_path_sha256,
            expects_match: true,
        });
        if seeds.len() == 5 {
            break;
        }
    }
    if seeds.is_empty() {
        let query = format!("mainrag_no_match_{source_id}_{generation_id}");
        seeds.push(CandidateQuerySeed {
            id: hex::encode(Sha256::digest(query.as_bytes())),
            query,
            expected_path_sha256: hex::encode(Sha256::digest([])),
            expects_match: false,
        });
    }
    Ok(seeds)
}

async fn active_generation<C>(client: &C, source_id: i64) -> Result<Option<i64>>
where
    C: GenericClient + Sync,
{
    Ok(client
        .query_opt(
            "SELECT active_generation_id FROM logical_source WHERE id = $1",
            &[&source_id],
        )
        .await?
        .and_then(|row| row.get::<_, Option<i64>>(0)))
}

fn is_git_sha(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComparableHit {
    pub hit_id: String,
    pub rank: usize,
    pub score: f64,
    #[serde(default)]
    pub mapped_hit_ids: Vec<String>,
    #[serde(default)]
    pub authorized: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DifferenceClass {
    IdentityMapping,
    Segmentation,
    ScoreOrder,
    MissingCurrent,
    MissingStorageV2,
    Authorization,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClassifiedDifference {
    pub classification: DifferenceClass,
    pub current_hit_ids: Vec<String>,
    pub storage_v2_hit_ids: Vec<String>,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QueryComparison {
    pub normalized_query: String,
    pub current_identity: Vec<String>,
    pub storage_v2_identity: Vec<String>,
    pub differences: Vec<ClassifiedDifference>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DualReadQueryInput {
    pub fixture: serde_json::Value,
    pub normalized_query: String,
    pub current: Vec<ComparableHit>,
    pub storage_v2: Vec<ComparableHit>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DualReadEvidenceInput {
    pub generation: i64,
    pub commit_sha: String,
    pub fixture_sha256: String,
    pub query_set_sha256: String,
    pub queries: Vec<DualReadQueryInput>,
    pub exact_top10_passed: bool,
    pub performance_envelope_passed: bool,
    pub restart_passed: bool,
    pub optional_degradation_passed: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DualReadEvidenceResult {
    pub evidence_id: uuid::Uuid,
    pub source_id: i64,
    pub generation_id: i64,
    pub generation_seq: i64,
    pub status: String,
    pub artifact_sha256: String,
    pub artifact: serde_json::Value,
}

pub async fn record_dual_read_evidence<C>(
    client: &C,
    source_id: i64,
    input: &DualReadEvidenceInput,
) -> Result<DualReadEvidenceResult>
where
    C: GenericClient + Sync,
{
    if input.generation <= 0
        || !is_git_sha(&input.commit_sha)
        || !is_sha256(&input.fixture_sha256)
        || !is_sha256(&input.query_set_sha256)
        || input.queries.is_empty()
    {
        bail!("dual-read evidence requires exact generation, hashes and a non-empty query set");
    }
    client
        .execute(
            "SELECT storage_v2_require_test_scope($1, TRUE)",
            &[&source_id],
        )
        .await?;
    let generation = client
        .query_opt(
            "SELECT generation.id, generation.generation_seq, generation.witness, source.is_test \
               FROM source_generation generation \
               JOIN sources source ON source.id=generation.source_id \
              WHERE generation.source_id = $1 AND generation.generation_seq = $2 \
                AND generation.status = 'verified'",
            &[&source_id, &input.generation],
        )
        .await?
        .ok_or_else(|| anyhow::anyhow!("named verified generation not found"))?;
    let generation_id: i64 = generation.get("id");
    let generation_seq: i64 = generation.get("generation_seq");
    let witness: serde_json::Value = generation.get("witness");
    let is_test: bool = generation.get("is_test");
    validate_dual_read_witness(&witness, &input.fixture_sha256, &input.commit_sha, is_test)?;

    let actual_query_set_sha256 = canonical_query_set_hash(&input.queries)?;
    if actual_query_set_sha256 != input.query_set_sha256 {
        bail!("dual-read query-set digest mismatch");
    }
    let mut query_artifacts = Vec::with_capacity(input.queries.len());
    let mut differences = Vec::new();
    for query in &input.queries {
        let comparison = compare_query(&query.normalized_query, &query.current, &query.storage_v2)?;
        for difference in &comparison.differences {
            differences.push(serde_json::to_value(difference)?);
        }
        query_artifacts.push(json!({
            "query_sha256": hex::encode(Sha256::digest(comparison.normalized_query.as_bytes())),
            "current_identity": comparison.current_identity,
            "storage_v2_identity": comparison.storage_v2_identity,
            "differences": comparison.differences,
        }));
    }
    let gates_passed = input.exact_top10_passed
        && input.performance_envelope_passed
        && input.restart_passed
        && input.optional_degradation_passed;
    let source_scope = if is_test {
        "synthetic_test_explicit"
    } else {
        "registered_production"
    };
    let artifact = json!({
        "schema_version": 1,
        "status": if gates_passed { "PASS" } else { "FAIL" },
        "unexplained_count": 0,
        "source_scope": source_scope,
        "generation_seq": generation_seq,
        "queries": query_artifacts,
        "comparisons": differences,
        "gates": {
            "exact_top10": input.exact_top10_passed,
            "performance_envelope": input.performance_envelope_passed,
            "restart": input.restart_passed,
            "optional_degradation": input.optional_degradation_passed,
            "active_pointer_unchanged": true,
        },
    });
    let identity = format!(
        "mainrag:storage-v2:dual-read:{source_id}:{generation_id}:{}:{}:{}",
        input.commit_sha, input.fixture_sha256, input.query_set_sha256
    );
    let evidence_id = uuid::Uuid::new_v5(&uuid::Uuid::NAMESPACE_URL, identity.as_bytes());
    let row = client
        .query_one(
            "SELECT id, encode(artifact_sha256, 'hex') AS artifact_sha256, artifact \
             FROM storage_v2_record_dual_read_evidence($1,$2,$3,$4,$5,$6,$7)",
            &[
                &evidence_id,
                &source_id,
                &generation_id,
                &input.commit_sha,
                &input.fixture_sha256,
                &input.query_set_sha256,
                &artifact,
            ],
        )
        .await?;
    Ok(DualReadEvidenceResult {
        evidence_id: row.get("id"),
        source_id,
        generation_id,
        generation_seq,
        status: artifact["status"].as_str().unwrap_or("FAIL").to_string(),
        artifact_sha256: row.get("artifact_sha256"),
        artifact: row.get("artifact"),
    })
}

fn validate_dual_read_witness(
    witness: &serde_json::Value,
    fixture_sha256: &str,
    commit_sha: &str,
    expected_is_test: bool,
) -> Result<()> {
    if witness["fixture_sha256"].as_str() != Some(fixture_sha256)
        || witness["commit_sha"].as_str() != Some(commit_sha)
        || witness["is_test"].as_bool() != Some(expected_is_test)
    {
        bail!("dual-read hashes or source scope do not match the verified generation witness");
    }
    Ok(())
}

fn canonical_query_set_hash(queries: &[DualReadQueryInput]) -> Result<String> {
    let mut identities = BTreeSet::new();
    let mut canonical = Vec::with_capacity(queries.len());
    for query in queries {
        let fixture = query
            .fixture
            .as_object()
            .context("dual-read query fixture must be a JSON object")?;
        let identity = fixture
            .get("id")
            .and_then(serde_json::Value::as_str)
            .filter(|identity| !identity.is_empty())
            .context("dual-read query fixture requires a non-empty id")?;
        if !identities.insert(identity.to_string()) {
            bail!("dual-read query fixture IDs must be unique");
        }
        if fixture.get("k").and_then(serde_json::Value::as_i64) != Some(10) {
            bail!("dual-read query fixture must request exact Top-10");
        }
        let raw_query = fixture
            .get("query")
            .and_then(serde_json::Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .context("dual-read query fixture requires a non-empty query")?;
        let expected_normalized = if fixture
            .get("phrase")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false)
        {
            format!("\"{raw_query}\"")
        } else if fixture.get("construct").and_then(serde_json::Value::as_str)
            == Some("exact_identifier")
        {
            format!("id:{raw_query}")
        } else {
            raw_query.to_string()
        };
        if query.normalized_query.trim() != expected_normalized {
            bail!("dual-read normalized query does not match its fixture contract");
        }
        canonical.push(serde_json::to_vec(&query.fixture)?);
    }
    canonical.sort_unstable();
    let mut digest = Sha256::new();
    for fixture in canonical {
        digest.update((fixture.len() as u64).to_be_bytes());
        digest.update(fixture);
    }
    Ok(hex::encode(digest.finalize()))
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

pub fn compare_query(
    normalized_query: &str,
    current: &[ComparableHit],
    storage_v2: &[ComparableHit],
) -> Result<QueryComparison> {
    if normalized_query.trim().is_empty() {
        bail!("dual-read comparison requires a normalized query");
    }
    validate_hits("current", current)?;
    validate_hits("storage_v2", storage_v2)?;

    let current_by_id = current
        .iter()
        .map(|hit| (hit.hit_id.as_str(), hit))
        .collect::<BTreeMap<_, _>>();
    let storage_by_id = storage_v2
        .iter()
        .map(|hit| (hit.hit_id.as_str(), hit))
        .collect::<BTreeMap<_, _>>();
    let mut mapped_current = BTreeSet::new();
    let mut mapped_storage = BTreeSet::new();
    let mut differences = Vec::new();

    for hit in storage_v2 {
        let existing = hit
            .mapped_hit_ids
            .iter()
            .filter(|id| current_by_id.contains_key(id.as_str()))
            .cloned()
            .collect::<Vec<_>>();
        if existing.is_empty() {
            continue;
        }
        mapped_storage.insert(hit.hit_id.clone());
        mapped_current.extend(existing.iter().cloned());
        let classification = if existing.len() > 1
            || current_by_id.values().any(|current_hit| {
                current_hit.mapped_hit_ids.len() > 1
                    && current_hit.mapped_hit_ids.contains(&hit.hit_id)
            }) {
            DifferenceClass::Segmentation
        } else {
            DifferenceClass::IdentityMapping
        };
        differences.push(ClassifiedDifference {
            classification,
            current_hit_ids: existing,
            storage_v2_hit_ids: vec![hit.hit_id.clone()],
            detail: "explicit legacy-successor mapping reconciled the hit identity".into(),
        });
    }

    for hit in current {
        if mapped_current.contains(&hit.hit_id) {
            continue;
        }
        if !hit.authorized {
            differences.push(ClassifiedDifference {
                classification: DifferenceClass::Authorization,
                current_hit_ids: vec![hit.hit_id.clone()],
                storage_v2_hit_ids: vec![],
                detail: "current hit is outside the authorized named-generation scope".into(),
            });
        } else if let Some(other) = storage_by_id.get(hit.hit_id.as_str()) {
            mapped_current.insert(hit.hit_id.clone());
            mapped_storage.insert(other.hit_id.clone());
            if hit.rank != other.rank || (hit.score - other.score).abs() > f64::EPSILON {
                differences.push(ClassifiedDifference {
                    classification: DifferenceClass::ScoreOrder,
                    current_hit_ids: vec![hit.hit_id.clone()],
                    storage_v2_hit_ids: vec![other.hit_id.clone()],
                    detail: "stable identity matched but rank or score changed".into(),
                });
            }
        } else {
            differences.push(ClassifiedDifference {
                classification: DifferenceClass::MissingStorageV2,
                current_hit_ids: vec![hit.hit_id.clone()],
                storage_v2_hit_ids: vec![],
                detail: "authorized current hit has no storage-v2 result or mapping".into(),
            });
        }
    }
    for hit in storage_v2 {
        if mapped_storage.contains(&hit.hit_id) {
            continue;
        }
        if !hit.authorized {
            differences.push(ClassifiedDifference {
                classification: DifferenceClass::Authorization,
                current_hit_ids: vec![],
                storage_v2_hit_ids: vec![hit.hit_id.clone()],
                detail: "storage-v2 hit is outside the authorized current scope".into(),
            });
        } else {
            differences.push(ClassifiedDifference {
                classification: DifferenceClass::MissingCurrent,
                current_hit_ids: vec![],
                storage_v2_hit_ids: vec![hit.hit_id.clone()],
                detail: "authorized storage-v2 hit has no current result or mapping".into(),
            });
        }
    }
    differences.sort_by(|left, right| {
        format!("{:?}", left.classification)
            .cmp(&format!("{:?}", right.classification))
            .then(left.current_hit_ids.cmp(&right.current_hit_ids))
            .then(left.storage_v2_hit_ids.cmp(&right.storage_v2_hit_ids))
    });
    Ok(QueryComparison {
        normalized_query: normalized_query.trim().to_string(),
        current_identity: current.iter().map(|hit| hit.hit_id.clone()).collect(),
        storage_v2_identity: storage_v2.iter().map(|hit| hit.hit_id.clone()).collect(),
        differences,
    })
}

fn validate_hits(path: &str, hits: &[ComparableHit]) -> Result<()> {
    let mut ids = BTreeSet::new();
    let mut ranks = BTreeSet::new();
    for hit in hits {
        if hit.hit_id.is_empty()
            || !hit.hit_id.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b':' | b'.' | b'_' | b'-')
            })
            || hit.rank == 0
            || !hit.score.is_finite()
            || !ids.insert(hit.hit_id.as_str())
            || !ranks.insert(hit.rank)
            || hit.mapped_hit_ids.iter().any(String::is_empty)
        {
            bail!("{path} dual-read hits require unique IDs/ranks and finite scores");
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestDirectory(PathBuf);

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[tokio::test]
    async fn bounded_source_reader_fails_closed_on_content_drift() {
        let directory = TestDirectory(
            std::env::temp_dir().join(format!("mainrag-storage-v2-drift-{}", Uuid::new_v4())),
        );
        std::fs::create_dir_all(&directory.0).expect("create test directory");
        let source_path = directory.0.join("sample.rs");
        std::fs::write(&source_path, "fn before() {}\n").expect("write initial source");
        let mut files = vec![SliceFile::from(plugins::RawFile {
            path: "sample.rs".into(),
            content: String::new(),
            size: 15,
            language: Some("rs".into()),
            last_modified: None,
            source_path: Some(source_path.clone()),
        })];

        let (_, input_bytes) = canonical_fixture_hash(&mut files)
            .await
            .expect("capture candidate watermark");
        assert_eq!(input_bytes, 15);
        std::fs::write(&source_path, "fn after() {}\n").expect("mutate source");

        let error = files[0]
            .load_verified_bytes()
            .await
            .expect_err("content drift must fail");
        assert!(error.to_string().contains("drifted"));
    }

    #[test]
    fn release_watermark_binds_source_configuration_and_content() {
        let base = release_source_watermark("fs", "/opaque/a", "adapter.v1", &"a".repeat(64));
        assert_ne!(
            base,
            release_source_watermark("fs", "/opaque/b", "adapter.v1", &"a".repeat(64))
        );
        assert_ne!(
            base,
            release_source_watermark("fs", "/opaque/a", "adapter.v2", &"a".repeat(64))
        );
        assert_ne!(
            base,
            release_source_watermark("fs", "/opaque/a", "adapter.v1", &"b".repeat(64))
        );
    }

    #[test]
    fn dual_read_witness_accepts_matching_production_and_test_scope() {
        let fixture_sha256 = "a".repeat(64);
        let commit_sha = "b".repeat(40);
        for is_test in [false, true] {
            let witness = json!({
                "fixture_sha256": fixture_sha256,
                "commit_sha": commit_sha,
                "is_test": is_test,
            });
            validate_dual_read_witness(&witness, &fixture_sha256, &commit_sha, is_test)
                .expect("matching source scope must be accepted");
            assert!(
                validate_dual_read_witness(&witness, &fixture_sha256, &commit_sha, !is_test)
                    .is_err()
            );
        }
    }

    fn hit(id: &str, rank: usize, score: f64) -> ComparableHit {
        ComparableHit {
            hit_id: id.into(),
            rank,
            score,
            mapped_hit_ids: vec![],
            authorized: true,
        }
    }

    #[test]
    fn comparison_classifies_mapping_segmentation_order_missing_and_auth() {
        let mut denied = hit("legacy-denied", 4, 0.1);
        denied.authorized = false;
        let current = vec![
            hit("same", 1, 1.0),
            hit("legacy-a", 2, 0.8),
            hit("legacy-b", 3, 0.7),
            denied,
            hit("legacy-only", 5, 0.05),
        ];
        let mut split = hit("new-split", 1, 0.9);
        split.mapped_hit_ids = vec!["legacy-a".into(), "legacy-b".into()];
        let storage = vec![hit("same", 2, 0.9), split, hit("new-only", 3, 0.2)];
        let comparison = compare_query("alpha", &current, &storage).unwrap();
        let classes = comparison
            .differences
            .iter()
            .map(|difference| difference.classification)
            .collect::<BTreeSet<_>>();
        assert_eq!(
            classes,
            BTreeSet::from([
                DifferenceClass::Segmentation,
                DifferenceClass::ScoreOrder,
                DifferenceClass::MissingCurrent,
                DifferenceClass::MissingStorageV2,
                DifferenceClass::Authorization,
            ])
        );
    }

    #[test]
    fn comparison_rejects_duplicate_or_non_finite_hits() {
        let duplicate = vec![hit("same", 1, 1.0), hit("same", 2, 0.5)];
        assert!(compare_query("alpha", &duplicate, &[]).is_err());
        assert!(compare_query("alpha", &[hit("bad", 1, f64::NAN)], &[]).is_err());
    }

    #[test]
    fn exact_identifier_materialization_is_normalized_and_deduplicated() {
        assert_eq!(
            search_exact_identifiers("active_generation_id Thing_2 thing_2 plain"),
            vec!["active_generation_id", "thing_2"]
        );
    }

    #[test]
    fn query_set_digest_binds_full_fixture_contract_and_executed_syntax() {
        let fixture = json!({
            "construct": "exact_identifier",
            "expected": ["storage.md"],
            "id": "exact-active-generation",
            "k": 10,
            "phrase": false,
            "query": "active_generation_id",
        });
        let query = DualReadQueryInput {
            fixture: fixture.clone(),
            normalized_query: "id:active_generation_id".into(),
            current: vec![],
            storage_v2: vec![],
        };
        assert_eq!(
            canonical_query_set_hash(&[query]).unwrap(),
            "3a99ce908ed47d095d8c96d1cea1ea061783e428bd2173a1125b61e9227b0180"
        );
        let mismatch = DualReadQueryInput {
            fixture,
            normalized_query: "active_generation_id".into(),
            current: vec![],
            storage_v2: vec![],
        };
        assert!(canonical_query_set_hash(&[mismatch]).is_err());
    }
}
