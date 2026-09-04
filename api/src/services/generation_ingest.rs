//! Opt-in orchestration primitives for storage-v2 shadow ingestion.
//!
//! The module is absent from default builds. Even when compiled with
//! `storage-v2-shadow-ingest`, callers must explicitly select `shadow` at
//! runtime. Nothing in the legacy `IndexService` calls this module.

use anyhow::{anyhow, bail, Context, Result};
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use uuid::Uuid;

pub const DEFAULT_BUFFER_BYTES: usize = 256 * 1024;
pub const MAX_BUFFER_BYTES: usize = 1024 * 1024;
pub const DEFAULT_WRITER_CONCURRENCY: usize = 2;
pub const MAX_WRITER_CONCURRENCY: usize = 16;
pub const DEFAULT_FULL_COMPARE_EVERY: u64 = 32;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StorageV2IngestMode {
    Disabled,
    Shadow,
}

impl StorageV2IngestMode {
    pub fn parse(value: Option<&str>) -> Result<Self> {
        match value.unwrap_or("off").trim().to_ascii_lowercase().as_str() {
            "" | "off" | "false" | "0" | "disabled" => Ok(Self::Disabled),
            "shadow" => Ok(Self::Shadow),
            value => bail!("invalid MAINRAG_STORAGE_V2_INGEST_MODE: {value}"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShadowIngestSettings {
    pub mode: StorageV2IngestMode,
    pub io_buffer_bytes: usize,
    pub writer_concurrency: usize,
    pub full_compare_every_appends: u64,
}

impl ShadowIngestSettings {
    pub fn from_env() -> Result<Self> {
        let mode = StorageV2IngestMode::parse(
            std::env::var("MAINRAG_STORAGE_V2_INGEST_MODE")
                .ok()
                .as_deref(),
        )?;
        let io_buffer_bytes = parse_bounded_usize(
            "MAINRAG_STORAGE_V2_BUFFER_BYTES",
            DEFAULT_BUFFER_BYTES,
            4096,
            MAX_BUFFER_BYTES,
        )?;
        let writer_concurrency = parse_bounded_usize(
            "MAINRAG_STORAGE_V2_WRITER_CONCURRENCY",
            DEFAULT_WRITER_CONCURRENCY,
            1,
            MAX_WRITER_CONCURRENCY,
        )?;
        let full_compare_every_appends = std::env::var("MAINRAG_STORAGE_V2_FULL_COMPARE_EVERY")
            .ok()
            .map(|value| value.parse::<u64>())
            .transpose()
            .context("MAINRAG_STORAGE_V2_FULL_COMPARE_EVERY must be an integer")?
            .unwrap_or(DEFAULT_FULL_COMPARE_EVERY);
        if !(1..=10_000).contains(&full_compare_every_appends) {
            bail!("MAINRAG_STORAGE_V2_FULL_COMPARE_EVERY must be between 1 and 10000");
        }
        Ok(Self {
            mode,
            io_buffer_bytes,
            writer_concurrency,
            full_compare_every_appends,
        })
    }

    pub fn require_shadow(&self) -> Result<()> {
        if self.mode != StorageV2IngestMode::Shadow {
            bail!("storage-v2 shadow ingest is disabled");
        }
        Ok(())
    }
}

fn parse_bounded_usize(
    name: &str,
    default: usize,
    minimum: usize,
    maximum: usize,
) -> Result<usize> {
    let value = std::env::var(name)
        .ok()
        .map(|value| value.parse::<usize>())
        .transpose()
        .with_context(|| format!("{name} must be an integer"))?
        .unwrap_or(default);
    if !(minimum..=maximum).contains(&value) {
        bail!("{name} must be between {minimum} and {maximum}");
    }
    Ok(value)
}

#[derive(Debug, Clone, Default)]
pub struct CancellationFlag(Arc<AtomicBool>);

impl CancellationFlag {
    pub fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }

    pub fn check(&self) -> Result<()> {
        if self.0.load(Ordering::Acquire) {
            bail!("shadow ingest cancelled");
        }
        Ok(())
    }
}

#[derive(Debug)]
pub struct SpoolArtifact {
    path: PathBuf,
    pub logical_bytes: u64,
    pub sha256: [u8; 32],
    pub managed_peak_buffer_bytes: usize,
}

impl SpoolArtifact {
    pub fn capture<R: Read>(
        mut reader: R,
        scratch_directory: &Path,
        buffer_bytes: usize,
        cancellation: &CancellationFlag,
    ) -> Result<Self> {
        if !(4096..=MAX_BUFFER_BYTES).contains(&buffer_bytes) {
            bail!("shadow ingest buffer must be between 4096 and {MAX_BUFFER_BYTES} bytes");
        }
        fs::create_dir_all(scratch_directory).with_context(|| {
            format!(
                "failed to create shadow scratch directory {}",
                scratch_directory.display()
            )
        })?;
        let path = scratch_directory.join(format!("{}.spool", Uuid::new_v4()));
        let mut output = File::options()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&path)
            .with_context(|| format!("failed to create shadow spool {}", path.display()))?;
        let result = (|| {
            let mut buffer = vec![0_u8; buffer_bytes];
            let mut hasher = Sha256::new();
            let mut logical_bytes = 0_u64;
            loop {
                cancellation.check()?;
                let read = reader
                    .read(&mut buffer)
                    .context("shadow source read failed")?;
                if read == 0 {
                    break;
                }
                output
                    .write_all(&buffer[..read])
                    .context("shadow spool write failed")?;
                hasher.update(&buffer[..read]);
                logical_bytes = logical_bytes
                    .checked_add(read as u64)
                    .ok_or_else(|| anyhow!("shadow artifact length overflow"))?;
            }
            output.sync_all().context("shadow spool sync failed")?;
            Ok(Self {
                path: path.clone(),
                logical_bytes,
                sha256: hasher.finalize().into(),
                managed_peak_buffer_bytes: buffer_bytes,
            })
        })();
        if result.is_err() {
            drop(output);
            let _ = fs::remove_file(&path);
        }
        result
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for SpoolArtifact {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

#[derive(Debug, Clone)]
pub struct WriterGate {
    semaphore: Arc<Semaphore>,
}

impl WriterGate {
    pub fn new(concurrency: usize) -> Result<Self> {
        if !(1..=MAX_WRITER_CONCURRENCY).contains(&concurrency) {
            bail!("writer concurrency must be between 1 and {MAX_WRITER_CONCURRENCY}");
        }
        Ok(Self {
            semaphore: Arc::new(Semaphore::new(concurrency)),
        })
    }

    pub async fn acquire(&self) -> Result<OwnedSemaphorePermit> {
        Arc::clone(&self.semaphore)
            .acquire_owned()
            .await
            .map_err(|_| anyhow!("shadow writer gate is closed"))
    }

    pub fn available_permits(&self) -> usize {
        self.semaphore.available_permits()
    }
}

pub trait ArtifactProcessor {
    type Output;

    fn process_once(&self, artifact: &Path, profile_id: &str) -> Result<Self::Output>;
}

pub fn process_spooled_once<P: ArtifactProcessor>(
    processor: &P,
    artifact: &SpoolArtifact,
    profile_id: &str,
    cancellation: &CancellationFlag,
) -> Result<P::Output> {
    cancellation.check()?;
    processor.process_once(artifact.path(), profile_id)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MembershipSnapshot {
    pub item_key: String,
    pub artifact_identity: [u8; 32],
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MembershipDelta {
    Unchanged {
        item_key: String,
    },
    Open {
        item_key: String,
        artifact_identity: [u8; 32],
    },
    Replace {
        item_key: String,
        previous_identity: [u8; 32],
        artifact_identity: [u8; 32],
    },
    Close {
        item_key: String,
        previous_identity: [u8; 32],
    },
}

pub fn plan_membership_delta(
    previous: &[MembershipSnapshot],
    desired: &[MembershipSnapshot],
) -> Result<Vec<MembershipDelta>> {
    use std::collections::BTreeMap;
    let mut previous_by_key = BTreeMap::new();
    let mut desired_by_key = BTreeMap::new();
    for item in previous {
        if previous_by_key
            .insert(item.item_key.as_str(), item.artifact_identity)
            .is_some()
        {
            bail!("duplicate previous item key: {}", item.item_key);
        }
    }
    for item in desired {
        if desired_by_key
            .insert(item.item_key.as_str(), item.artifact_identity)
            .is_some()
        {
            bail!("duplicate desired item key: {}", item.item_key);
        }
    }
    let mut deltas = Vec::with_capacity(previous.len().max(desired.len()));
    for (item_key, desired_identity) in &desired_by_key {
        match previous_by_key.get(item_key) {
            Some(previous_identity) if previous_identity == desired_identity => {
                deltas.push(MembershipDelta::Unchanged {
                    item_key: (*item_key).to_string(),
                });
            }
            Some(previous_identity) => deltas.push(MembershipDelta::Replace {
                item_key: (*item_key).to_string(),
                previous_identity: *previous_identity,
                artifact_identity: *desired_identity,
            }),
            None => deltas.push(MembershipDelta::Open {
                item_key: (*item_key).to_string(),
                artifact_identity: *desired_identity,
            }),
        }
    }
    for (item_key, previous_identity) in previous_by_key {
        if !desired_by_key.contains_key(item_key) {
            deltas.push(MembershipDelta::Close {
                item_key: item_key.to_string(),
                previous_identity,
            });
        }
    }
    Ok(deltas)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppendFrontier {
    pub prefix_bytes: u64,
    pub prefix_sha256: [u8; 32],
    pub appends_since_full: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AppendDecision {
    Accept,
    RequireFullComparison,
}

pub fn verify_append_candidate(
    frontier: &AppendFrontier,
    candidate_bytes: u64,
    observed_old_prefix_sha256: [u8; 32],
    full_comparison_matches: Option<bool>,
    full_compare_every: u64,
) -> Result<AppendDecision> {
    if candidate_bytes < frontier.prefix_bytes {
        bail!("append candidate shrank below the persisted frontier");
    }
    if observed_old_prefix_sha256 != frontier.prefix_sha256 {
        bail!("append candidate prefix does not match the persisted frontier");
    }
    let comparison_due = frontier
        .appends_since_full
        .checked_add(1)
        .ok_or_else(|| anyhow!("append comparison counter overflow"))?
        >= full_compare_every.max(1);
    match (comparison_due, full_comparison_matches) {
        (_, Some(false)) => bail!("scheduled full comparison detected source drift"),
        (true, None) => Ok(AppendDecision::RequireFullComparison),
        _ => Ok(AppendDecision::Accept),
    }
}

#[derive(Debug, Default)]
pub struct ShadowIngestMetrics {
    bytes_read: AtomicU64,
    reused_content: AtomicU64,
    parser_work: AtomicU64,
    generated_views: AtomicU64,
    interval_changes: AtomicU64,
    errors: AtomicU64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ShadowIngestMetricSnapshot {
    pub bytes_read: u64,
    pub reused_content: u64,
    pub parser_work: u64,
    pub generated_views: u64,
    pub interval_changes: u64,
    pub errors: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ShadowIngestStage {
    ReadAndHash,
    ContentStore,
    StructuralProjection,
    Analysis,
    DatabaseStage,
    MembershipDelta,
    Seal,
}

impl ShadowIngestStage {
    const ALL: [Self; 7] = [
        Self::ReadAndHash,
        Self::ContentStore,
        Self::StructuralProjection,
        Self::Analysis,
        Self::DatabaseStage,
        Self::MembershipDelta,
        Self::Seal,
    ];

    pub const fn telemetry_key(self) -> &'static str {
        match self {
            Self::ReadAndHash => "lesen_hashen_ms",
            Self::ContentStore => "content_store_ms",
            Self::StructuralProjection => "strukturprojektion_ms",
            Self::Analysis => "analyse_ms",
            Self::DatabaseStage => "db_staging_ms",
            Self::MembershipDelta => "intervall_delta_ms",
            Self::Seal => "sealing_ms",
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ShadowIngestMeasurements {
    stage_durations: std::collections::HashMap<ShadowIngestStage, Duration>,
    total_duration: Duration,
    pub input_bytes: u64,
    pub unique_bytes: u64,
    pub stored_bytes: u64,
    pub reused_bodies: u64,
    pub reused_nodes: u64,
    pub reused_views: u64,
    pub reused_analysis: u64,
    pub reused_generation: u64,
    pub parser_passes: u64,
    pub analysis_retries: u64,
    pub artifacts_created: u64,
    pub occurrences_created: u64,
    pub intervals_opened: u64,
    pub intervals_closed: u64,
    pub errors: u64,
    pub io_buffer_bytes: u64,
    pub peak_buffer_bytes: u64,
    pub writer_concurrency: u64,
    pub fragments_created: u64,
    pub largest_item_bytes: u64,
}

impl ShadowIngestMeasurements {
    pub fn record_stage(&mut self, stage: ShadowIngestStage, duration: Duration) {
        *self.stage_durations.entry(stage).or_default() += duration;
    }

    pub fn record_total(&mut self, duration: Duration) {
        self.total_duration = duration;
    }

    pub fn measure<T, F>(&mut self, stage: ShadowIngestStage, work: F) -> Result<T>
    where
        F: FnOnce() -> Result<T>,
    {
        let started = Instant::now();
        let result = work();
        self.record_stage(stage, started.elapsed());
        if result.is_err() {
            self.errors = self.errors.saturating_add(1);
        }
        result
    }

    pub async fn measure_async<T, F>(&mut self, stage: ShadowIngestStage, work: F) -> Result<T>
    where
        F: std::future::Future<Output = Result<T>>,
    {
        let started = Instant::now();
        let result = work.await;
        self.record_stage(stage, started.elapsed());
        if result.is_err() {
            self.errors = self.errors.saturating_add(1);
        }
        result
    }

    pub fn stage_duration(&self, stage: ShadowIngestStage) -> Duration {
        self.stage_durations
            .get(&stage)
            .copied()
            .unwrap_or_default()
    }

    pub fn to_telemetry_json(&self) -> serde_json::Value {
        let mut phases = serde_json::Map::new();
        for stage in ShadowIngestStage::ALL {
            phases.insert(
                stage.telemetry_key().to_string(),
                serde_json::Value::from(milliseconds(self.stage_duration(stage))),
            );
        }
        let total_ms = if self.total_duration.is_zero() {
            self.stage_durations.values().copied().sum()
        } else {
            self.total_duration
        };
        serde_json::json!({
            "ablauf": {
                "latenz_ms": milliseconds(total_ms),
                "eingang_bytes": self.input_bytes,
                "unique_bytes": self.unique_bytes,
                "stored_bytes": self.stored_bytes,
                "reuse_bodies": self.reused_bodies,
                "reuse_nodes": self.reused_nodes,
                "reuse_views": self.reused_views,
                "reuse_analysis": self.reused_analysis,
                "reuse_generation": self.reused_generation,
                "parser_passes": self.parser_passes,
                "analysis_retries": self.analysis_retries,
                "artifacts_created": self.artifacts_created,
                "occurrences_created": self.occurrences_created,
                "intervals_opened": self.intervals_opened,
                "intervals_closed": self.intervals_closed,
                "errors": self.errors,
                "io_buffer_bytes": self.io_buffer_bytes,
                "peak_buffer_bytes": self.peak_buffer_bytes,
                "writer_concurrency": self.writer_concurrency,
                "fragments_created": self.fragments_created,
                "largest_item_bytes": self.largest_item_bytes,
            },
            "phase": phases,
        })
    }

    pub fn write_telemetry_json(&self, path: &Path) -> Result<()> {
        let parent = path
            .parent()
            .ok_or_else(|| anyhow!("telemetry output path has no parent"))?;
        fs::create_dir_all(parent).with_context(|| {
            format!("failed to create telemetry directory {}", parent.display())
        })?;
        let temporary = parent.join(format!(".{}.tmp", Uuid::new_v4()));
        let result = (|| {
            let mut output = File::options()
                .create_new(true)
                .write(true)
                .mode(0o600)
                .open(&temporary)
                .with_context(|| {
                    format!("failed to create telemetry output {}", temporary.display())
                })?;
            serde_json::to_writer(&mut output, &self.to_telemetry_json())
                .context("failed to serialize shadow telemetry")?;
            output.write_all(b"\n")?;
            output.sync_all()?;
            fs::rename(&temporary, path).with_context(|| {
                format!("failed to publish telemetry output {}", path.display())
            })?;
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }
}

fn milliseconds(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1000.0
}

impl ShadowIngestMetrics {
    pub fn record_bytes_read(&self, bytes: u64) {
        self.bytes_read.fetch_add(bytes, Ordering::Relaxed);
        metrics::counter!("storage_v2_ingest_bytes_read_total").increment(bytes);
    }

    pub fn record_reuse(&self) {
        self.reused_content.fetch_add(1, Ordering::Relaxed);
        metrics::counter!("storage_v2_ingest_reuse_total").increment(1);
    }

    pub fn record_parser_work(&self) {
        self.parser_work.fetch_add(1, Ordering::Relaxed);
        metrics::counter!("storage_v2_ingest_parser_work_total").increment(1);
    }

    pub fn record_generated_view(&self) {
        self.generated_views.fetch_add(1, Ordering::Relaxed);
        metrics::counter!("storage_v2_ingest_generated_views_total").increment(1);
    }

    pub fn record_interval_changes(&self, count: u64) {
        self.interval_changes.fetch_add(count, Ordering::Relaxed);
        metrics::counter!("storage_v2_ingest_interval_changes_total").increment(count);
    }

    pub fn record_error(&self) {
        self.errors.fetch_add(1, Ordering::Relaxed);
        metrics::counter!("storage_v2_ingest_errors_total").increment(1);
    }

    pub fn record_generation_state(&self, state: &'static str) {
        metrics::counter!("storage_v2_ingest_generation_state_total", "state" => state)
            .increment(1);
    }

    pub fn snapshot(&self) -> ShadowIngestMetricSnapshot {
        ShadowIngestMetricSnapshot {
            bytes_read: self.bytes_read.load(Ordering::Relaxed),
            reused_content: self.reused_content.load(Ordering::Relaxed),
            parser_work: self.parser_work.load(Ordering::Relaxed),
            generated_views: self.generated_views.load(Ordering::Relaxed),
            interval_changes: self.interval_changes.load(Ordering::Relaxed),
            errors: self.errors.load(Ordering::Relaxed),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::sync::atomic::AtomicUsize;

    fn identity(byte: u8) -> [u8; 32] {
        [byte; 32]
    }

    #[test]
    fn ingest_mode_is_fail_closed_and_explicit() {
        assert_eq!(
            StorageV2IngestMode::parse(None).unwrap(),
            StorageV2IngestMode::Disabled
        );
        assert_eq!(
            StorageV2IngestMode::parse(Some("shadow")).unwrap(),
            StorageV2IngestMode::Shadow
        );
        assert!(StorageV2IngestMode::parse(Some("active")).is_err());
        let settings = ShadowIngestSettings {
            mode: StorageV2IngestMode::Disabled,
            io_buffer_bytes: DEFAULT_BUFFER_BYTES,
            writer_concurrency: DEFAULT_WRITER_CONCURRENCY,
            full_compare_every_appends: DEFAULT_FULL_COMPARE_EVERY,
        };
        assert!(settings.require_shadow().is_err());
    }

    #[test]
    fn spooling_is_byte_bounded_and_cancelled_spools_are_removed() {
        let root = std::env::temp_dir().join(format!("mainrag-shadow-test-{}", Uuid::new_v4()));
        let cancellation = CancellationFlag::default();
        let bytes = vec![0x5a; 1024 * 1024 + 17];
        let artifact =
            SpoolArtifact::capture(Cursor::new(&bytes), &root, 8192, &cancellation).unwrap();
        assert_eq!(artifact.logical_bytes, bytes.len() as u64);
        assert_eq!(artifact.managed_peak_buffer_bytes, 8192);
        let expected_digest: [u8; 32] = Sha256::digest(&bytes).into();
        assert_eq!(artifact.sha256, expected_digest);
        let path = artifact.path().to_owned();
        assert!(path.exists());
        drop(artifact);
        assert!(!path.exists());

        cancellation.cancel();
        assert!(SpoolArtifact::capture(Cursor::new(&bytes), &root, 4096, &cancellation).is_err());
        assert_eq!(fs::read_dir(&root).unwrap().count(), 0);
        fs::remove_dir(&root).unwrap();
    }

    struct CountingProcessor {
        calls: AtomicUsize,
    }

    impl ArtifactProcessor for CountingProcessor {
        type Output = usize;

        fn process_once(&self, artifact: &Path, _profile_id: &str) -> Result<Self::Output> {
            self.calls.fetch_add(1, Ordering::Relaxed);
            Ok(fs::read(artifact)?.len())
        }
    }

    #[test]
    fn one_artifact_is_processed_once_per_profile() {
        let root = std::env::temp_dir().join(format!("mainrag-shadow-test-{}", Uuid::new_v4()));
        let cancellation = CancellationFlag::default();
        let artifact =
            SpoolArtifact::capture(Cursor::new(b"one pass"), &root, 4096, &cancellation).unwrap();
        let processor = CountingProcessor {
            calls: AtomicUsize::new(0),
        };
        assert_eq!(
            process_spooled_once(&processor, &artifact, "fixture-v1", &cancellation).unwrap(),
            8
        );
        assert_eq!(processor.calls.load(Ordering::Relaxed), 1);
        drop(artifact);
        fs::remove_dir(&root).unwrap();
    }

    #[test]
    fn membership_planner_emits_no_copy_for_unchanged_items() {
        let previous = vec![
            MembershipSnapshot {
                item_key: "a".into(),
                artifact_identity: identity(1),
            },
            MembershipSnapshot {
                item_key: "delete".into(),
                artifact_identity: identity(2),
            },
        ];
        let desired = vec![
            MembershipSnapshot {
                item_key: "a".into(),
                artifact_identity: identity(1),
            },
            MembershipSnapshot {
                item_key: "add".into(),
                artifact_identity: identity(3),
            },
        ];
        assert_eq!(
            plan_membership_delta(&previous, &desired).unwrap(),
            vec![
                MembershipDelta::Unchanged {
                    item_key: "a".into()
                },
                MembershipDelta::Open {
                    item_key: "add".into(),
                    artifact_identity: identity(3)
                },
                MembershipDelta::Close {
                    item_key: "delete".into(),
                    previous_identity: identity(2),
                },
            ]
        );
    }

    #[test]
    fn a_to_b_to_a_reuses_the_original_identity() {
        let a = MembershipSnapshot {
            item_key: "item".into(),
            artifact_identity: identity(1),
        };
        let b = MembershipSnapshot {
            item_key: "item".into(),
            artifact_identity: identity(2),
        };
        assert!(matches!(
            &plan_membership_delta(std::slice::from_ref(&a), std::slice::from_ref(&b)).unwrap()[0],
            MembershipDelta::Replace { .. }
        ));
        let back = plan_membership_delta(&[b], std::slice::from_ref(&a)).unwrap();
        assert!(matches!(
            &back[0],
            MembershipDelta::Replace { artifact_identity, .. } if *artifact_identity == identity(1)
        ));
    }

    #[test]
    fn append_contract_rejects_shrink_and_prefix_drift_and_requires_full_compare() {
        let frontier = AppendFrontier {
            prefix_bytes: 10,
            prefix_sha256: identity(4),
            appends_since_full: 2,
        };
        assert!(verify_append_candidate(&frontier, 9, identity(4), None, 3).is_err());
        assert!(verify_append_candidate(&frontier, 11, identity(5), None, 3).is_err());
        assert_eq!(
            verify_append_candidate(&frontier, 11, identity(4), None, 3).unwrap(),
            AppendDecision::RequireFullComparison
        );
        assert!(verify_append_candidate(&frontier, 11, identity(4), Some(false), 3).is_err());
        assert_eq!(
            verify_append_candidate(&frontier, 11, identity(4), Some(true), 3).unwrap(),
            AppendDecision::Accept
        );
    }

    #[tokio::test]
    async fn writer_gate_enforces_configured_concurrency() {
        let gate = WriterGate::new(1).unwrap();
        let permit = gate.acquire().await.unwrap();
        assert_eq!(gate.available_permits(), 0);
        drop(permit);
        assert_eq!(gate.available_permits(), 1);
        assert!(WriterGate::new(0).is_err());
    }

    #[test]
    fn metrics_keep_dimensions_separate() {
        let metrics = ShadowIngestMetrics::default();
        metrics.record_bytes_read(100);
        metrics.record_reuse();
        metrics.record_parser_work();
        metrics.record_generated_view();
        metrics.record_interval_changes(2);
        metrics.record_error();
        assert_eq!(
            metrics.snapshot(),
            ShadowIngestMetricSnapshot {
                bytes_read: 100,
                reused_content: 1,
                parser_work: 1,
                generated_views: 1,
                interval_changes: 2,
                errors: 1,
            }
        );
    }

    #[test]
    fn telemetry_json_exposes_every_optimization_stage_and_dedup_dimension() {
        let mut measurements = ShadowIngestMeasurements {
            input_bytes: 100,
            unique_bytes: 60,
            stored_bytes: 30,
            reused_bodies: 2,
            reused_nodes: 3,
            reused_views: 4,
            reused_analysis: 5,
            parser_passes: 1,
            analysis_retries: 1,
            artifacts_created: 1,
            occurrences_created: 1,
            intervals_opened: 1,
            intervals_closed: 0,
            errors: 0,
            io_buffer_bytes: 8192,
            peak_buffer_bytes: 8192,
            writer_concurrency: 2,
            fragments_created: 3,
            largest_item_bytes: 4096,
            ..ShadowIngestMeasurements::default()
        };
        for (index, stage) in ShadowIngestStage::ALL.into_iter().enumerate() {
            measurements.record_stage(stage, Duration::from_millis((index + 1) as u64));
        }
        measurements.record_total(Duration::from_millis(28));
        let json = measurements.to_telemetry_json();
        assert_eq!(json["ablauf"]["latenz_ms"], 28.0);
        assert_eq!(json["ablauf"]["eingang_bytes"], 100);
        assert_eq!(json["ablauf"]["unique_bytes"], 60);
        assert_eq!(json["ablauf"]["stored_bytes"], 30);
        assert_eq!(json["ablauf"]["analysis_retries"], 1);
        assert_eq!(json["ablauf"]["io_buffer_bytes"], 8192);
        assert_eq!(json["ablauf"]["fragments_created"], 3);
        assert_eq!(json["ablauf"]["largest_item_bytes"], 4096);
        assert_eq!(json["phase"].as_object().unwrap().len(), 7);
        assert_eq!(json["phase"]["lesen_hashen_ms"], 1.0);
        assert_eq!(json["phase"]["sealing_ms"], 7.0);
    }

    #[test]
    fn telemetry_output_is_atomic_and_compatible_with_run_script() {
        let root =
            std::env::temp_dir().join(format!("mainrag-shadow-telemetry-{}", Uuid::new_v4()));
        let path = root.join("kennzahlen.json");
        let measurements = ShadowIngestMeasurements {
            input_bytes: 42,
            ..ShadowIngestMeasurements::default()
        };
        measurements.write_telemetry_json(&path).unwrap();
        let decoded: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(decoded["ablauf"]["eingang_bytes"], 42);
        assert!(decoded.get("phase").is_some());
        fs::remove_file(path).unwrap();
        fs::remove_dir(root).unwrap();
    }
}
