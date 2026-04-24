use axum::{extract::State, Json};
use serde::Serialize;
use std::sync::Arc;

use crate::error::Result;
use crate::AppState;

#[derive(Serialize)]
pub struct HealthResponse {
    pub status: String,
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
    pub embedding_model: String,
    /// Embedding dimension (e.g., 768, 1024)
    pub embedding_dim: usize,
    /// Reranker model info if available
    pub reranker_model: Option<String>,
}

pub async fn health_check(State(state): State<Arc<AppState>>) -> Result<Json<HealthResponse>> {
    // K3-FIX1: Use HealthPool (restricted) instead of raw pool
    let postgres_ok = state.health_pool.health_check().await.is_ok();
    let qdrant_ok = state.qdrant.health_check().await.unwrap_or(false);
    let tei_ok = state.tei.health_check().await.unwrap_or(false);

    let all_ok = postgres_ok && qdrant_ok && tei_ok;

    Ok(Json(HealthResponse {
        status: if all_ok { "healthy".to_string() } else { "degraded".to_string() },
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
    // Fetch reranker model name (may be None if reranker is offline)
    let reranker_url = state.config.tei.reranker_url.as_deref();
    let reranker_model = state.tei.get_reranker_model_name(reranker_url).await;

    Ok(Json(ModelInfo {
        embedding_model: state.tei.get_model_name().to_string(),
        embedding_dim: state.tei.get_embedding_dim(),
        reranker_model,
    }))
}
