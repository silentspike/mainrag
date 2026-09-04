use axum::{
    extract::{Path, State},
    http::StatusCode,
    Extension, Json,
};
use serde::{Deserialize, Serialize};
use std::path::{Path as FsPath, PathBuf};
use std::sync::Arc;
#[cfg(feature = "storage-v2-retrieval")]
use uuid::Uuid;

use crate::api::JsonBody;
use crate::error::{AppError, Result};
use crate::plugins;
use crate::services::index::{embedding_document_text, embedding_storage_model_name};
use crate::AppState;

#[derive(Debug, Serialize)]
pub struct SourceResponse {
    pub id: i64,
    pub name: String,
    pub source_type: String,
    pub path: String,
    pub file_count: i64,
    pub chunk_count: i64,
    pub total_size: i64,
    pub last_synced: Option<chrono::DateTime<chrono::Utc>>,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub updated_at: chrono::DateTime<chrono::Utc>,
}

#[derive(Debug, Deserialize)]
pub struct CreateSourceRequest {
    pub name: Option<String>,
    pub source_type: Option<String>,
    pub path: String,
    pub config: Option<serde_json::Value>, // Flexible source-type-specific config
    #[serde(default)]
    pub is_test: bool,
}

#[cfg(feature = "storage-v2-retrieval")]
#[derive(Debug, Deserialize)]
pub struct ShadowSliceRequest {
    pub commit_sha: String,
}

#[cfg(feature = "storage-v2-retrieval")]
pub async fn admin_run_shadow_slice(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<ShadowSliceRequest>,
) -> Result<Json<crate::services::shadow_slice::ShadowSliceResult>> {
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".to_string()))?;
    let commit_sha = request.commit_sha;
    let pack_root = state.config.storage_v2_pack_root.clone();
    let pack_io_buffer_bytes = state.config.storage_v2_pack_io_buffer_bytes;
    state
        .rls_client
        .with_rls(user_id, true, move |transaction| {
            Box::pin(async move {
                let source = transaction
                    .query_opt(
                        "SELECT type, path, is_test FROM sources WHERE id = $1",
                        &[&source_id],
                    )
                    .await?
                    .ok_or_else(|| AppError::NotFound(format!("Source {source_id} not found")))?;
                if !source.get::<_, bool>("is_test") {
                    return Err(AppError::BadRequest(
                        "shadow slice requires a source created with is_test=true".to_string(),
                    ));
                }
                let source_type: String = source.get("type");
                let source_path: String = source.get("path");
                let result = crate::services::shadow_slice::run_public_shadow_slice(
                    &**transaction,
                    source_id,
                    &source_type,
                    std::path::Path::new(&source_path),
                    &pack_root,
                    pack_io_buffer_bytes,
                    &commit_sha,
                )
                .await
                .map_err(|error| {
                    AppError::Internal(format!("storage-v2 shadow slice failed: {error}"))
                })?;
                Ok(Json(result))
            })
        })
        .await
}

#[cfg(feature = "storage-v2-retrieval")]
pub async fn admin_build_release_candidate(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<ShadowSliceRequest>,
) -> Result<Json<crate::services::shadow_slice::ShadowSliceResult>> {
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".to_string()))?;
    let commit_sha = request.commit_sha;
    let pack_root = state.config.storage_v2_pack_root.clone();
    let pack_io_buffer_bytes = state.config.storage_v2_pack_io_buffer_bytes;
    state
        .rls_client
        .with_rls(user_id, true, move |transaction| {
            Box::pin(async move {
                let source = transaction
                    .query_opt(
                        "SELECT type, path FROM sources WHERE id = $1",
                        &[&source_id],
                    )
                    .await?
                    .ok_or_else(|| AppError::NotFound(format!("Source {source_id} not found")))?;
                let source_type: String = source.get("type");
                let source_path: String = source.get("path");
                let result = crate::services::shadow_slice::run_release_candidate_build(
                    &**transaction,
                    source_id,
                    &source_type,
                    std::path::Path::new(&source_path),
                    &pack_root,
                    pack_io_buffer_bytes,
                    &commit_sha,
                )
                .await
                .map_err(|error| {
                    tracing::error!(
                        error = ?error,
                        "storage-v2 release-candidate build detail"
                    );
                    AppError::Internal(format!(
                        "storage-v2 release-candidate build failed: {error}"
                    ))
                })?;
                Ok(Json(result))
            })
        })
        .await
}

#[cfg(feature = "storage-v2-retrieval")]
pub async fn admin_verify_release_candidate(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<crate::services::shadow_slice::ReleaseCandidateVerifyInput>,
) -> Result<Json<crate::services::shadow_slice::ReleaseCandidateVerifyResult>> {
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".to_string()))?;
    let pack_root = state.config.storage_v2_pack_root.clone();
    let pack_io_buffer_bytes = state.config.storage_v2_pack_io_buffer_bytes;
    state
        .rls_client
        .with_rls(user_id, true, move |transaction| {
            Box::pin(async move {
                let result = crate::services::shadow_slice::verify_release_candidate(
                    &**transaction,
                    source_id,
                    &request,
                    &pack_root,
                    pack_io_buffer_bytes,
                )
                .await
                .map_err(|error| {
                    AppError::BadRequest(format!(
                        "storage-v2 release-candidate verification failed: {error}"
                    ))
                })?;
                Ok(Json(result))
            })
        })
        .await
}

