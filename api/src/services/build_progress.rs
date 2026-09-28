//! Private attempt-bound progress, independent of the uncommitted build rows.
//! Counts describe staged work, never a committed or qualified candidate.

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use uuid::Uuid;

use super::generation_ingest::ShadowIngestMeasurements;

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
    pub run_id: Option<i64>,
    pub generation_id: Option<i64>,
    pub db_staging_round_trips: u64,
    pub db_staging_ms: f64,
    pub elapsed_seconds: f64,
    pub observed_at_unix_ms: u128,
    pub transaction_committed: bool,
}

struct Inner {
    value: BuildProgress,
    persisted_at: Instant,
    persisted_items: usize,
}

pub struct BuildProgressRecorder {
    path: PathBuf,
    started: Instant,
    inner: Mutex<Inner>,
}

fn directory(root: &Path) -> Result<PathBuf> {
    let directory = root.join(".build-progress");
    match fs::create_dir(&directory) {
        Ok(()) => fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))?,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error.into()),
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
        let path = directory(root)?.join(format!("{attempt}.json"));
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
            run_id: None,
            generation_id: None,
            db_staging_round_trips: 0,
            db_staging_ms: 0.0,
            elapsed_seconds: 0.0,
            observed_at_unix_ms: SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis(),
            transaction_committed: false,
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
        let now = Instant::now();
        Ok(Self {
            path,
            started: now,
            inner: Mutex::new(Inner {
                value,
                persisted_at: now,
                persisted_items: 0,
            }),
        })
    }

    fn persist(&self, inner: &mut Inner) -> Result<()> {
        inner.value.elapsed_seconds = self.started.elapsed().as_secs_f64();
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
        if force
            || staged_items.saturating_sub(inner.persisted_items) >= 32
            || inner.persisted_at.elapsed() >= Duration::from_secs(30)
        {
            self.persist(&mut inner)?;
        }
        Ok(())
    }

    pub fn finish(&self, committed: bool) -> Result<()> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| anyhow::anyhow!("progress lock poisoned"))?;
        inner.value.transaction_committed = committed;
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
        if let Ok(mut inner) = self.inner.lock() {
            if inner.value.status == "running" {
                inner.value.status = "interrupted_requires_reconciliation".into();
                let _ = self.persist(&mut inner);
            }
        }
    }
}

pub fn read(root: &Path, source_id: i64, commit: &str, attempt: Uuid) -> Result<BuildProgress> {
    let path = directory(root)?.join(format!("{attempt}.json"));
    let input = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let metadata = input.metadata()?;
    if !metadata.is_file() || metadata.permissions().mode() & 0o077 != 0 || metadata.len() > 16384 {
        bail!("private bounded progress record required");
    }
    let value: BuildProgress = serde_json::from_reader(input)?;
    if value.schema_version != "mainrag.storage-v2.build-progress.v1"
        || value.source_id != source_id
        || value.commit_sha != commit
        || value.attempt_id != attempt
    {
        bail!("build progress identity differs");
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn attempt_progress_survives_reopen_without_claiming_commit() -> Result<()> {
        let root = tempfile::tempdir()?;
        let attempt = Uuid::new_v4();
        let commit = "a".repeat(40);
        let progress =
            BuildProgressRecorder::create(root.path(), 7, &commit, Uuid::new_v4(), attempt)?;
        progress.phase("staging", Some(100), Some((11, 12)))?;
        let measurements = ShadowIngestMeasurements::default();
        progress.advance(32, &measurements, true)?;
        let value = read(root.path(), 7, &commit, attempt)?;
        assert_eq!(value.staged_items, 32);
        assert!(!value.transaction_committed);
        assert!(read(root.path(), 8, &commit, attempt).is_err());
        assert!(
            BuildProgressRecorder::create(root.path(), 7, &commit, Uuid::new_v4(), attempt)
                .is_err()
        );
        assert!(progress.advance(31, &measurements, true).is_err());
        assert!(progress.advance(101, &measurements, true).is_err());
        progress.finish(false)?;
        assert_eq!(
            read(root.path(), 7, &commit, attempt)?.status,
            "failed_requires_reconciliation"
        );
        Ok(())
    }
}
