use axum::{
    extract::{Path, State},
    Extension, Json,
};
use serde::Serialize;
use std::str::FromStr;
use std::sync::Arc;
use uuid::Uuid;

use crate::db::models::Source;
use crate::error::{AppError, Result};
use crate::AppState;

#[derive(Debug, Serialize)]
pub struct SourcesResponse {
    pub sources: Vec<Source>,
    pub total: usize,
}

#[cfg(feature = "storage-v2-retrieval")]
#[derive(Debug, serde::Deserialize)]
pub struct ShadowSourceStateQuery {
    pub generation: String,
    #[serde(default)]
    pub include_test: bool,
}

#[cfg(feature = "storage-v2-retrieval")]
pub async fn shadow_source_state(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    axum::extract::Query(request): axum::extract::Query<ShadowSourceStateQuery>,
) -> Result<Json<serde_json::Value>> {
    let user_id = Uuid::from_str(&claims.sub)
        .map_err(|_| AppError::Auth("Invalid user ID in claims".into()))?;
    let instance_id = state.instance_id.to_string();
    state
        .rls_client
        .with_rls(user_id, claims.is_admin, move |transaction| {
            Box::pin(async move {
                let row = transaction
                    .query_one(
                        "SELECT storage_v2_shadow_source_state($1,$2,$3)",
                        &[&source_id, &request.generation, &request.include_test],
                    )
                    .await
                    .map_err(|error| {
                        if error.code()
                            == Some(&tokio_postgres::error::SqlState::INSUFFICIENT_PRIVILEGE)
                        {
                            AppError::Forbidden("shadow source state is not authorized".to_string())
                        } else {
                            AppError::Database(error)
                        }
                    })?;
                let mut value: serde_json::Value = row.get(0);
                value["server_instance_id"] = serde_json::Value::String(instance_id);
                Ok(Json(value))
            })
        })
        .await
}

pub async fn list_sources(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
) -> Result<Json<SourcesResponse>> {
    let user_id = Uuid::from_str(&claims.sub)
        .map_err(|_| AppError::Auth("Invalid user ID in claims".into()))?;

    state
        .rls_client
        .with_rls(user_id, claims.is_admin, |txn| {
            Box::pin(async move {
                let rows = txn
                    .query(
                        r#"
                SELECT id, name, type, path, config, last_synced,
                       file_count, total_size, created_at
                FROM sources
                ORDER BY name
                "#,
                        &[],
                    )
                    .await?;

                let sources: Vec<Source> = rows
                    .iter()
                    .map(|row| Source {
                        id: row.get("id"),
                        name: row.get("name"),
                        source_type: row.get("type"),
                        path: row.get("path"),
                        config: row.get("config"),
                        last_synced: row.get("last_synced"),
                        file_count: row.get("file_count"),
                        total_size: row.get("total_size"),
                        created_at: row.get("created_at"),
                    })
                    .collect();

                let total = sources.len();

                Ok(Json(SourcesResponse { sources, total }))
            })
        })
        .await
}

pub async fn get_source(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
) -> Result<Json<Source>> {
    let user_id = Uuid::from_str(&claims.sub)
        .map_err(|_| AppError::Auth("Invalid user ID in claims".into()))?;

    state
        .rls_client
        .with_rls(user_id, claims.is_admin, |txn| {
            Box::pin(async move {
                let row = txn
                    .query_opt(
                        r#"
                SELECT id, name, type, path, config, last_synced,
                       file_count, total_size, created_at
                FROM sources
                WHERE id = $1
                "#,
                        &[&id],
                    )
                    .await?
                    .ok_or_else(|| AppError::NotFound(format!("Source {} not found", id)))?;

                Ok(Json(Source {
                    id: row.get("id"),
                    name: row.get("name"),
                    source_type: row.get("type"),
                    path: row.get("path"),
                    config: row.get("config"),
                    last_synced: row.get("last_synced"),
                    file_count: row.get("file_count"),
                    total_size: row.get("total_size"),
                    created_at: row.get("created_at"),
                }))
            })
        })
        .await
}