#[cfg(feature = "storage-v2-retrieval")]
pub async fn admin_qualify_release_candidate(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<crate::services::shadow_slice::ReleaseCandidateEvidenceInput>,
) -> Result<Json<crate::services::shadow_slice::ReleaseCandidateEvidenceResult>> {
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".to_string()))?;
    state
        .rls_client
        .with_rls(user_id, true, move |transaction| {
            Box::pin(async move {
                let result = crate::services::shadow_slice::qualify_release_candidate(
                    &**transaction,
                    source_id,
                    &request,
                )
                .await
                .map_err(|error| {
                    AppError::BadRequest(format!(
                        "invalid storage-v2 release-candidate evidence: {error}"
                    ))
                })?;
                Ok(Json(result))
            })
        })
        .await
}

#[cfg(feature = "storage-v2-retrieval")]
pub async fn admin_record_dual_read(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<crate::services::shadow_slice::DualReadEvidenceInput>,
) -> Result<Json<crate::services::shadow_slice::DualReadEvidenceResult>> {
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".to_string()))?;
    state
        .rls_client
        .with_rls(user_id, true, move |transaction| {
            Box::pin(async move {
                let result = crate::services::shadow_slice::record_dual_read_evidence(
                    &**transaction,
                    source_id,
                    &request,
                )
                .await
                .map_err(|error| {
                    AppError::BadRequest(format!("invalid dual-read evidence: {error}"))
                })?;
                Ok(Json(result))
            })
        })
        .await
}

#[cfg(feature = "storage-v2-retrieval")]
pub async fn admin_cleanup_shadow_slice(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path((source_id, run_id)): Path<(i64, i64)>,
) -> Result<Json<serde_json::Value>> {
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".to_string()))?;
    state
        .rls_client
        .with_rls(user_id, true, move |transaction| {
            Box::pin(async move {
                let row = transaction
                    .query_opt(
                        "SELECT storage_v2_cleanup_abandoned_shadow_ingest($1) AS cleanup \
                         WHERE EXISTS (SELECT 1 FROM storage_v2_ingest_run WHERE id=$1 AND source_id=$2)",
                        &[&run_id, &source_id],
                    )
                    .await
                    .map_err(|error| {
                        if error.code()
                            == Some(&tokio_postgres::error::SqlState::INSUFFICIENT_PRIVILEGE)
                        {
                            AppError::Forbidden(
                                "shadow cleanup cannot touch visible, verified, active, or foreign state"
                                    .to_string(),
                            )
                        } else {
                            AppError::Database(error)
                        }
                    })?
                    .ok_or_else(|| {
                        AppError::NotFound("shadow ingest run not found".to_string())
                    })?;
                Ok(Json(row.get::<_, serde_json::Value>("cleanup")))
            })
        })
        .await
}

#[derive(Debug, Deserialize)]
pub struct UpdateSourceRequest {
    pub name: Option<String>,
}

pub async fn admin_list_sources(
    State(state): State<Arc<AppState>>,
    Extension(_claims): Extension<Arc<crate::auth::Claims>>,
) -> Result<Json<Vec<SourceResponse>>> {
    // K3: All DB operations in a single transaction via RlsClient
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let rows = txn
                    .query(
                        r#"
                SELECT
                    s.id,
                    s.name,
                    s.type as source_type,
                    s.path,
                    s.last_synced,
                    s.created_at,
                    s.updated_at,
                    COALESCE(s.file_count, 0)::bigint as file_count,
                    COALESCE(s.total_size, 0)::bigint as total_size,
                    COUNT(DISTINCT c.id)::bigint as chunk_count
                FROM sources s
                LEFT JOIN files f ON f.source_id = s.id
                LEFT JOIN chunks c ON c.file_id = f.id
                GROUP BY s.id
                ORDER BY s.name
                "#,
                        &[],
                    )
                    .await?;

                let sources: Vec<SourceResponse> = rows
                    .iter()
                    .map(|row| SourceResponse {
                        id: row.get("id"),
                        name: row.get("name"),
                        source_type: row.get("source_type"),
                        path: row.get("path"),
                        file_count: row.get("file_count"),
                        chunk_count: row.get("chunk_count"),
                        total_size: row.get("total_size"),
                        last_synced: row.get("last_synced"),
                        created_at: row.get("created_at"),
                        updated_at: row.get("updated_at"),
                    })
                    .collect();

                Ok(Json(sources))
            })
        })
        .await
}

