//! Dedicated source session with transaction-local authorization at every checkpoint.
//! Dropping the detached connection rolls back pending work and releases its
//! session lock, including when an HTTP request is cancelled.

use anyhow::{ensure, Context, Result};
use deadpool_postgres::{Client, ClientWrapper, Pool};
use serde_json::Value;
use std::fs::{File, OpenOptions};
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use uuid::Uuid;

pub struct BuildCheckpointSession {
    connection: ClientWrapper,
    user_id: Uuid,
    source_id: i64,
    identity: Value,
    expected_run_id: Option<i64>,
    local_wal_budget: Option<super::local_wal_budget::LocalWalBudget>,
    _maintenance: File,
    #[cfg(test)]
    fail_after: std::sync::atomic::AtomicUsize,
}

impl BuildCheckpointSession {
    pub async fn open(pool: &Pool, user_id: Uuid, source_id: i64, root: &Path) -> Result<Self> {
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
        // GIN pending-list maintenance measured longer than the ordinary
        // reader deadline. Keep writes bounded in this detached connection;
        // it is destroyed on drop and never returns its settings to the pool.
        connection
            .batch_execute("SET LOCAL statement_timeout='120s'")
            .await?;
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
        self.connection
            .batch_execute("BEGIN; SET LOCAL statement_timeout='120s'")
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
