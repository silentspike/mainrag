//! Dedicated source session with transaction-local authorization at every checkpoint.
//! Dropping the detached connection rolls back pending work and releases its
//! session lock, including when an HTTP request is cancelled.

use anyhow::{ensure, Context, Result};
use deadpool_postgres::{Client, ClientWrapper, Pool};
use serde_json::Value;
use std::ffi::OsStr;
use std::fs::{File, OpenOptions};
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use uuid::Uuid;

// Physical write settings belong to the detached writer. GIN consolidation can
// outlive a reader deadline; its memory and pending entries remain bounded.
// SET LOCAL is reapplied after every durable boundary and never reaches readers.
const DEFAULT_WRITE_SETTINGS: &str = "SET LOCAL statement_timeout='10min'; \
    SET LOCAL maintenance_work_mem='128MB'; \
    SET LOCAL gin_pending_list_limit='16MB'; \
    SET LOCAL client_min_messages='warning'";

const BULK_WRITE_SETTINGS: &str = "SET LOCAL statement_timeout='10min'; \
    SET LOCAL maintenance_work_mem='256MB'; \
    SET LOCAL gin_pending_list_limit='64MB'; \
    SET LOCAL client_min_messages='warning'";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum BuildWriteProfile {
    Default,
    Bulk,
}

impl BuildWriteProfile {
    fn parse(value: Option<&OsStr>) -> Result<Self> {
        let value = value
            .map(|value| value.to_str().context("build write profile must be UTF-8"))
            .transpose()?;
        match value {
            None | Some("default") => Ok(Self::Default),
            Some("bulk") => Ok(Self::Bulk),
            Some(_) => anyhow::bail!("build write profile must be exactly default or bulk"),
        }
    }

    fn from_env() -> Result<Self> {
        Self::parse(std::env::var_os("MAINRAG_V2_BUILD_WRITE_PROFILE").as_deref())
    }

    fn settings(self) -> &'static str {
        match self {
            Self::Default => DEFAULT_WRITE_SETTINGS,
            Self::Bulk => BULK_WRITE_SETTINGS,
        }
    }
}

pub struct BuildCheckpointSession {
    connection: ClientWrapper,
    user_id: Uuid,
    source_id: i64,
    identity: Value,
    expected_run_id: Option<i64>,
    write_profile: BuildWriteProfile,
    local_wal_budget: Option<super::local_wal_budget::LocalWalBudget>,
    _maintenance: File,
    #[cfg(test)]
    fail_after: std::sync::atomic::AtomicUsize,
}

impl BuildCheckpointSession {
    pub async fn open(pool: &Pool, user_id: Uuid, source_id: i64, root: &Path) -> Result<Self> {
        // Reject unknown/non-UTF-8 profiles before touching the pool. Freeze the
        // selection for this detached session, including every later checkpoint.
        let write_profile = BuildWriteProfile::from_env()?;
        Self::open_configured(pool, user_id, source_id, root, write_profile).await
    }

    #[cfg(test)]
    pub(crate) async fn open_with_write_profile(
        pool: &Pool,
        user_id: Uuid,
        source_id: i64,
        root: &Path,
        write_profile: BuildWriteProfile,
    ) -> Result<Self> {
        Self::open_configured(pool, user_id, source_id, root, write_profile).await
    }

    async fn open_configured(
        pool: &Pool,
        user_id: Uuid,
        source_id: i64,
        root: &Path,
        write_profile: BuildWriteProfile,
    ) -> Result<Self> {
        let local_wal_budget = super::local_wal_budget::LocalWalBudget::from_env()?;
        // A checkpointing connection never returns to the pool with session
        // state or an open transaction. ClientWrapper aborts its driver on drop.
        let connection = Client::take(pool.get().await?);
        connection.batch_execute("BEGIN").await?;
        connection
            .execute(
                "SELECT set_config('app.user_id',$1,true), \
            set_config('app.is_admin','true',true)",
                &[&user_id.to_string()],
            )
            .await?;
        let allowed: bool = connection
            .query_one(
                "SELECT storage_v2_is_admin() AND storage_v2_can_access_source($1,'write')",
                &[&source_id],
            )
            .await?
            .get(0);
        ensure!(
            allowed,
            "checkpointed source build requires administrator source access"
        );
        if local_wal_budget.is_some() {
            let available: bool = connection.query_one(
                "SELECT to_regprocedure('public.storage_v2_local_wal_ready_bytes()') IS NOT NULL", &[]
            ).await?.get(0);
            ensure!(
                available,
                "local WAL backpressure requires its controlled observation function"
            );
        }
        connection.batch_execute(write_profile.settings()).await?;
        let acquired: bool = connection
            .query_one(
                "SELECT pg_try_advisory_lock(hashtextextended( \
             'mainrag.storage-v2-ingest-source:'||$1::BIGINT::TEXT,0))",
                &[&source_id],
            )
            .await?
            .get(0);
        ensure!(acquired, "another source writer is active");
        std::fs::create_dir_all(root)?;
        let maintenance = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(root.join(".maintenance.lock"))?;
        ensure!(
            maintenance.metadata()?.is_file(),
            "regular maintenance lock required"
        );
        maintenance
            .try_lock_shared()
            .context("pack maintenance is active")?;
        let identity = Self::observe(&connection, source_id).await?;
        Ok(Self {
            connection,
            user_id,
            source_id,
            identity,
            expected_run_id: None,
            write_profile,
            local_wal_budget,
            _maintenance: maintenance,
            #[cfg(test)]
            fail_after: std::sync::atomic::AtomicUsize::new(0),
        })
    }