pub async fn admin_create_source(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    JsonBody(req): JsonBody<CreateSourceRequest>,
) -> Result<(StatusCode, Json<SourceResponse>)> {
    // Generate name if not provided (before closure — no DB needed)
    let name = req.name.unwrap_or_else(|| {
        req.path
            .split('/')
            .next_back()
            .unwrap_or("source")
            .trim_end_matches(".git")
            .to_string()
    });
    // Set source owner to the authenticated user (not hardcoded admin default)
    let owner_id = uuid::Uuid::parse_str(&claims.sub)
        .map_err(|_| crate::error::AppError::Internal("Invalid user_id in claims".to_string()))?;

    // Auto-detect source type if not provided
    let source_type = req
        .source_type
        .unwrap_or_else(|| plugins::detect_source_type(&req.path));

    // S2: Path traversal prevention — reject suspicious path components
    let path = req.path;
    if path.contains("..") || path.contains('\0') {
        return Err(crate::error::AppError::BadRequest(
            "Invalid path: must not contain '..' or null bytes".to_string(),
        ));
    }
    let config = req.config;
    let is_test = req.is_test;

    // K3: INSERT in transaction via RlsClient
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                // Try to insert, reject duplicates
                match txn
                    .query_one(
                        r#"
                INSERT INTO sources (name, type, path, config, user_id, is_test)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, name, type as source_type, path, last_synced, created_at, updated_at,
                          COALESCE(file_count, 0)::bigint as file_count,
                          COALESCE(total_size, 0)::bigint as total_size
                "#,
                        &[&name, &source_type, &path, &config, &owner_id, &is_test],
                    )
                    .await
                {
                    Ok(row) => {
                        let source = SourceResponse {
                            id: row.get("id"),
                            name: row.get("name"),
                            source_type: row.get("source_type"),
                            path: row.get("path"),
                            file_count: row.get("file_count"),
                            chunk_count: 0,
                            total_size: row.get("total_size"),
                            last_synced: row.get("last_synced"),
                            created_at: row.get("created_at"),
                            updated_at: row.get("updated_at"),
                        };
                        Ok((StatusCode::CREATED, Json(source)))
                    }
                    Err(e) => {
                        // Check if it's a unique constraint violation (error code 23505)
                        if e.code() == Some(&tokio_postgres::error::SqlState::UNIQUE_VIOLATION) {
                            Err(AppError::Conflict(format!(
                                "Source with name '{}' already exists",
                                name
                            )))
                        } else {
                            Err(AppError::Internal(format!(
                                "Failed to create source: {}",
                                e
                            )))
                        }
                    }
                }
            })
        })
        .await
}

pub async fn admin_update_source(
    State(state): State<Arc<AppState>>,
    Extension(_claims): Extension<Arc<crate::auth::Claims>>,
    Path(id): Path<i64>,
    JsonBody(req): JsonBody<UpdateSourceRequest>,
) -> Result<Json<SourceResponse>> {
    // K3: All DB operations in a single transaction via RlsClient
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                // Build dynamic update query
                let mut set_clauses = vec!["updated_at = NOW()".to_string()];
                let mut params: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> = vec![];
                let mut param_idx = 1;

                if let Some(ref name) = req.name {
                    set_clauses.push(format!("name = ${}", param_idx));
                    params.push(name);
                    param_idx += 1;
                }

                params.push(&id);

                let query = format!(
                    r#"
            UPDATE sources
            SET {}
            WHERE id = ${}
            RETURNING id, name, type as source_type, path, last_synced, created_at, updated_at,
                      COALESCE(file_count, 0)::bigint as file_count,
                      COALESCE(total_size, 0)::bigint as total_size
            "#,
                    set_clauses.join(", "),
                    param_idx
                );

                let row = txn
                    .query_opt(&query, &params)
                    .await?
                    .ok_or_else(|| AppError::NotFound(format!("Source {} not found", id)))?;

                // Get chunk count in same transaction
                let chunk_count: i64 = txn
                    .query_one(
                        r#"
                SELECT COUNT(DISTINCT c.id)::bigint as chunk_count
                FROM files f
                LEFT JOIN chunks c ON c.file_id = f.id
                WHERE f.source_id = $1
                "#,
                        &[&id],
                    )
                    .await?
                    .get("chunk_count");

                let source = SourceResponse {
                    id: row.get("id"),
                    name: row.get("name"),
                    source_type: row.get("source_type"),
                    path: row.get("path"),
                    file_count: row.get("file_count"),
                    chunk_count,
                    total_size: row.get("total_size"),
                    last_synced: row.get("last_synced"),
                    created_at: row.get("created_at"),
                    updated_at: row.get("updated_at"),
                };

                Ok(Json(source))
            })
        })
        .await
}

