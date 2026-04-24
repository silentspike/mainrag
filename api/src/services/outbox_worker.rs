//! Outbox Worker: Async Qdrant synchronization via transactional outbox pattern
//!
//! Polls indexing_outbox table and processes pending upsert/delete operations.
//! Uses SKIP LOCKED pattern for concurrent safety.

use crate::db::DEFAULT_USER_ID;
use crate::services::qdrant::{Point, QdrantClient};
use deadpool_postgres::Pool;
use std::sync::Arc;
use tokio::time::{interval, Duration};
use tracing::{debug, error, info, warn};
use uuid::Uuid;

/// Outbox Worker for async Qdrant synchronization
pub struct OutboxWorker {
    db: Pool,
    qdrant: Arc<QdrantClient>,
    batch_size: i32,
    poll_interval: Duration,
    // Configurable retention for cleanup
    done_retention_hours: i64,
    failed_retention_days: i64,
}

impl OutboxWorker {
    pub fn new(db: Pool, qdrant: Arc<QdrantClient>) -> Self {
        Self {
            db,
            qdrant,
            batch_size: 100,
            poll_interval: Duration::from_millis(500),
            // Configurable via env (fallback to defaults)
            done_retention_hours: std::env::var("OUTBOX_DONE_RETENTION_HOURS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(24),
            failed_retention_days: std::env::var("OUTBOX_FAILED_RETENTION_DAYS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(7),
        }
    }

    /// Run the worker (blocking loop)
    pub async fn run(&self) {
        info!(
            "Outbox worker started (batch_size: {}, poll_interval: {:?})",
            self.batch_size, self.poll_interval
        );

        let mut ticker = interval(self.poll_interval);

        loop {
            ticker.tick().await;

            if let Err(e) = self.process_batch().await {
                error!("Outbox worker error: {}", e);
            }
        }
    }

    /// Process one batch of pending outbox entries
    async fn process_batch(&self) -> anyhow::Result<()> {
        let client = self.db.get().await?;

        // Set RLS context: claim_outbox_batch JOINs sources (RLS-enabled)
        client
            .execute(
                "SELECT set_config('app.user_id', $1::text, false)",
                &[&DEFAULT_USER_ID.to_string()],
            )
            .await?;
        client
            .execute("SELECT set_config('app.is_admin', 'true', false)", &[])
            .await?;

        // Reaper: Reset stuck 'processing' entries after 5 minutes
        // Uses processing_started_at (not created_at) for accurate timeout detection
        let stale_reset = client
            .execute(
                "UPDATE indexing_outbox
             SET status = 'pending',
                 retry_count = retry_count + 1,
                 processing_started_at = NULL,
                 error_message = 'Reaper: Timeout after 5 minutes in processing'
             WHERE status = 'processing'
               AND processing_started_at < NOW() - INTERVAL '5 minutes'
               AND retry_count < 3",
                &[],
            )
            .await?;

        if stale_reset > 0 {
            warn!("Reaper reset {} stale 'processing' entries", stale_reset);
        }

        // Claim batch with SKIP LOCKED - returns vector from chunk_embeddings JOIN
        let rows = client
            .query("SELECT * FROM claim_outbox_batch($1)", &[&self.batch_size])
            .await?;

        if rows.is_empty() {
            return Ok(());
        }

        debug!("Processing {} outbox entries", rows.len());

        // Collect Points for batch upsert and IDs for batch delete
        // IMPORTANT: Separate tracking for correct error isolation
        let mut upsert_points: Vec<Point> = Vec::new();
        let mut delete_chunk_ids: Vec<u64> = Vec::new();
        let mut upsert_outbox_ids: Vec<i64> = Vec::new(); // Track upserts separately
        let mut delete_outbox_ids: Vec<i64> = Vec::new(); // Track deletes separately
        let mut failed_ids: Vec<(i64, String)> = Vec::new();

        for row in &rows {
            let outbox_id: i64 = row.get("outbox_id");
            let action: String = row.get("action");
            let chunk_id: i64 = row.get("chunk_id");
            let file_id: Option<i64> = row.get("file_id");
            let source_id: Option<i64> = row.get("source_id");
            // K4: user_id from sources JOIN for Qdrant tenant isolation
            let user_id: Option<Uuid> = row.get("user_id");
            // Enriched metadata for Qdrant-side filtering
            let chunk_type: Option<String> = row.get("chunk_type");
            let language: Option<String> = row.get("language");

            match action.as_str() {
                "upsert" => {
                    // Vector comes from JOIN with chunk_embeddings
                    let vector_opt: Option<pgvector::Vector> = row.get("vector");

                    if let Some(vec) = vector_opt {
                        let payload = serde_json::json!({
                            "chunk_id": chunk_id,
                            "file_id": file_id,
                            "source_id": source_id,
                            "user_id": user_id.map(|u| u.to_string()),
                            "chunk_type": chunk_type,
                            "language": language,
                        });

                        upsert_points.push(Point {
                            id: chunk_id as u64,
                            vector: vec.to_vec(),
                            payload,
                        });
                        upsert_outbox_ids.push(outbox_id); // Track in upsert list
                    } else {
                        warn!(
                            "No embedding for chunk {} (outbox {}), marking failed",
                            chunk_id, outbox_id
                        );
                        failed_ids.push((outbox_id, "No embedding found".to_string()));
                    }
                }
                "delete" => {
                    delete_chunk_ids.push(chunk_id as u64);
                    delete_outbox_ids.push(outbox_id); // Track in delete list
                }
                _ => {
                    warn!("Unknown action '{}' for outbox {}", action, outbox_id);
                    failed_ids.push((outbox_id, format!("Unknown action: {}", action)));
                }
            }
        }

        // Track successful operations for final status update
        let mut upsert_success = false;
        let mut delete_success = false;

        // Batch upsert to Qdrant
        if !upsert_points.is_empty() {
            let count = upsert_points.len();
            match self.qdrant.upsert_chunks(upsert_points).await {
                Ok(_) => {
                    info!("Upserted {} chunks to Qdrant", count);
                    upsert_success = true;
                }
                Err(e) => {
                    error!("Qdrant upsert failed: {}", e);
                    // Sprint 3.4: Batch UPDATE instead of per-ID loop
                    if let Err(update_err) = client.execute(
                        "UPDATE indexing_outbox SET status = 'failed', error_message = $2, retry_count = retry_count + 1, processing_started_at = NULL WHERE id = ANY($1::bigint[])",
                        &[&upsert_outbox_ids, &e.to_string()]
                    ).await {
                        error!("Failed to batch-mark upsert outbox entries as failed: {}", update_err);
                    }
                    // Continue to process deletes even if upserts failed
                }
            }
        } else {
            upsert_success = true; // No upserts to process = success
        }

        // Batch delete from Qdrant
        if !delete_chunk_ids.is_empty() {
            let count = delete_chunk_ids.len();
            match self.qdrant.delete_chunks(delete_chunk_ids).await {
                Ok(_) => {
                    info!("Deleted {} chunks from Qdrant", count);
                    delete_success = true;
                }
                Err(e) => {
                    error!("Qdrant delete failed: {}", e);
                    // Sprint 3.4: Batch UPDATE instead of per-ID loop
                    if let Err(update_err) = client.execute(
                        "UPDATE indexing_outbox SET status = 'failed', error_message = $2, retry_count = retry_count + 1, processing_started_at = NULL WHERE id = ANY($1::bigint[])",
                        &[&delete_outbox_ids, &e.to_string()]
                    ).await {
                        error!("Failed to batch-mark delete outbox entries as failed: {}", update_err);
                    }
                }
            }
        } else {
            delete_success = true; // No deletes to process = success
        }

        // Sprint 3.4: Batch UPDATE for success/failure instead of per-ID loops

        // Mark successful upsert entries as done
        if upsert_success && !upsert_outbox_ids.is_empty() {
            if let Err(e) = client.execute(
                "UPDATE indexing_outbox SET status = 'done', processed_at = NOW(), processing_started_at = NULL WHERE id = ANY($1::bigint[])",
                &[&upsert_outbox_ids]
            ).await {
                error!("Failed to batch-mark upsert outbox entries as done: {}", e);
            }
        }

        // Mark successful delete entries as done
        if delete_success && !delete_outbox_ids.is_empty() {
            if let Err(e) = client.execute(
                "UPDATE indexing_outbox SET status = 'done', processed_at = NOW(), processing_started_at = NULL WHERE id = ANY($1::bigint[])",
                &[&delete_outbox_ids]
            ).await {
                error!("Failed to batch-mark delete outbox entries as done: {}", e);
            }
        }

        // Mark pre-validation failed entries (each may have different error messages)
        // Group by error_message for batch updates where possible
        if !failed_ids.is_empty() {
            let mut by_error: std::collections::HashMap<&str, Vec<i64>> =
                std::collections::HashMap::new();
            for (id, msg) in &failed_ids {
                by_error.entry(msg.as_str()).or_default().push(*id);
            }
            for (error_msg, ids) in &by_error {
                if let Err(e) = client.execute(
                    "UPDATE indexing_outbox SET status = 'failed', error_message = $2, retry_count = retry_count + 1, processing_started_at = NULL WHERE id = ANY($1::bigint[])",
                    &[ids, &error_msg.to_string()]
                ).await {
                    error!("Failed to batch-mark outbox entries as failed: {}", e);
                }
            }
        }

        let total_processed = upsert_outbox_ids.len() + delete_outbox_ids.len();
        if total_processed > 0 || !failed_ids.is_empty() {
            info!(
                "Outbox batch complete: {} upserts, {} deletes, {} failed",
                upsert_outbox_ids.len(),
                delete_outbox_ids.len(),
                failed_ids.len()
            );
        }

        Ok(())
    }

    /// Purge old entries to prevent unbounded growth
    /// - 'done' entries: delete after `done_retention_hours` (default 24h)
    /// - 'failed' entries with retry_count >= 3: delete after `failed_retention_days` (default 7d)
    async fn purge_old_entries(&self) -> anyhow::Result<()> {
        let client = self.db.get().await?;

        // Purge 'done' older than retention
        // NOTE: Use make_interval() with integer param (more robust than string concat)
        // CRITICAL: processed_at IS NOT NULL for 'done' entries
        let done_deleted = client
            .execute(
                "DELETE FROM indexing_outbox
             WHERE status = 'done'
               AND processed_at IS NOT NULL
               AND processed_at < NOW() - make_interval(hours => $1)",
                &[&self.done_retention_hours],
            )
            .await?;

        if done_deleted > 0 {
            info!(
                "Purged {} 'done' entries (>{}h)",
                done_deleted, self.done_retention_hours
            );
            metrics::counter!("outbox_entries_purged", "status" => "done").increment(done_deleted);
        }

        // Purge 'failed' (retry_count >= 3) older than retention
        // CRITICAL: Use created_at, not processed_at (can be NULL for failed!)
        let failed_deleted = client
            .execute(
                "DELETE FROM indexing_outbox
             WHERE status = 'failed'
               AND retry_count >= 3
               AND created_at < NOW() - make_interval(days => $1)",
                &[&self.failed_retention_days],
            )
            .await?;

        if failed_deleted > 0 {
            warn!(
                "Purged {} 'failed' entries (>{}d)",
                failed_deleted, self.failed_retention_days
            );
            metrics::counter!("outbox_entries_purged", "status" => "failed")
                .increment(failed_deleted);
        }

        Ok(())
    }

    /// Update outbox queue metrics (pending/failed counts)
    /// Called from purge task (1h interval) - NOT in request path!
    async fn update_outbox_metrics(&self) -> anyhow::Result<()> {
        let client = self.db.get().await?;

        // Single query with FILTER (efficient, single scan)
        let metrics_row = client
            .query_one(
                "SELECT
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed
             FROM indexing_outbox",
                &[],
            )
            .await?;

        let pending: i64 = metrics_row.get("pending");
        let failed: i64 = metrics_row.get("failed");

        metrics::gauge!("outbox_pending_entries").set(pending as f64);
        metrics::gauge!("outbox_failed_entries").set(failed as f64);

        debug!(
            "Outbox metrics updated: pending={}, failed={}",
            pending, failed
        );

        Ok(())
    }

    /// Run the purge task (blocking loop, 1h interval)
    /// Spawned separately from the main worker
    pub async fn run_purge_task(&self) {
        info!(
            "Outbox purge task started (done_retention: {}h, failed_retention: {}d)",
            self.done_retention_hours, self.failed_retention_days
        );

        let mut ticker = interval(Duration::from_secs(3600)); // 1h

        loop {
            ticker.tick().await;

            // Run purge
            if let Err(e) = self.purge_old_entries().await {
                error!("Outbox purge error: {}", e);
            }

            // Update metrics after purge (same interval = 1h)
            if let Err(e) = self.update_outbox_metrics().await {
                warn!("Outbox metrics update error: {}", e);
            }
        }
    }
}