    pub fn expect_run(mut self, run_id: Option<i64>) -> Self {
        self.expected_run_id = run_id;
        self
    }

    pub fn expected_run_id(&self) -> Option<i64> {
        self.expected_run_id
    }

    pub fn validate_source(&self, source_id: i64) -> Result<()> {
        ensure!(
            source_id == self.source_id,
            "checkpoint session belongs to another source"
        );
        Ok(())
    }

    pub fn validate_run(&self, actual_run_id: i64) -> Result<()> {
        ensure!(
            self.expected_run_id
                .is_none_or(|expected| expected == actual_run_id),
            "resume run identity differs from the observed source and build package"
        );
        Ok(())
    }

    #[cfg(test)]
    pub fn fail_after_checkpoints(&self, count: usize) {
        self.fail_after
            .store(count, std::sync::atomic::Ordering::SeqCst);
    }

    #[cfg(test)]
    pub fn fixture_local_wal_budget(&mut self) {
        self.local_wal_budget = Some(super::local_wal_budget::LocalWalBudget::fixture_policy());
    }

    pub fn client(&self) -> &tokio_postgres::Client {
        &self.connection
    }

    async fn observe(client: &tokio_postgres::Client, source_id: i64) -> Result<Value> {
        Ok(client
            .query_one(
                "WITH pointer AS MATERIALIZED (SELECT active_generation_id \
             FROM logical_source WHERE id=$1 FOR SHARE) \
             SELECT jsonb_build_object('type',s.type,'path',s.path,'config',s.config, \
             'is_test',s.is_test,'active',(SELECT active_generation_id FROM pointer)) \
             FROM sources s WHERE s.id=$1 FOR SHARE OF s",
                &[&source_id],
            )
            .await?
            .get(0))
    }

    pub async fn checkpoint(&self) -> Result<()> {
        self.checkpoint_with_hooks(|| Ok(()), |_| Ok(())).await
    }

    pub async fn checkpoint_with_hooks<F, W>(&self, committed: F, waiting: W) -> Result<()>
    where
        F: FnOnce() -> Result<()>,
        W: FnMut(bool) -> Result<()>,
    {
        // Source rows remain share-locked until each COMMIT. Reacquire and
        // compare before allowing any writes in the following transaction.
        ensure!(
            Self::observe(self.client(), self.source_id).await? == self.identity,
            "source identity or active pointer changed before checkpoint"
        );
        self.connection.batch_execute("COMMIT").await?;
        // Report the durable boundary before waiting or a controlled interruption.
        committed()?;
        #[cfg(test)]
        {
            use std::sync::atomic::Ordering::SeqCst;
            let mut remaining = self.fail_after.load(SeqCst);
            while remaining > 0 {
                match self.fail_after.compare_exchange_weak(
                    remaining,
                    remaining - 1,
                    SeqCst,
                    SeqCst,
                ) {
                    Ok(1) => anyhow::bail!("controlled interruption after durable checkpoint"),
                    Ok(_) => break,
                    Err(actual) => remaining = actual,
                }
            }
        }
        // No transaction or transaction-scoped row/graph lock is held here.
        // The dedicated source session lock still excludes another writer.
        if let Some(budget) = &self.local_wal_budget {
            budget.wait(self.client(), waiting).await?;
        }
        self.connection.batch_execute("BEGIN").await?;
        self.connection
            .batch_execute(self.write_profile.settings())
            .await?;
        self.connection
            .execute(
                "SELECT set_config('app.user_id',$1,true), \
            set_config('app.is_admin','true',true)",
                &[&self.user_id.to_string()],
            )
            .await?;
        ensure!(
            Self::observe(self.client(), self.source_id).await? == self.identity,
            "source identity or active pointer changed after checkpoint"
        );
        let allowed: bool = self
            .connection
            .query_one(
                "SELECT storage_v2_is_admin() AND storage_v2_can_access_source($1,'write')",
                &[&self.source_id],
            )
            .await?
            .get(0);
        ensure!(allowed, "source access changed after checkpoint");
        Ok(())
    }

    pub async fn finish(&self) -> Result<()> {
        ensure!(
            Self::observe(self.client(), self.source_id).await? == self.identity,
            "source identity or active pointer changed before final commit"
        );
        self.connection.batch_execute("COMMIT").await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::ffi::OsStrExt;

    #[test]
    fn build_write_profile_accepts_only_absent_default_or_bulk() {
        assert_eq!(
            BuildWriteProfile::parse(None).unwrap(),
            BuildWriteProfile::Default
        );
        assert_eq!(
            BuildWriteProfile::parse(Some(OsStr::new("default"))).unwrap(),
            BuildWriteProfile::Default
        );
        assert_eq!(
            BuildWriteProfile::parse(Some(OsStr::new("bulk"))).unwrap(),
            BuildWriteProfile::Bulk
        );
    }

    #[test]
    fn build_write_profile_rejects_unbounded_or_ambiguous_values() {
        for value in [
            "",
            "Bulk",
            " bulk",
            "bulk ",
            "64",
            "128MB",
            "custom",
            "bulk;RESET ALL",
        ] {
            assert!(BuildWriteProfile::parse(Some(OsStr::new(value))).is_err());
        }
    }

    #[test]
    fn build_write_profile_rejects_non_utf8_without_lossy_conversion() {
        assert!(BuildWriteProfile::parse(Some(OsStr::from_bytes(b"bulk\xff"))).is_err());
    }
}