pub async fn admin_delete_source(
    State(state): State<Arc<AppState>>,
    Extension(_claims): Extension<Arc<crate::auth::Claims>>,
    Path(id): Path<i64>,
) -> Result<StatusCode> {
    // K3: All DB deletes in a single atomic transaction via RlsClient
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                // Check source exists first
                let exists: i64 = txn
                    .query_one("SELECT COUNT(*) FROM sources WHERE id = $1", &[&id])
                    .await?
                    .get(0);

                if exists == 0 {
                    return Err(AppError::NotFound(format!("Source {} not found", id)));
                }

                // BATCH DELETE: Delete dependent data explicitly to avoid slow CASCADE
                // This is faster than letting CASCADE handle 100k+ rows in one transaction

                // 1. Delete outbox entries (prevents ON DELETE SET NULL timeout)
                let cleaned_outbox: i64 = txn
                    .query_one(
                        "WITH deleted AS (
                    DELETE FROM indexing_outbox
                    WHERE file_id IN (SELECT id FROM files WHERE source_id = $1)
                    RETURNING 1
                ) SELECT COUNT(*) FROM deleted",
                        &[&id],
                    )
                    .await?
                    .get(0);
                tracing::info!(
                    source_id = id,
                    count = cleaned_outbox,
                    "Deleted outbox entries"
                );

                // 2. Delete call_graph (depends on symbols)
                let deleted_cg: i64 = txn
                    .query_one(
                        "WITH deleted AS (
                    DELETE FROM call_graph
                    WHERE caller_symbol_id IN (
                        SELECT s.id FROM symbols s
                        JOIN files f ON s.file_id = f.id
                        WHERE f.source_id = $1
                    )
                    RETURNING 1
                ) SELECT COUNT(*) FROM deleted",
                        &[&id],
                    )
                    .await?
                    .get(0);
                tracing::info!(
                    source_id = id,
                    count = deleted_cg,
                    "Deleted call_graph entries"
                );

                // 3. Delete symbols (depends on files)
                let deleted_sym: i64 = txn
                    .query_one(
                        "WITH deleted AS (
                    DELETE FROM symbols
                    WHERE file_id IN (SELECT id FROM files WHERE source_id = $1)
                    RETURNING 1
                ) SELECT COUNT(*) FROM deleted",
                        &[&id],
                    )
                    .await?
                    .get(0);
                tracing::info!(source_id = id, count = deleted_sym, "Deleted symbols");

                // 4. Delete chunks (depends on files)
                let deleted_chunks: i64 = txn
                    .query_one(
                        "WITH deleted AS (
                    DELETE FROM chunks
                    WHERE file_id IN (SELECT id FROM files WHERE source_id = $1)
                    RETURNING 1
                ) SELECT COUNT(*) FROM deleted",
                        &[&id],
                    )
                    .await?
                    .get(0);
                tracing::info!(source_id = id, count = deleted_chunks, "Deleted chunks");

                // 5. Delete files
                let deleted_files: i64 = txn
                    .query_one(
                        "WITH deleted AS (
                    DELETE FROM files WHERE source_id = $1 RETURNING 1
                ) SELECT COUNT(*) FROM deleted",
                        &[&id],
                    )
                    .await?
                    .get(0);
                tracing::info!(source_id = id, count = deleted_files, "Deleted files");

                // 6. Finally delete the source (now empty, fast)
                txn.execute("DELETE FROM sources WHERE id = $1", &[&id])
                    .await?;

                tracing::info!(
                    source_id = id,
                    files = deleted_files,
                    chunks = deleted_chunks,
                    symbols = deleted_sym,
                    call_graph = deleted_cg,
                    "Source deleted from PostgreSQL"
                );

                Ok(())
            })
        })
        .await?;

    // Delete vectors from Qdrant (outside DB transaction, best-effort)
    // Done AFTER PostgreSQL commit to ensure consistency
    match state.qdrant.delete_by_source(id).await {
        Ok(_) => {
            tracing::info!(source_id = id, "Qdrant vectors deleted for source");
        }
        Err(e) => {
            // Log error but don't fail - PostgreSQL data is already deleted
            // Qdrant cleanup can be retried manually if needed
            tracing::error!(source_id = id, error = %e,
                "Failed to delete Qdrant vectors - manual cleanup may be required");
        }
    }

    Ok(StatusCode::NO_CONTENT)
}

/// Sync/Index a source - this is the real implementation
pub async fn admin_sync_source(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Extension(_claims): Extension<Arc<crate::auth::Claims>>,
) -> Result<Json<serde_json::Value>> {
    use crate::services::IndexService;

    let is_test = state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                Ok(txn
                    .query_opt("SELECT is_test FROM sources WHERE id=$1", &[&id])
                    .await?
                    .map(|row| row.get::<_, bool>(0)))
            })
        })
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Source {id} not found")))?;
    if is_test {
        return Err(AppError::BadRequest(
            "test sources can only use the explicit storage-v2 shadow endpoint".to_string(),
        ));
    }

    // K3: IndexService manages its own DB connections from the pool.
    // No RLS setup needed here — IndexService handles it internally.
    let index_service =
        IndexService::new(state.db.clone(), state.tei.clone(), state.qdrant.clone())?;

    // Run indexing (this can take a while for large sources)
    let stats = index_service.index_source(id).await?;

    Ok(Json(serde_json::json!({
        "status": "completed",
        "source_id": id,
        "stats": {
            "files_processed": stats.files_processed,
            "chunks_created": stats.chunks_created,
            "embeddings_generated": stats.embeddings_generated,
            "errors": stats.errors.len()
        },
        "error_details": stats.errors
    })))
}

