use axum::{extract::State, Json};
use serde::Serialize;
use std::sync::Arc;

use crate::error::Result;
use crate::AppState;

#[derive(Serialize)]
pub struct HealthResponse {
    pub status: String,
    pub mode: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub read_path: Option<&'static str>,
    pub services: ServiceStatus,
}

#[derive(Serialize)]
pub struct ServiceStatus {
    pub postgres: bool,
    pub qdrant: bool,
    pub tei: bool,
}

/// Model information for the embedding service
#[derive(Serialize)]
pub struct ModelInfo {
    /// Embedding model name
    pub embedding_model: Option<String>,
    /// Embedding dimension (e.g., 768, 1024)
    pub embedding_dim: Option<usize>,
    /// Reranker model info if available
    pub reranker_model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub read_path: Option<&'static str>,
}

pub async fn health_check(State(state): State<Arc<AppState>>) -> Result<Json<HealthResponse>> {
    let cpu_mode = state.config.server.cpu_mode;
    let mode = if cpu_mode { "cpu" } else { "full" };
    let active_manifest = state
        .config
        .server
        .storage_v2_default_read_manifest_sha256
        .as_deref();
    let active = active_manifest.is_some();

    // K3-FIX1: Use HealthPool (restricted) instead of raw pool
    let postgres_ok = state.health_pool.health_check().await.is_ok();
    let (qdrant_ok, tei_ok) = if cpu_mode || active {
        (false, false)
    } else {
        (
            state.qdrant.health_check().await.unwrap_or(false),
            state.tei.health_check().await.unwrap_or(false),
        )
    };

    let all_ok = if let Some(manifest) = active_manifest {
        postgres_ok
            && state
                .health_pool
                .active_set_health_check(manifest)
                .await
                .is_ok()
    } else if cpu_mode {
        postgres_ok
    } else {
        postgres_ok && qdrant_ok && tei_ok
    };

    Ok(Json(HealthResponse {
        status: if all_ok {
            "healthy".to_string()
        } else {
            "degraded".to_string()
        },
        mode: mode.to_string(),
        read_path: active.then_some("storage_v2_active"),
        services: ServiceStatus {
            postgres: postgres_ok,
            qdrant: qdrant_ok,
            tei: tei_ok,
        },
    }))
}

pub async fn liveness() -> &'static str {
    "OK"
}

/// Get model information (Phase 14: Model Upgrades)
pub async fn model_info(State(state): State<Arc<AppState>>) -> Result<Json<ModelInfo>> {
    let active = state
        .config
        .server
        .storage_v2_default_read_manifest_sha256
        .is_some();
    // Fetch reranker model name only in full mode. CPU mode keeps GPU services off
    // intentionally, so model metadata must not probe the reranker endpoint.
    let reranker_model = if state.config.server.cpu_mode || active {
        None
    } else {
        let reranker_url = state.config.tei.reranker_url.as_deref();
        state.tei.get_reranker_model_name(reranker_url).await
    };

    Ok(Json(ModelInfo {
        embedding_model: (!active).then(|| state.tei.get_model_name().to_string()),
        embedding_dim: (!active).then(|| state.tei.get_embedding_dim()),
        reranker_model,
        read_path: active.then_some("storage_v2_active"),
    }))
}
