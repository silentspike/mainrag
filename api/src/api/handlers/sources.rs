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

pub async fn list_sources(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
) -> Result<Json<SourcesResponse>> {
    let user_id = Uuid::from_str(&claims.sub)
        .map_err(|_| AppError::Auth("Invalid user ID in claims".into()))?;

    state.rls_client.with_rls(user_id, claims.is_admin, |txn| Box::pin(async move {
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
    })).await
}

pub async fn get_source(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
) -> Result<Json<Source>> {
    let user_id = Uuid::from_str(&claims.sub)
        .map_err(|_| AppError::Auth("Invalid user ID in claims".into()))?;

    state.rls_client.with_rls(user_id, claims.is_admin, |txn| Box::pin(async move {
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
    })).await
}
