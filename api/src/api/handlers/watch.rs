//! Watch Mode Handlers - File system monitoring and auto-indexing

use crate::AppState;
use axum::{
    extract::{Path, State},
    http::StatusCode,
    Json,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

#[derive(Debug, Serialize, Deserialize)]
pub struct WatchStatusResponse {
    pub source_id: i64,
    pub name: String,
    pub path: String,
    pub watching: bool,
}

#[allow(dead_code)]
#[derive(Debug, Serialize, Deserialize)]
pub struct WatchToggleRequest {
    pub enabled: bool,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct AllWatchStatusResponse {
    pub watches: Vec<WatchStatusResponse>,
    pub active_count: i64,
    pub total_sources: i64,
}

/// Get watch status for all sources
pub async fn get_watch_status_all(
    State(state): State<Arc<AppState>>,
) -> Result<Json<AllWatchStatusResponse>, StatusCode> {
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let rows = txn
                    .query(
                        "SELECT id, name, path, COALESCE(watch_enabled, false) as watch_enabled
             FROM sources
             ORDER BY name ASC",
                        &[],
                    )
                    .await?;

                let watches: Vec<WatchStatusResponse> = rows
                    .iter()
                    .map(|row| WatchStatusResponse {
                        source_id: row.get(0),
                        name: row.get(1),
                        path: row.get(2),
                        watching: row.get(3),
                    })
                    .collect();

                let active_count = watches.iter().filter(|w| w.watching).count() as i64;
                let total_sources = watches.len() as i64;

                Ok(Json(AllWatchStatusResponse {
                    watches,
                    active_count,
                    total_sources,
                }))
            })
        })
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

/// Get watch status for a specific source
pub async fn get_watch_status(
    State(state): State<Arc<AppState>>,
    Path(source_id): Path<i64>,
) -> Result<Json<WatchStatusResponse>, StatusCode> {
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let row = txn
                    .query_one(
                        "SELECT id, name, path, COALESCE(watch_enabled, false) as watch_enabled
             FROM sources
             WHERE id = $1",
                        &[&source_id],
                    )
                    .await?;

                Ok(Json(WatchStatusResponse {
                    source_id: row.get(0),
                    name: row.get(1),
                    path: row.get(2),
                    watching: row.get(3),
                }))
            })
        })
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

/// Toggle watch mode for a source (flips current state)
/// No body required - this is a true toggle operation
pub async fn toggle_watch(
    State(state): State<Arc<AppState>>,
    Path(source_id): Path<i64>,
) -> Result<Json<WatchStatusResponse>, StatusCode> {
    state.rls_client.with_system(|txn| Box::pin(async move {
        let row = txn.query_opt(
            "UPDATE sources SET watch_enabled = NOT COALESCE(watch_enabled, false), updated_at = NOW()
             WHERE id = $1 AND NOT is_test
             RETURNING id, name, path, watch_enabled",
            &[&source_id],
        ).await?.ok_or(crate::error::AppError::BadRequest(
            "test sources cannot enter legacy watch sync".to_string(),
        ))?;

        let new_status: bool = row.get(3);

        if new_status {
            tracing::info!("Watch mode enabled for source {}", source_id);
        } else {
            tracing::info!("Watch mode disabled for source {}", source_id);
        }

        Ok(Json(WatchStatusResponse {
            source_id: row.get(0),
            name: row.get(1),
            path: row.get(2),
            watching: new_status,
        }))
    })).await.map_err(|error| match error {
        crate::error::AppError::BadRequest(_) => StatusCode::BAD_REQUEST,
        _ => StatusCode::INTERNAL_SERVER_ERROR,
    })
}

/// Get detailed watch statistics for monitoring
#[derive(Debug, Serialize)]
pub struct WatchStatsResponse {
    pub total_watched_sources: i64,
    pub files_monitored: i64,
    pub debounce_ms: u64,
    pub last_scan: Option<String>,
}

pub async fn get_watch_stats(
    State(state): State<Arc<AppState>>,
) -> Result<Json<WatchStatsResponse>, StatusCode> {
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let watched_sources: i64 = txn
                    .query_one(
                        "SELECT COUNT(*) FROM sources WHERE watch_enabled = true",
                        &[],
                    )
                    .await?
                    .get(0);

                let monitored_files: i64 = txn
                    .query_one(
                        "SELECT COALESCE(SUM(file_count), 0)
             FROM sources WHERE watch_enabled = true",
                        &[],
                    )
                    .await?
                    .get(0);

                let debounce_ms = std::env::var("MAINRAG_WATCH_DEBOUNCE_MS")
                    .ok()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(500);

                let last_scan = txn
                    .query_opt(
                        "SELECT MAX(last_synced) FROM sources WHERE watch_enabled = true",
                        &[],
                    )
                    .await?
                    .and_then(|row| row.get::<_, Option<chrono::DateTime<chrono::Utc>>>(0))
                    .map(|ts| ts.to_rfc3339());

                Ok(Json(WatchStatsResponse {
                    total_watched_sources: watched_sources,
                    files_monitored: monitored_files,
                    debounce_ms,
                    last_scan,
                }))
            })
        })
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}
