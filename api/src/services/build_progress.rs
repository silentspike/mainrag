//! Private attempt-bound progress, independent of the uncommitted build rows.
//! Counts describe staged work, never a committed or qualified candidate.

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock, Weak};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use uuid::Uuid;

use super::generation_ingest::ShadowIngestMeasurements;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BuildFailure {
    pub category: String,
    pub sqlstate: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub operation: Option<String>,
}

impl BuildFailure {
    fn from_error(error: &anyhow::Error) -> Self {
        let sqlstate = error.chain().find_map(|cause| {
            cause
                .downcast_ref::<tokio_postgres::Error>()
                .and_then(|error| error.code())
                .map(|code| code.code().to_owned())
        });
        let operation = error.chain().find_map(|cause| {
            cause
                .downcast_ref::<tokio_postgres::Error>()
                .and_then(|error| error.as_db_error())
                .and_then(|error| error.where_())
                .and_then(database_operation)
                .map(str::to_owned)
        });
        let category = match sqlstate.as_deref() {
            Some("53100" | "53200" | "53300" | "53400") => "database_resource_exhausted",
            Some("54000" | "54001" | "54011") => "database_limit_exceeded",
            Some("57014") => "database_query_cancelled",
            Some("42501") => "database_access_denied",
            Some(code) if code.starts_with("23") => "database_integrity_failure",
            Some(_) => "database_failure",
            None => "build_failure",
        };
        Self {
            category: category.into(),
            sqlstate,
            operation,
        }
    }
}

// Persist only a known public operation name, never raw SQL/source context.
fn database_operation(context: &str) -> Option<&'static str> {
    [
        "storage_v2_put_search_document",
        "storage_v2_put_lexical_segments_located",
        "storage_v2_stage_shadow_item",
        "storage_v2_seal_ingest_run",
    ]
    .into_iter()
    .find(|operation| context.contains(&format!("PL/pgSQL function {operation}(")))
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BuildProgress {
    pub schema_version: String,
    pub attempt_id: Uuid,
    pub server_instance_id: Uuid,
    pub source_id: i64,
    pub commit_sha: String,
    pub pid: u32,
    pub phase: String,
    pub status: String,
    pub staged_items: usize,
    pub planned_items: Option<usize>,
    /// Scanned source candidates, including later exclusions; never staged/committed.
    #[serde(default)]
    pub observed_items: usize,
    #[serde(default)]
    pub observed_bytes: u64,
    /// Durable rows observed before this attempt, not additive to committed_items.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retained_committed_items: Option<usize>,
    pub run_id: Option<i64>,
    pub generation_id: Option<i64>,
    pub db_staging_round_trips: u64,
    pub db_staging_ms: f64,
    #[serde(default)]
    pub staging_sql_await_ms: f64,
    #[serde(default)]
    pub lexical_preparation_ms: f64,
    pub elapsed_seconds: f64,
    /// Last persisted work observation; reading a clock does not refresh work.
    pub observed_at_unix_ms: u128,
    /// Read time of the exact live recorder's monotonic clock, never a worker heartbeat.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub live_elapsed_observed_at_unix_ms: Option<u128>,
    pub transaction_committed: bool,
    #[serde(default)]
    pub committed_items: usize,
    #[serde(default)]
    pub failure: Option<BuildFailure>,
}

struct Inner {
    value: BuildProgress,
    persisted_at: Instant,
    persisted_items: usize,
}

pub struct BuildProgressRecorder {
    path: PathBuf,
    clock: Arc<AttemptClock>,
    inner: Mutex<Inner>,
}

struct AttemptClock {
    started: Instant,
    source_id: i64,
    commit_sha: String,
    attempt_id: Uuid,
    server_instance_id: Uuid,
    active: AtomicBool,
}

// A file from another server/boot has no live clock. Weak entries cannot keep
// abandoned attempts alive or infer that an unresolved writer has stopped.
static LIVE_CLOCKS: OnceLock<Mutex<HashMap<PathBuf, Weak<AttemptClock>>>> = OnceLock::new();

fn live_clocks() -> &'static Mutex<HashMap<PathBuf, Weak<AttemptClock>>> {
    LIVE_CLOCKS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn directory(root: &Path, create: bool) -> Result<PathBuf> {
    let directory = root.join(".build-progress");
    if create {
        // A first candidate may precede the content store's pack-root creation.
        fs::create_dir_all(root)?;
        match fs::create_dir(&directory) {
            Ok(()) => fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))?,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
            Err(error) => return Err(error.into()),
        }
    }
    let metadata = fs::symlink_metadata(&directory)?;
    if !metadata.is_dir() || metadata.permissions().mode() & 0o077 != 0 {
        bail!("build progress directory must be private and regular");
    }
    Ok(directory)
}