/// Get detailed stats for a source (used before deletion)
#[derive(Debug, Serialize)]
pub struct SourceStats {
    pub chunks: i64,
    pub symbols: i64,
    pub call_graph: i64,
    pub qdrant_vectors: i64,
}

pub async fn admin_source_stats(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Extension(_claims): Extension<Arc<crate::auth::Claims>>,
) -> Result<Json<SourceStats>> {
    // K3: DB queries in a single transaction via RlsClient
    let (chunks, symbols, call_graph) = state.rls_client.with_system(|txn| Box::pin(async move {
        let chunks: i64 = txn
            .query_one(
                "SELECT COUNT(*) FROM chunks c JOIN files f ON c.file_id = f.id WHERE f.source_id = $1",
                &[&id],
            )
            .await?
            .get(0);

        let symbols: i64 = txn
            .query_one(
                "SELECT COUNT(*) FROM symbols s JOIN files f ON s.file_id = f.id WHERE f.source_id = $1",
                &[&id],
            )
            .await?
            .get(0);

        let call_graph: i64 = txn
            .query_one(
                "SELECT COUNT(*) FROM call_graph cg
                 JOIN symbols s ON cg.caller_symbol_id = s.id
                 JOIN files f ON s.file_id = f.id
                 WHERE f.source_id = $1",
                &[&id],
            )
            .await?
            .get(0);

        Ok((chunks, symbols, call_graph))
    })).await?;

    // Get Qdrant vectors count (outside DB transaction)
    let qdrant_vectors = match state.qdrant.count_by_source(id).await {
        Ok(count) => count as i64,
        Err(_) => 0, // Fallback if Qdrant query fails
    };

    Ok(Json(SourceStats {
        chunks,
        symbols,
        call_graph,
        qdrant_vectors,
    }))
}

#[derive(Debug, Serialize)]
pub struct SystemStats {
    pub sources: i64,
    pub files: i64,
    pub chunks: i64,
    pub total_size_bytes: i64,
    pub postgres_size: String,
}

pub async fn admin_system_stats(
    State(state): State<Arc<AppState>>,
    Extension(_claims): Extension<Arc<crate::auth::Claims>>,
) -> Result<Json<SystemStats>> {
    // K3: System stats in a single transaction via RlsClient
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let row = txn
                    .query_one(
                        r#"
                SELECT
                    (SELECT COUNT(*) FROM sources)::bigint as sources,
                    (SELECT COUNT(*) FROM files)::bigint as files,
                    (SELECT COUNT(*) FROM chunks)::bigint as chunks,
                    (SELECT COALESCE(SUM(size_original), 0) FROM files)::bigint as total_size,
                    pg_size_pretty(pg_database_size(current_database())) as db_size
                "#,
                        &[],
                    )
                    .await?;

                Ok(Json(SystemStats {
                    sources: row.get("sources"),
                    files: row.get("files"),
                    chunks: row.get("chunks"),
                    total_size_bytes: row.get("total_size"),
                    postgres_size: row.get("db_size"),
                }))
            })
        })
        .await
}

/// Response for backfill operations
#[derive(Debug, Serialize)]
pub struct BackfillResult {
    pub processed: usize,
    pub batches: usize,
    pub message: String,
}

#[derive(Debug, Deserialize)]
pub struct IntelligenceBackfillRequest {
    pub source_id: Option<i64>,
    pub limit: Option<i64>,
    #[serde(default)]
    pub force: bool,
}

