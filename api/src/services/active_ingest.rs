//! Ordinary source sync after the reviewed complete-set activation.
//!
//! The source is rebuilt through the verified storage-v2 writer, then one
//! controlled database function advances its active pointer with a receipt.
//! Legacy mutable search state is never written by this path.

use anyhow::{bail, ensure, Context, Result};
use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::Path;
use sysinfo::Disks;
use tokio_postgres::GenericClient;

use super::shadow_slice::{
    observe_release_watermark, observe_release_watermark_with_prefix, run_release_candidate_build,
    verify_release_candidate, ReleaseCandidateVerifyInput, ReleaseCandidateVerifyResult,
    ReleaseWatermarkObservation, ShadowSliceResult,
};
use crate::plugins::managed_append::TrustedPrefix;

const MINIMUM_PACK_FREE_BYTES: u64 = 40 * 1024 * 1024 * 1024;

#[derive(Debug, Serialize)]
pub struct ActiveIngestResult {
    pub status: &'static str,
    pub sync_mode: &'static str,
    pub source_id: i64,
    pub generation_id: i64,
    pub generation_seq: i64,
    pub item_count: usize,
    pub changed_item_count: u64,
    pub source_io: Value,
    pub telemetry: Value,
    pub receipt: Option<Value>,
}

pub enum ActiveIngestPreparation {
    NoChange(ActiveIngestResult),
    Candidate(PreparedActiveSource),
}

pub struct PreparedActiveSource {
    source_id: i64,
    old_id: i64,
    manifest_sha256: String,
    source_type: String,
    source_path: String,
    built: ShadowSliceResult,
    verified: ReleaseCandidateVerifyResult,
}

fn pack_free_bytes(root: &Path) -> Result<u64> {
    let root = root
        .canonicalize()
        .context("storage-v2 pack root is unavailable")?;
    ensure!(root.is_dir(), "storage-v2 pack root is not a directory");
    Disks::new_with_refreshed_list()
        .iter()
        .filter(|disk| root.starts_with(disk.mount_point()))
        .max_by_key(|disk| disk.mount_point().components().count())
        .map(|disk| disk.available_space())
        .context("storage-v2 pack filesystem free space is unavailable")
}

async fn managed_prefix<C: GenericClient + Sync>(
    client: &C,
    source_id: i64,
    generation_id: i64,
    adapter_profile_id: &str,
) -> Result<Option<TrustedPrefix>> {
    let row = client
        .query_opt(
            "SELECT frontier.epoch, frontier.segment_count, frontier.chain, \
                frontier.last_generation_id, generation.status::TEXT AS generation_status, \
                run.expected_active_generation_id \
           FROM storage_v2_managed_append_frontier frontier \
           JOIN storage_v2_ingest_run run ON run.id=frontier.last_run_id \
           JOIN source_generation generation ON generation.id=frontier.last_generation_id \
          WHERE frontier.source_id=$1 AND frontier.adapter_profile_id=$2 \
            AND run.source_id=$1 AND run.generation_id=frontier.last_generation_id \
            AND run.status='sealed' AND generation.source_id=$1 \
            AND generation.status IN ('verified','release_candidate','active')",
            &[&source_id, &adapter_profile_id],
        )
        .await?;
    row.map(|row| {
        let frontier_generation_id: i64 = row.get("last_generation_id");
        if frontier_generation_id != generation_id {
            ensure!(
                row.get::<_, String>("generation_status") == "verified"
                    && row.get::<_, Option<i64>>("expected_active_generation_id")
                        == Some(generation_id),
                "managed append trusted frontier is not bound to the active predecessor"
            );
        }
        Ok(TrustedPrefix {
            epoch: row.get::<_, uuid::Uuid>("epoch").to_string(),
            segments: usize::try_from(row.get::<_, i64>("segment_count"))?,
            chain: hex::encode(row.get::<_, Vec<u8>>("chain")),
        })
    })
    .transpose()
}

async fn observe_active_watermark<C: GenericClient + Sync>(
    client: &C,
    source_id: i64,
    source_type: &str,
    source_path: &Path,
    generation_id: i64,
    adapter_profile_id: &str,
) -> Result<ReleaseWatermarkObservation> {
    if source_type != "managed_append" {
        return observe_release_watermark(source_id, source_type, source_path).await;
    }
    let prefix = managed_prefix(client, source_id, generation_id, adapter_profile_id)
        .await?
        .context("managed append trusted frontier is absent")?;
    observe_release_watermark_with_prefix(source_id, source_type, source_path, Some(&prefix), false)
        .await
}

