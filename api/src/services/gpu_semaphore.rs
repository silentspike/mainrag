//! Sprint 7.3b + W6: GPU-Aware Concurrency Semaphore with Per-Agent Fairness
//!
//! Limits concurrent GPU operations to prevent VRAM OOM:
//! - Embed: max EMBED_CONCURRENCY (default 4) concurrent requests
//! - Rerank: max RERANK_CONCURRENCY (default 2) concurrent requests
//! - Qdrant: max QDRANT_CONCURRENCY (default 8) concurrent requests
//!
//! W6 Per-Agent Fairness:
//! - Each agent gets at most EMBED_PER_AGENT_CONCURRENCY (default 2) concurrent embed requests
//! - Per-agent semaphores are cached with 10min idle TTL (max 50 entries)
//! - Acquisition order: per-agent semaphore FIRST, then global (prevents starvation)

use std::sync::Arc;
use std::time::Duration;

use moka::sync::Cache;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

/// GPU-aware concurrency limits for TEI and Qdrant,
/// with per-agent fairness for embedding requests.
#[allow(dead_code)]
pub struct GpuSemaphores {
    pub embed: Arc<Semaphore>,
    pub rerank: Arc<Semaphore>,
    pub qdrant: Arc<Semaphore>,
    /// Per-agent semaphores for embed fairness (W6)
    per_agent_semaphores: Cache<String, Arc<Semaphore>>,
    /// Per-agent concurrency limit
    per_agent_limit: usize,
}

impl GpuSemaphores {
    pub fn from_env() -> Self {
        let embed_concurrency: usize = std::env::var("EMBED_CONCURRENCY")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(4);
        let rerank_concurrency: usize = std::env::var("RERANK_CONCURRENCY")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(2);
        let qdrant_concurrency: usize = std::env::var("QDRANT_CONCURRENCY")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(8);
        let per_agent_limit: usize = std::env::var("EMBED_PER_AGENT_CONCURRENCY")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(2);

        // Per-agent semaphore cache: max 50 entries, 10min idle TTL
        let per_agent_semaphores: Cache<String, Arc<Semaphore>> = Cache::builder()
            .max_capacity(50)
            .time_to_idle(Duration::from_secs(600))
            .build();

        tracing::info!(
            embed = embed_concurrency,
            rerank = rerank_concurrency,
            qdrant = qdrant_concurrency,
            per_agent = per_agent_limit,
            "GPU semaphores initialized (with per-agent fairness)"
        );

        Self {
            embed: Arc::new(Semaphore::new(embed_concurrency)),
            rerank: Arc::new(Semaphore::new(rerank_concurrency)),
            qdrant: Arc::new(Semaphore::new(qdrant_concurrency)),
            per_agent_semaphores,
            per_agent_limit,
        }
    }

    /// Acquire both a per-agent and global embed semaphore permit.
    ///
    /// Acquires per-agent FIRST (to enforce fairness), then global
    /// (to enforce total GPU concurrency). This ordering ensures that
    /// a single agent cannot starve others by monopolizing the global
    /// embed semaphore.
    ///
    /// Returns a tuple of (per_agent_permit, global_permit). Both must
    /// be held for the duration of the embedding operation and dropped
    /// when complete.
    /// M1: Returns Result instead of panicking on closed semaphore
    #[allow(dead_code)]
    pub async fn acquire_embed_with_agent(
        &self,
        agent_id: &str,
    ) -> Result<(OwnedSemaphorePermit, OwnedSemaphorePermit), String> {
        let per_agent_limit = self.per_agent_limit;

        // Get or create per-agent semaphore
        let agent_sem = self
            .per_agent_semaphores
            .get_with(agent_id.to_string(), || {
                tracing::debug!(
                    agent_id = agent_id,
                    limit = per_agent_limit,
                    "Creating per-agent embed semaphore"
                );
                Arc::new(Semaphore::new(per_agent_limit))
            });

        // Acquire per-agent permit FIRST (fairness)
        let agent_start = std::time::Instant::now();
        let agent_permit = agent_sem
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| "per-agent semaphore closed unexpectedly".to_string())?;
        let agent_wait = agent_start.elapsed();

        if agent_wait.as_millis() > 0 {
            metrics::histogram!("gpu_semaphore_agent_wait_seconds", "agent_id" => agent_id.to_string())
                .record(agent_wait.as_secs_f64());
        }

        // Then acquire global embed permit
        let global_start = std::time::Instant::now();
        let global_permit = self
            .embed
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| "global embed semaphore closed unexpectedly".to_string())?;
        let global_wait = global_start.elapsed();

        if global_wait.as_millis() > 0 {
            metrics::histogram!("gpu_semaphore_global_wait_seconds")
                .record(global_wait.as_secs_f64());
        }

        // Record total wait time
        let total_wait = agent_wait + global_wait;
        metrics::histogram!("gpu_semaphore_total_wait_seconds").record(total_wait.as_secs_f64());

        if total_wait.as_millis() > 50 {
            tracing::debug!(
                agent_id = agent_id,
                agent_wait_ms = agent_wait.as_millis() as u64,
                global_wait_ms = global_wait.as_millis() as u64,
                "Embed semaphore acquisition took >50ms"
            );
        }

        Ok((agent_permit, global_permit))
    }
}