#[derive(Debug, Serialize)]
pub struct IntelligenceBackfillFileResult {
    pub file_id: i64,
    pub path: String,
    pub status: String,
    pub symbols: usize,
    pub calls: usize,
    pub reason: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct IntelligenceBackfillResult {
    pub processed: usize,
    pub skipped: usize,
    pub errors: usize,
    pub candidates: usize,
    pub files: Vec<IntelligenceBackfillFileResult>,
    pub message: String,
}

struct IntelligenceBackfillCandidate {
    file_id: i64,
    file_path: String,
    source_path: String,
    content: Vec<u8>,
    content_text: Option<String>,
    size_original: i32,
}

/// Request for incremental file sync (watch mode)
#[derive(Debug, Deserialize)]
pub struct SyncFilesRequest {
    /// Absolute file paths to sync
    pub files: Vec<String>,
}

/// Sync specific files incrementally (for watch mode)
/// Unlike full sync, this only processes the specified files and does NOT detect deletions.
pub async fn admin_sync_files(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Extension(_claims): Extension<Arc<crate::auth::Claims>>,
    JsonBody(req): JsonBody<SyncFilesRequest>,
) -> Result<Json<serde_json::Value>> {
    use crate::services::IndexService;

    let is_test = state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                Ok(txn
                    .query_opt("SELECT is_test FROM sources WHERE id=$1", &[&id])
                    .await?
                    .map(|row| row.get::<_, bool>(0)))
            })
        })
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Source {id} not found")))?;
    if is_test {
        return Err(AppError::BadRequest(
            "test sources cannot enter legacy incremental sync".to_string(),
        ));
    }

    // S2: Path traversal prevention on file paths
    for f in &req.files {
        if f.contains("..") || f.contains('\0') {
            return Err(crate::error::AppError::BadRequest(
                "Invalid file path: must not contain '..' or null bytes".to_string(),
            ));
        }
    }

    // Convert strings to PathBuf
    let files: Vec<std::path::PathBuf> = req.files.iter().map(std::path::PathBuf::from).collect();

    tracing::info!(
        source_id = id,
        file_count = files.len(),
        "Incremental sync requested"
    );

    // K3: IndexService manages its own DB connections from the pool.
    // No RLS setup needed here — IndexService handles it internally.
    let index_service =
        IndexService::new(state.db.clone(), state.tei.clone(), state.qdrant.clone())?;

    // Run incremental indexing
    let stats = index_service.sync_files(id, &files).await?;

    Ok(Json(serde_json::json!({
        "status": "completed",
        "source_id": id,
        "stats": {
            "files_processed": stats.files_processed,
            "files_skipped": stats.files_skipped,
            "chunks_created": stats.chunks_created,
            "embeddings_generated": stats.embeddings_generated,
            "errors": stats.errors.len()
        },
        "error_details": stats.errors
    })))
}

/// Batch size for backfill operations (must not exceed TEI max_client_batch_size=32)
const BACKFILL_BATCH_SIZE: i64 = 32;

/// Backfill orphaned chunks (chunks without embeddings)
/// This is an admin-only maintenance endpoint.
/// Orphaned chunks can occur when embedding fails mid-transaction.
///
/// Uses BATCH PROCESSING with LIMIT to avoid loading all orphans into RAM.
/// Each batch is committed separately for progress visibility.
pub async fn admin_backfill_orphaned(
    State(state): State<Arc<AppState>>,
    Extension(_claims): Extension<Arc<crate::auth::Claims>>,
) -> Result<Json<BackfillResult>> {
    use pgvector::Vector;

    let model_name = embedding_storage_model_name(state.tei.get_model_name());
    let mut total_processed = 0;
    let mut batch_count = 0;

    // Process in batches to avoid OOM with large orphan counts
    loop {
        // 1. Find orphaned chunks (BATCH with LIMIT) in one transaction
        // Returns owned data so the transaction can be committed immediately
        let orphans: Vec<(i64, String, Option<String>, i64, i64)> = state
            .rls_client
            .with_system(|txn| {
                Box::pin(async move {
                    let rows = txn
                        .query(
                            "SELECT c.id, c.content_text, c.context_prefix, c.file_id, f.source_id
                 FROM chunks c
                 JOIN files f ON f.id = c.file_id
                 LEFT JOIN chunk_embeddings ce ON ce.chunk_id = c.id
                 WHERE ce.chunk_id IS NULL
                 LIMIT $1",
                            &[&BACKFILL_BATCH_SIZE],
                        )
                        .await?;

                    let data: Vec<(i64, String, Option<String>, i64, i64)> = rows
                        .iter()
                        .map(|r| {
                            (
                                r.get("id"),
                                r.get("content_text"),
                                r.get("context_prefix"),
                                r.get("file_id"),
                                r.get("source_id"),
                            )
                        })
                        .collect();

                    Ok(data)
                })
            })
            .await?;

        if orphans.is_empty() {
            break; // No more orphans
        }

        let batch_size = orphans.len();
        tracing::info!(
            batch = batch_count + 1,
            count = batch_size,
            "Processing orphan batch"
        );

        // 2. Collect texts for batch embedding (outside DB transaction)
        let texts: Vec<String> = orphans
            .iter()
            .map(|(_, text, context_prefix, _, _)| {
                embedding_document_text(context_prefix.as_deref(), text)
            })
            .collect();
        let text_refs: Vec<&str> = texts.iter().map(String::as_str).collect();

        // 3. Batch-embed via TEI (CRITICAL: batch call, not per-chunk!)
        let embeddings = state
            .tei
            .embed_batch(&text_refs)
            .await
            .map_err(|e| AppError::Internal(format!("TEI batch embedding failed: {}", e)))?;

        if embeddings.len() != orphans.len() {
            return Err(AppError::Internal(format!(
                "Embedding count mismatch: got {}, expected {}",
                embeddings.len(),
                orphans.len()
            )));
        }

        // 4. Transaction: Insert embeddings + outbox entries for THIS BATCH
        let model_name_c = model_name.clone();
        state
            .rls_client
            .with_system(|txn| {
                Box::pin(async move {
                    for (orphan, embedding) in orphans.iter().zip(embeddings.iter()) {
                        let chunk_id = orphan.0;
                        let file_id = orphan.3;
                        let source_id = orphan.4;
                        let embedding_vec = Vector::from(embedding.clone());

                        // Insert embedding
                        txn.execute(
                            "INSERT INTO chunk_embeddings (chunk_id, model, vector)
                     VALUES ($1, $2, $3)
                     ON CONFLICT (chunk_id) DO UPDATE SET
                     vector = EXCLUDED.vector, model = EXCLUDED.model, created_at = NOW()",
                            &[&chunk_id, &model_name_c, &embedding_vec],
                        )
                        .await?;

                        // Queue to outbox for Qdrant sync
                        txn.execute(
                    "INSERT INTO indexing_outbox (action, chunk_id, file_id, source_id, payload)
                     VALUES ('upsert', $1, $2, $3, '{}'::jsonb)",
                    &[&chunk_id, &file_id, &source_id]
                ).await?;
                    }

                    Ok(())
                })
            })
            .await?;

        total_processed += batch_size;
        batch_count += 1;

        tracing::info!(
            batch = batch_count,
            processed = batch_size,
            total = total_processed,
            "Batch committed"
        );
    }

    if total_processed == 0 {
        return Ok(Json(BackfillResult {
            processed: 0,
            batches: 0,
            message: "No orphaned chunks found".to_string(),
        }));
    }

    tracing::info!(
        total = total_processed,
        batches = batch_count,
        "Backfill completed successfully"
    );

    Ok(Json(BackfillResult {
        processed: total_processed,
        batches: batch_count,
        message: format!(
            "Backfilled {} orphaned chunks in {} batches",
            total_processed, batch_count
        ),
    }))
}