#[allow(clippy::too_many_arguments)]
pub async fn prepare_active_source<C>(
    client: &C,
    source_id: i64,
    manifest_sha256: &str,
    commit_sha: &str,
    pack_root: &Path,
    io_buffer_bytes: usize,
) -> Result<ActiveIngestPreparation>
where
    C: GenericClient + Sync,
{
    ensure!(source_id > 0, "active ingest requires a source");
    ensure!(
        manifest_sha256.len() == 64
            && manifest_sha256
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()),
        "active ingest requires an exact activation manifest"
    );
    ensure!(
        commit_sha.len() == 40
            && commit_sha
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()),
        "active ingest requires an exact runtime commit"
    );
    ensure!(
        pack_free_bytes(pack_root)? >= MINIMUM_PACK_FREE_BYTES,
        "storage-v2 pack reserve is insufficient before ordinary ingest"
    );

    let source = client
        .query_opt(
            "SELECT source.type, source.path, source.is_test, pointer.active_generation_id, \
                active.generation_seq, active.status::TEXT AS active_status, active.witness \
           FROM sources source \
           JOIN logical_source pointer ON pointer.id=source.id \
           LEFT JOIN source_generation active ON active.id=pointer.active_generation_id \
          WHERE source.id=$1 FOR UPDATE OF pointer",
            &[&source_id],
        )
        .await?
        .context("registered active source is unavailable")?;
    let old_id: i64 = source
        .get::<_, Option<i64>>("active_generation_id")
        .context("ordinary ingest requires an activated source")?;
    ensure!(
        !source.get::<_, bool>("is_test")
            && source.get::<_, Option<String>>("active_status").as_deref() == Some("active"),
        "ordinary ingest requires a non-test active source"
    );
    client
        .query_one(
            "SELECT storage_v2_require_complete_active_set($1)",
            &[&manifest_sha256],
        )
        .await?;
    let source_type: String = source.get("type");
    let source_path: String = source.get("path");
    let witness: Value = source.get("witness");
    let adapter_profile_id = witness["adapter_profile_id"]
        .as_str()
        .context("active generation adapter profile is absent")?;
    let observation = observe_active_watermark(
        client,
        source_id,
        &source_type,
        Path::new(&source_path),
        old_id,
        adapter_profile_id,
    )
    .await?;
    if witness["source_watermark_sha256"].as_str()
        == Some(observation.source_watermark_sha256.as_str())
        && witness["adapter_profile_id"].as_str() == Some(observation.adapter_profile_id.as_str())
    {
        return Ok(ActiveIngestPreparation::NoChange(ActiveIngestResult {
            status: "NO_CHANGE",
            sync_mode: if source_type == "managed_append" {
                "managed_append_verified_prefix"
            } else {
                "full_source_observation"
            },
            source_id,
            generation_id: old_id,
            generation_seq: source.get("generation_seq"),
            item_count: observation.item_count,
            changed_item_count: 0,
            source_io: json!({
                "application_read_bytes": observation.application_read_bytes,
                "logical_input_bytes": observation.input_bytes,
                "device_read_bytes": null,
            }),
            telemetry: json!({"generation_reused": true}),
            receipt: None,
        }));
    }
    ensure!(
        pack_free_bytes(pack_root)?
            >= MINIMUM_PACK_FREE_BYTES.saturating_add(observation.input_bytes),
        "storage-v2 pack filesystem cannot hold the observed source and reserve"
    );

    let built = run_release_candidate_build(
        client,
        source_id,
        &source_type,
        Path::new(&source_path),
        pack_root,
        io_buffer_bytes,
        commit_sha,
    )
    .await?;
    ensure!(
        built.active_generation_before == Some(old_id)
            && built.active_generation_after == Some(old_id)
            && built.source_watermark_sha256 == observation.source_watermark_sha256,
        "ordinary ingest candidate or active pointer drifted"
    );
    let verified = verify_release_candidate(
        client,
        source_id,
        &ReleaseCandidateVerifyInput {
            generation_id: built.generation_id,
        },
        pack_root,
        io_buffer_bytes,
    )
    .await?;
    ensure!(
        verified.status == "verified"
            && verified.generation_id == built.generation_id
            && verified.source_watermark_sha256 == built.source_watermark_sha256
            && verified.active_generation_id == Some(old_id)
            && verified.checks.values().all(|value| value == "PASS"),
        "ordinary ingest verification differs from the sealed generation"
    );
    ensure!(
        pack_free_bytes(pack_root)? >= MINIMUM_PACK_FREE_BYTES,
        "storage-v2 pack reserve is insufficient after ordinary ingest build"
    );
    Ok(ActiveIngestPreparation::Candidate(PreparedActiveSource {
        source_id,
        old_id,
        manifest_sha256: manifest_sha256.to_string(),
        source_type,
        source_path,
        built,
        verified,
    }))
}