impl BuildProgressRecorder {
    pub fn create(
        root: &Path,
        source_id: i64,
        commit: &str,
        instance: Uuid,
        attempt: Uuid,
    ) -> Result<Self> {
        if source_id <= 0
            || commit.len() != 40
            || !commit
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        {
            bail!("exact build progress identity required");
        }
        let path = directory(root, true)?.join(format!("{attempt}.json"));
        let started = Instant::now();
        let value = BuildProgress {
            schema_version: "mainrag.storage-v2.build-progress.v1".into(),
            attempt_id: attempt,
            server_instance_id: instance,
            source_id,
            commit_sha: commit.into(),
            pid: std::process::id(),
            phase: "source_observation".into(),
            status: "running".into(),
            staged_items: 0,
            planned_items: None,
            observed_items: 0,
            observed_bytes: 0,
            retained_committed_items: None,
            run_id: None,
            generation_id: None,
            db_staging_round_trips: 0,
            db_staging_ms: 0.0,
            staging_sql_await_ms: 0.0,
            lexical_preparation_ms: 0.0,
            elapsed_seconds: 0.0,
            observed_at_unix_ms: SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis(),
            live_elapsed_observed_at_unix_ms: None,
            transaction_committed: false,
            committed_items: 0,
            failure: None,
        };
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&path)
            .context("progress attempt already exists or cannot be created; reconcile it")?;
        serde_json::to_writer(&mut output, &value)?;
        output.write_all(b"\n")?;
        output.sync_all()?;
        File::open(path.parent().context("progress parent missing")?)?.sync_all()?;
        let clock = Arc::new(AttemptClock {
            started,
            source_id,
            commit_sha: commit.into(),
            attempt_id: attempt,
            server_instance_id: instance,
            active: AtomicBool::new(true),
        });
        live_clocks()
            .lock()
            .map_err(|_| anyhow::anyhow!("live progress clock lock poisoned"))?
            .insert(path.clone(), Arc::downgrade(&clock));
        Ok(Self {
            path,
            clock,
            inner: Mutex::new(Inner {
                value,
                persisted_at: Instant::now(),
                persisted_items: 0,
            }),
        })
    }

    fn persist(&self, inner: &mut Inner) -> Result<()> {
        inner.value.elapsed_seconds = self.clock.started.elapsed().as_secs_f64();
        inner.value.observed_at_unix_ms = SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis();
        let temporary = self.path.with_extension(format!("{}.tmp", Uuid::new_v4()));
        let result = (|| -> Result<()> {
            let mut output = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&temporary)?;
            serde_json::to_writer(&mut output, &inner.value)?;
            output.write_all(b"\n")?;
            output.sync_all()?;
            fs::rename(&temporary, &self.path)?;
            File::open(self.path.parent().context("progress parent missing")?)?.sync_all()?;
            Ok(())
        })();
        if temporary.exists() {
            let _ = fs::remove_file(&temporary);
        }
        result?;
        inner.persisted_at = Instant::now();
        inner.persisted_items = inner.value.staged_items;
        Ok(())
    }

    pub fn phase(
        &self,
        phase: &str,
        planned: Option<usize>,
        identity: Option<(i64, i64)>,
    ) -> Result<()> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| anyhow::anyhow!("progress lock poisoned"))?;
        inner.value.phase = phase.into();
        if let Some(planned) = planned {
            inner.value.planned_items = Some(planned);
        }
        if let Some((run, generation)) = identity {
            inner.value.run_id = Some(run);
            inner.value.generation_id = Some(generation);
        }
        self.persist(&mut inner)
    }

    /// Call only after the caller validates the retained run's exact witness and
    /// reads its durable rows. This is a baseline, not work by the new attempt.
    pub fn observation_baseline(
        &self,
        run_id: i64,
        generation_id: i64,
        committed_items: usize,
        planned_items: Option<usize>,
    ) -> Result<()> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| anyhow::anyhow!("progress lock poisoned"))?;
        if inner.value.status != "running"
            || inner.value.phase != "source_observation"
            || run_id <= 0
            || generation_id <= 0
            || planned_items.is_some_and(|planned| committed_items > planned)
            || inner.value.run_id.is_some_and(|run| run != run_id)
            || inner
                .value
                .generation_id
                .is_some_and(|generation| generation != generation_id)
            || inner
                .value
                .retained_committed_items
                .is_some_and(|retained| retained != committed_items)
            || inner
                .value
                .planned_items
                .is_some_and(|planned| Some(planned) != planned_items)
        {
            bail!("retained observation baseline conflicts with the exact attempt");
        }
        inner.value.run_id = Some(run_id);
        inner.value.generation_id = Some(generation_id);
        inner.value.retained_committed_items = Some(committed_items);
        inner.value.planned_items = planned_items;
        self.persist(&mut inner)
    }

    /// Actual scanned candidates, independent of the accepted plan/stage/commit counts.
    pub fn observation_progress(&self, observed_items: usize, observed_bytes: u64) -> Result<()> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| anyhow::anyhow!("progress lock poisoned"))?;
        if inner.value.status != "running"
            || inner.value.phase != "source_observation"
            || observed_items < inner.value.observed_items
            || observed_bytes < inner.value.observed_bytes
        {
            bail!("source observation counters reverse or no longer belong to running observation");
        }
        let first = inner.value.observed_items == 0 && observed_items > 0;
        inner.value.observed_items = observed_items;
        inner.value.observed_bytes = observed_bytes;
        if first || inner.persisted_at.elapsed() >= Duration::from_secs(30) {
            self.persist(&mut inner)?;
        }
        Ok(())
    }

    pub fn advance(
        &self,
        staged_items: usize,
        measurements: &ShadowIngestMeasurements,
        force: bool,
    ) -> Result<()> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| anyhow::anyhow!("progress lock poisoned"))?;
        if staged_items < inner.value.staged_items
            || inner
                .value
                .planned_items
                .is_none_or(|planned| staged_items > planned)
        {
            bail!("build item progress exceeds or reverses the observed plan");
        }
        inner.value.staged_items = staged_items;
        inner.value.db_staging_round_trips = measurements.db_staging_round_trips;
        inner.value.db_staging_ms = measurements.database_staging_ms();
        inner.value.staging_sql_await_ms = measurements.staging_sql_await_ms();
        inner.value.lexical_preparation_ms = measurements.lexical_preparation_ms();
        if force
            || staged_items.saturating_sub(inner.persisted_items) >= 32
            || inner.persisted_at.elapsed() >= Duration::from_secs(30)
        {
            self.persist(&mut inner)?;
        }
        Ok(())
    }

    pub fn checkpoint(&self, committed_items: usize) -> Result<()> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| anyhow::anyhow!("progress lock poisoned"))?;
        if committed_items < inner.value.committed_items
            || committed_items > inner.value.staged_items
        {
            bail!("committed checkpoint count exceeds or reverses staged work");
        }
        inner.value.committed_items = committed_items;
        self.persist(&mut inner)
    }

    /// Store only classified diagnostics; database messages can contain source text.
    pub fn record_failure(&self, error: &anyhow::Error) -> Result<()> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| anyhow::anyhow!("progress lock poisoned"))?;
        inner.value.failure = Some(BuildFailure::from_error(error));
        self.persist(&mut inner)
    }

    pub fn finish(&self, committed: bool) -> Result<()> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| anyhow::anyhow!("progress lock poisoned"))?;
        self.clock.active.store(false, Ordering::Release);
        inner.value.transaction_committed = committed;
        if committed {
            inner.value.committed_items = inner.value.staged_items;
        } else if inner.value.failure.is_none() {
            inner.value.failure = Some(BuildFailure {
                category: "build_or_commit_failure".into(),
                sqlstate: None,
                operation: None,
            });
        }
        inner.value.status = if committed {
            "committed"
        } else {
            "failed_requires_reconciliation"
        }
        .into();
        self.persist(&mut inner)
    }
}