fn is_large_json_like(candidate: &IntelligenceBackfillCandidate) -> bool {
    const LARGE_FILE_THRESHOLD: i32 = 5 * 1024 * 1024;
    let path = candidate.file_path.to_ascii_lowercase();
    candidate.size_original > LARGE_FILE_THRESHOLD
        && (path.ends_with(".json") || path.ends_with(".jsonl"))
}

fn source_backed_path(candidate: &IntelligenceBackfillCandidate) -> PathBuf {
    let file_path = PathBuf::from(&candidate.file_path);
    if file_path.is_absolute() {
        file_path
    } else {
        PathBuf::from(&candidate.source_path).join(file_path)
    }
}

async fn load_intelligence_backfill_content(
    candidate: &IntelligenceBackfillCandidate,
) -> std::result::Result<String, String> {
    if let Some(content_text) = candidate.content_text.as_deref() {
        if !content_text.trim().is_empty() {
            return Ok(content_text.to_string());
        }
    }

    if is_large_json_like(candidate) {
        return Err(
            "large JSON/JSONL file has no DB content; skipped to avoid full conversation re-read"
                .to_string(),
        );
    }

    match zstd::decode_all(candidate.content.as_slice()) {
        Ok(decoded) if !decoded.is_empty() => {
            return String::from_utf8(decoded)
                .map_err(|e| format!("stored content is not valid UTF-8: {}", e));
        }
        Ok(_) => {}
        Err(e) => {
            tracing::warn!(
                file_id = candidate.file_id,
                path = %candidate.file_path,
                error = %e,
                "Could not decode stored file content, trying source-backed file"
            );
        }
    }

    let disk_path = source_backed_path(candidate);
    match tokio::fs::read_to_string(&disk_path).await {
        Ok(text) if !text.trim().is_empty() => Ok(text),
        Ok(_) => Err(format!(
            "source-backed file is empty: {}",
            disk_path.display()
        )),
        Err(e) => Err(format!(
            "content unavailable; stored content is empty and source-backed file is not readable: {} ({})",
            disk_path.display(),
            e
        )),
    }
}