pub async fn commit_active_source<C>(
    client: &C,
    prepared: PreparedActiveSource,
    pack_root: &Path,
    io_buffer_bytes: usize,
) -> Result<ActiveIngestResult>
where
    C: GenericClient + Sync,
{
    let PreparedActiveSource {
        source_id,
        old_id,
        manifest_sha256,
        source_type,
        source_path,
        built,
        verified,
    } = prepared;
    client
        .query_one(
            "SELECT storage_v2_require_complete_active_set($1)",
            &[&manifest_sha256],
        )
        .await?;
    let final_verified = verify_release_candidate(
        client,
        source_id,
        &ReleaseCandidateVerifyInput {
            generation_id: built.generation_id,
        },
        pack_root,
        io_buffer_bytes,
    )
    .await?;
    ensure!(
        final_verified == verified,
        "ordinary ingest persisted candidate verification drifted"
    );
    let final_observation = observe_active_watermark(
        client,
        source_id,
        &source_type,
        Path::new(&source_path),
        built.generation_id,
        &final_verified.adapter_profile_id,
    )
    .await?;
    ensure!(
        final_observation.source_watermark_sha256 == built.source_watermark_sha256
            && final_observation.adapter_profile_id == final_verified.adapter_profile_id,
        "ordinary ingest source advanced before activation"
    );
    ensure!(
        pack_free_bytes(pack_root)? >= MINIMUM_PACK_FREE_BYTES,
        "storage-v2 pack reserve is insufficient after ordinary ingest"
    );
    let proof_sha256 = hex::encode(Sha256::digest(serde_json::to_vec(&final_verified)?));
    let receipt: Value = client
        .query_one(
            "SELECT storage_v2_activate_regular_ingest($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            &[
                &manifest_sha256,
                &source_id,
                &built.generation_id,
                &old_id,
                &source_type,
                &source_path,
                &built.source_watermark_sha256,
                &final_verified.verification_manifest_sha256,
                &proof_sha256,
            ],
        )
        .await?
        .get(0);
    if receipt["status"].as_str() != Some("ACTIVE_INGEST_COMMITTED")
        || receipt["generation_id"].as_i64() != Some(built.generation_id)
    {
        bail!("ordinary ingest activation receipt differs");
    }
    let active: Value = client
        .query_one(
            "SELECT storage_v2_active_source_state($1,$2,FALSE)",
            &[&manifest_sha256, &source_id],
        )
        .await?
        .get(0);
    ensure!(
        active["active_generation_id"].as_i64() == Some(built.generation_id),
        "ordinary ingest active source readback differs"
    );
    let changed_item_count = u64::try_from(
        client
            .query_one(
                "SELECT COUNT(DISTINCT source_item_id) FROM generation_item_version \
          WHERE source_id=$1 AND (valid_from_seq=$2 OR valid_to_seq=$2)",
                &[&source_id, &built.generation_seq],
            )
            .await?
            .get::<_, i64>(0),
    )?;
    Ok(ActiveIngestResult {
        status: "ACTIVE_INGEST_COMMITTED",
        sync_mode: if source_type == "managed_append" {
            "managed_append_verified_prefix"
        } else {
            "full_source_observation"
        },
        source_id,
        generation_id: built.generation_id,
        generation_seq: built.generation_seq,
        item_count: built.item_count,
        changed_item_count,
        source_io: built.telemetry["source_io"].clone(),
        telemetry: built.telemetry,
        receipt: Some(receipt),
    })
}