impl Drop for BuildProgressRecorder {
    fn drop(&mut self) {
        self.clock.active.store(false, Ordering::Release);
        if let Ok(mut inner) = self.inner.lock() {
            if inner.value.status == "running" {
                inner.value.status = "interrupted_requires_reconciliation".into();
                let _ = self.persist(&mut inner);
            }
        }
        if let Ok(mut clocks) = live_clocks().lock() {
            if clocks
                .get(&self.path)
                .is_some_and(|clock| Weak::ptr_eq(clock, &Arc::downgrade(&self.clock)))
            {
                clocks.remove(&self.path);
            }
        }
    }
}

pub fn read(root: &Path, source_id: i64, commit: &str, attempt: Uuid) -> Result<BuildProgress> {
    let path = directory(root, false)?.join(format!("{attempt}.json"));
    let input = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&path)?;
    let metadata = input.metadata()?;
    if !metadata.is_file() || metadata.permissions().mode() & 0o077 != 0 || metadata.len() > 16384 {
        bail!("private bounded progress record required");
    }
    let mut value: BuildProgress = serde_json::from_reader(input)?;
    if value.schema_version != "mainrag.storage-v2.build-progress.v1"
        || value.source_id != source_id
        || value.commit_sha != commit
        || value.attempt_id != attempt
    {
        bail!("build progress identity differs");
    }
    // Refresh only an exact running recorder in THIS process. Terminal records,
    // legacy files, failed work and records from another server remain frozen.
    if value.status == "running" && !value.transaction_committed && value.failure.is_none() {
        let clock = live_clocks()
            .lock()
            .map_err(|_| anyhow::anyhow!("live progress clock lock poisoned"))?
            .get(&path)
            .and_then(Weak::upgrade);
        if let Some(clock) = clock.filter(|clock| {
            clock.active.load(Ordering::Acquire)
                && clock.source_id == value.source_id
                && clock.commit_sha == value.commit_sha
                && clock.attempt_id == value.attempt_id
                && clock.server_instance_id == value.server_instance_id
                && value.pid == std::process::id()
        }) {
            value.elapsed_seconds = value
                .elapsed_seconds
                .max(clock.started.elapsed().as_secs_f64());
            value.live_elapsed_observed_at_unix_ms =
                Some(SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis());
        }
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn live_elapsed_read_preserves_persisted_work_and_terminal_records() -> Result<()> {
        for committed in [false, true] {
            let temporary = tempfile::tempdir()?;
            let root = temporary.path();
            let attempt = Uuid::new_v4();
            let commit = "a".repeat(40);
            let progress =
                BuildProgressRecorder::create(root, 7, &commit, Uuid::new_v4(), attempt)?;
            let stored = fs::read(&progress.path)?;
            let original: BuildProgress = serde_json::from_slice(&stored)?;
            let live = read(root, 7, &commit, attempt)?;
            assert!(live.elapsed_seconds > original.elapsed_seconds);
            assert!(live.live_elapsed_observed_at_unix_ms.is_some());
            assert_eq!(live.observed_at_unix_ms, original.observed_at_unix_ms);
            assert_eq!(live.phase, "source_observation");
            assert_eq!(live.staged_items, 0);
            assert_eq!(live.committed_items, 0);
            assert_eq!(live.planned_items, None);
            assert!(!live.transaction_committed);
            assert_eq!(fs::read(&progress.path)?, stored);
            progress.finish(committed)?;
            let finished = fs::read(&progress.path)?;
            let terminal = read(root, 7, &commit, attempt)?;
            assert!(terminal.live_elapsed_observed_at_unix_ms.is_none());
            let reopened = read(root, 7, &commit, attempt)?;
            assert_eq!(terminal.elapsed_seconds, reopened.elapsed_seconds);
            assert_eq!(terminal.observed_at_unix_ms, reopened.observed_at_unix_ms);
            assert_eq!(fs::read(&progress.path)?, finished);
        }
        Ok(())
    }

    #[test]
    fn other_server_and_legacy_records_do_not_borrow_a_live_clock() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let root = temporary.path();
        let attempt = Uuid::new_v4();
        let commit = "a".repeat(40);
        let progress = BuildProgressRecorder::create(root, 7, &commit, Uuid::new_v4(), attempt)?;
        let mut value = read(root, 7, &commit, attempt)?;
        value.server_instance_id = Uuid::new_v4();
        value.elapsed_seconds = 123.0;
        value.live_elapsed_observed_at_unix_ms = None;
        let mut legacy = serde_json::to_value(value)?;
        for name in [
            "live_elapsed_observed_at_unix_ms",
            "observed_items",
            "observed_bytes",
            "retained_committed_items",
        ] {
            legacy.as_object_mut().unwrap().remove(name);
        }
        fs::write(&progress.path, serde_json::to_vec(&legacy)?)?;
        let reopened = read(root, 7, &commit, attempt)?;
        assert_eq!(reopened.elapsed_seconds, 123.0);
        assert!(reopened.live_elapsed_observed_at_unix_ms.is_none());
        assert_eq!(reopened.observed_items, 0);
        assert_eq!(reopened.observed_bytes, 0);
        assert_eq!(reopened.retained_committed_items, None);
        Ok(())
    }

    #[test]
    fn source_observation_keeps_retained_and_new_work_separate() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let root = temporary.path();
        let attempt = Uuid::new_v4();
        let commit = "a".repeat(40);
        let progress = BuildProgressRecorder::create(root, 7, &commit, Uuid::new_v4(), attempt)?;
        progress.observation_baseline(11, 12, 64, Some(100))?;
        progress.observation_progress(8, 4096)?;
        let value = read(root, 7, &commit, attempt)?;
        assert_eq!(value.run_id, Some(11));
        assert_eq!(value.generation_id, Some(12));
        assert_eq!(value.retained_committed_items, Some(64));
        assert_eq!(value.observed_items, 8);
        assert_eq!(value.observed_bytes, 4096);
        assert_eq!(value.staged_items, 0);
        assert_eq!(value.committed_items, 0);
        assert!(!value.transaction_committed);
        assert!(progress
            .observation_baseline(11, 12, 65, Some(100))
            .is_err());
        assert!(progress
            .observation_baseline(11, 13, 64, Some(100))
            .is_err());
        assert!(progress.observation_baseline(11, 12, 64, Some(63)).is_err());
        assert!(progress.observation_progress(7, 4096).is_err());
        assert!(progress.observation_progress(9, 4095).is_err());
        // Scanned candidates can include excluded input and exceed the accepted plan.
        progress.observation_progress(101, 8192)?;
        progress.phase("content_store", Some(100), None)?;
        let complete = read(root, 7, &commit, attempt)?;
        assert_eq!(complete.observed_items, 101);
        assert_eq!(complete.staged_items, 0);
        assert_eq!(complete.committed_items, 0);
        progress.finish(false)?;
        assert!(progress.observation_progress(101, 8192).is_err());
        Ok(())
    }

    #[test]
    fn attempt_progress_survives_reopen_without_claiming_commit() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let root = temporary.path().join("initially-missing-packs");
        let attempt = Uuid::new_v4();
        let commit = "a".repeat(40);
        let progress = BuildProgressRecorder::create(&root, 7, &commit, Uuid::new_v4(), attempt)?;
        progress.phase("staging", Some(100), Some((11, 12)))?;
        let measurements = ShadowIngestMeasurements::default();
        progress.advance(32, &measurements, true)?;
        let value = read(&root, 7, &commit, attempt)?;
        assert_eq!(value.staged_items, 32);
        assert!(!value.transaction_committed);
        assert!(read(&root, 8, &commit, attempt).is_err());
        assert!(BuildProgressRecorder::create(&root, 7, &commit, Uuid::new_v4(), attempt).is_err());
        assert!(progress.advance(31, &measurements, true).is_err());
        assert!(progress.advance(101, &measurements, true).is_err());
        progress.checkpoint(16)?;
        assert!(progress.checkpoint(15).is_err());
        assert!(progress.checkpoint(33).is_err());
        progress.record_failure(&anyhow::anyhow!(
            "private-source-text-must-not-be-published"
        ))?;
        progress.finish(false)?;
        let failed = read(&root, 7, &commit, attempt)?;
        assert_eq!(failed.committed_items, 16);
        assert_eq!(failed.failure.as_ref().unwrap().category, "build_failure");
        assert!(!serde_json::to_string(&failed)?.contains("private-source-text"));
        assert_eq!(
            read(&root, 7, &commit, attempt)?.status,
            "failed_requires_reconciliation"
        );
        Ok(())
    }
    #[test]
    fn failure_operation_retains_public_identity_without_private_context() {
        let context = "SQL statement with private path and secret parameters\nPL/pgSQL function storage_v2_put_search_document(text,text,bigint,text,text[]) line 62 at SQL statement";
        assert_eq!(
            database_operation(context),
            Some("storage_v2_put_search_document")
        );
        assert_eq!(
            database_operation("SQL private_function and source contents"),
            None
        );
        let old: BuildFailure =
            serde_json::from_str(r#"{"category":"database_query_cancelled","sqlstate":"57014"}"#)
                .unwrap();
        assert!(old.operation.is_none());
        let value = serde_json::to_string(&BuildFailure {
            category: "database_query_cancelled".into(),
            sqlstate: Some("57014".into()),
            operation: database_operation(context).map(str::to_owned),
        })
        .unwrap();
        assert!(!value.contains("private") && !value.contains("secret"));
    }
}