/// Backfill code intelligence for files that were hash-skipped before symbols existed.
///
/// This endpoint deliberately does not create embeddings or Qdrant outbox rows.
/// It reuses IntelligenceService::analyze_file(), whose re-analysis semantics
/// delete stale per-file symbols/call_graph rows before inserting current ones.
pub async fn admin_backfill_intelligence(
    State(state): State<Arc<AppState>>,
    Extension(_claims): Extension<Arc<crate::auth::Claims>>,
    JsonBody(req): JsonBody<IntelligenceBackfillRequest>,
) -> Result<Json<IntelligenceBackfillResult>> {
    let limit = req.limit.unwrap_or(100).clamp(1, 1000);
    let force = req.force;
    let source_id = req.source_id;

    let candidates: Vec<IntelligenceBackfillCandidate> = state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let rows = txn
                    .query(
                        r#"
                        SELECT f.id, f.path, s.path AS source_path, f.content,
                               f.content_text, f.size_original
                        FROM files f
                        JOIN sources s ON s.id = f.source_id
                        WHERE ($1::BIGINT IS NULL OR f.source_id = $1)
                          AND ($2::BOOL OR f.intelligence_analyzed_at IS NULL)
                        ORDER BY f.updated_at ASC, f.id ASC
                        LIMIT $3
                        "#,
                        &[&source_id, &force, &limit],
                    )
                    .await?;

                Ok(rows
                    .iter()
                    .map(|row| IntelligenceBackfillCandidate {
                        file_id: row.get("id"),
                        file_path: row.get("path"),
                        source_path: row.get("source_path"),
                        content: row.get("content"),
                        content_text: row.get("content_text"),
                        size_original: row.get("size_original"),
                    })
                    .collect())
            })
        })
        .await?;

    let candidates_count = candidates.len();
    let mut processed = 0usize;
    let mut skipped = 0usize;
    let mut errors = 0usize;
    let mut files = Vec::with_capacity(candidates_count);

    for candidate in candidates {
        let path = candidate.file_path.clone();
        match load_intelligence_backfill_content(&candidate).await {
            Ok(content) => {
                match state
                    .intelligence
                    .analyze_file(candidate.file_id, FsPath::new(&path), &content)
                    .await
                {
                    Ok(parse_result) => {
                        processed += 1;
                        files.push(IntelligenceBackfillFileResult {
                            file_id: candidate.file_id,
                            path,
                            status: "processed".to_string(),
                            symbols: parse_result.symbols.len(),
                            calls: parse_result.calls.len(),
                            reason: None,
                        });
                    }
                    Err(e) => {
                        errors += 1;
                        files.push(IntelligenceBackfillFileResult {
                            file_id: candidate.file_id,
                            path,
                            status: "error".to_string(),
                            symbols: 0,
                            calls: 0,
                            reason: Some(e.to_string()),
                        });
                    }
                }
            }
            Err(reason) => {
                skipped += 1;
                files.push(IntelligenceBackfillFileResult {
                    file_id: candidate.file_id,
                    path,
                    status: "skipped".to_string(),
                    symbols: 0,
                    calls: 0,
                    reason: Some(reason),
                });
            }
        }
    }

    let message = format!(
        "Backfilled intelligence for {} files ({} skipped, {} errors, {} candidates)",
        processed, skipped, errors, candidates_count
    );

    Ok(Json(IntelligenceBackfillResult {
        processed,
        skipped,
        errors,
        candidates: candidates_count,
        files,
        message,
    }))
}

/// K4: Backfill user_id into existing Qdrant point payloads.
/// Iterates over all sources, reads their user_id from PostgreSQL,
/// and uses Qdrant set_payload API to add user_id to all points
/// for that source. Also creates a payload index on user_id.
///
/// This is idempotent — safe to re-run.
pub async fn admin_backfill_qdrant_user_ids(
    State(state): State<Arc<AppState>>,
    Extension(_claims): Extension<Arc<crate::auth::Claims>>,
) -> Result<Json<BackfillResult>> {
    if state.config.server.cpu_mode {
        return Err(AppError::BadRequest(
            "requires Qdrant - not available in CPU mode".to_string(),
        ));
    }

    // 1. Create payload index for user_id (idempotent)
    tracing::info!("K4 Backfill: Creating user_id payload index on Qdrant...");
    state
        .qdrant
        .create_payload_index("user_id", "keyword")
        .await?;
    tracing::info!("K4 Backfill: user_id payload index created (or already exists)");

    // 2. Get all source_id -> user_id mappings from PostgreSQL
    let source_mappings: Vec<(i64, String)> = state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let rows = txn
                    .query("SELECT id, user_id::text FROM sources", &[])
                    .await?;

                Ok(rows
                    .iter()
                    .map(|r| {
                        let id: i64 = r.get("id");
                        let user_id: String = r.get("user_id");
                        (id, user_id)
                    })
                    .collect())
            })
        })
        .await?;

    tracing::info!(
        sources = source_mappings.len(),
        "K4 Backfill: Found sources to process"
    );

    let mut total_sources = 0;
    let mut total_errors = 0;

    // 3. For each source, set user_id payload on all its Qdrant points
    for (source_id, user_id) in &source_mappings {
        let payload = serde_json::json!({
            "user_id": user_id
        });

        match state
            .qdrant
            .set_payload_by_source(*source_id, payload)
            .await
        {
            Ok(()) => {
                total_sources += 1;
                tracing::info!(source_id, user_id, "K4 Backfill: Updated points for source");
            }
            Err(e) => {
                total_errors += 1;
                tracing::error!(source_id, user_id, error = %e, "K4 Backfill: Failed to update source");
            }
        }
    }

    let message = if total_errors > 0 {
        format!(
            "Backfilled user_id for {} sources ({} errors). Re-run to retry failed sources.",
            total_sources, total_errors
        )
    } else {
        format!(
            "Backfilled user_id for all {} sources successfully",
            total_sources
        )
    };

    tracing::info!(
        sources = total_sources,
        errors = total_errors,
        "K4 Backfill complete"
    );

    Ok(Json(BackfillResult {
        processed: total_sources,
        batches: source_mappings.len(),
        message,
    }))
}
