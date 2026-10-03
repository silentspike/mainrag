//! Authorized native legacy hit resolution and bounded administrator mapping work.
use crate::api::extractors::JsonBody;
use crate::error::{AppError, Result};
use crate::AppState;
use axum::{
    extract::{Path, State},
    Extension, Json,
};
use serde::Deserialize;
use serde_json::Value;
use std::sync::Arc;
use uuid::Uuid;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LegacyHitResolveRequest {
    pub source_id: i64,
    pub generation: String,
    pub old_hit_id: String,
    #[serde(default)]
    pub include_test: bool,
}

fn database_error(error: tokio_postgres::Error) -> AppError {
    if error.code() == Some(&tokio_postgres::error::SqlState::INSUFFICIENT_PRIVILEGE) {
        AppError::Forbidden("legacy hit source is not authorized".into())
    } else if error.code() == Some(&tokio_postgres::error::SqlState::RAISE_EXCEPTION) {
        AppError::BadRequest("legacy hit selector, mapping state or coordinates rejected".into())
    } else {
        AppError::Database(error)
    }
}

pub async fn resolve_legacy_hit(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    JsonBody(request): JsonBody<LegacyHitResolveRequest>,
) -> Result<Json<Value>> {
    if request.source_id <= 0
        || request.old_hit_id.is_empty()
        || request.old_hit_id.len() > 512
        || !(request.generation == "active"
            || (!request.generation.starts_with('0')
                && request
                    .generation
                    .parse::<i64>()
                    .is_ok_and(|sequence| sequence > 0)))
    {
        return Err(AppError::BadRequest(
            "a source, active or positive generation, and bounded legacy hit ID are required"
                .into(),
        ));
    }
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".into()))?;
    state
        .rls_client
        .with_rls(user_id, claims.is_admin, move |transaction| {
            Box::pin(async move {
                transaction
                    .execute(
                        "SELECT storage_v2_require_test_scope($1,$2)",
                        &[&Some(request.source_id), &request.include_test],
                    )
                    .await
                    .map_err(database_error)?;
                let row = transaction
                    .query_one(
                        "SELECT storage_v2_resolve_legacy_hit($1,$2,$3)",
                        &[&request.source_id, &request.generation, &request.old_hit_id],
                    )
                    .await
                    .map_err(database_error)?;
                Ok(Json(row.get(0)))
            })
        })
        .await
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LegacyHitMappingRequest {
    pub records: Value,
    #[serde(default)]
    pub include_test: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LegacyHitMappingStateRequest {
    pub old_hit_ids: Vec<String>,
    #[serde(default)]
    pub include_test: bool,
}

pub async fn admin_produce_legacy_hits(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<crate::services::legacy_hit_producer::operation::ProduceBatchInput>,
) -> Result<Json<Value>> {
    if !claims.is_admin {
        return Err(AppError::Forbidden(
            "administrator authority required".into(),
        ));
    }
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".into()))?;
    let root = state.config.storage_v2_pack_root.clone();
    let buffer = state.config.storage_v2_pack_io_buffer_bytes;
    // Hold this fence until with_rls has committed, including the unknown
    // COMMIT outcome. A returned service future must not drop it prematurely.
    let _lease = crate::services::content_store::build_recovery::writer_lease(&root)
        .map_err(|_| AppError::BadRequest("native pack maintenance is active".into()))?;
    let result = state
        .rls_client
        .with_rls(user_id, true, move |transaction| {
            Box::pin(async move {
                crate::services::legacy_hit_producer::operation::produce_batch(
                    &**transaction,
                    source_id,
                    &request,
                    &root,
                    buffer,
                )
                .await
                .map(Json)
                .map_err(|error| {
                    if error
                        .downcast_ref::<tokio_postgres::Error>()
                        .and_then(tokio_postgres::Error::code)
                        == Some(&tokio_postgres::error::SqlState::INSUFFICIENT_PRIVILEGE)
                    {
                        AppError::Forbidden("legacy producer source is not authorized".into())
                    } else {
                        AppError::BadRequest(
                            "legacy producer identity, resource, content or mapping proof rejected"
                                .into(),
                        )
                    }
                })
            })
        })
        .await;
    result
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LegacyHitInventoryRequest {
    pub generation_id: i64,
    pub after_file_id: i64,
    #[serde(default)]
    pub include_test: bool,
}

pub async fn admin_legacy_hit_inventory(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<LegacyHitInventoryRequest>,
) -> Result<Json<Value>> {
    if !claims.is_admin {
        return Err(AppError::Forbidden(
            "administrator authority required".into(),
        ));
    }
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".into()))?;
    state
        .rls_client
        .with_rls(user_id, true, move |transaction| {
            Box::pin(async move {
                transaction
                    .execute(
                        "SELECT storage_v2_require_test_scope($1,$2)",
                        &[&Some(source_id), &request.include_test],
                    )
                    .await
                    .map_err(database_error)?;
                transaction
                    .query_one(
                        "SELECT storage_v2_legacy_hit_inventory($1,$2,$3)",
                        &[&source_id, &request.generation_id, &request.after_file_id],
                    )
                    .await
                    .map(|row| Json(row.get(0)))
                    .map_err(database_error)
            })
        })
        .await
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LegacyHitProgressRequest {
    pub generation_id: i64,
    pub file_id: i64,
    #[serde(default)]
    pub include_test: bool,
}

pub async fn admin_legacy_hit_progress(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<LegacyHitProgressRequest>,
) -> Result<Json<Value>> {
    if !claims.is_admin || source_id <= 0 || request.file_id <= 0 || request.generation_id <= 0 {
        return Err(AppError::Forbidden(
            "authorized administrator job identity required".into(),
        ));
    }
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".into()))?;
    state.rls_client.with_rls(user_id,true,move |transaction| {
        Box::pin(async move {
            transaction.execute("SELECT storage_v2_require_test_scope($1,$2)",&[&Some(source_id),&request.include_test])
                .await.map_err(database_error)?;
            let authorized:bool=transaction.query_one("SELECT storage_v2_is_admin() AND storage_v2_can_access_source($1,'write')",
                &[&source_id]).await.map_err(database_error)?.get(0);
            if !authorized {return Err(AppError::Forbidden("legacy job source is not authorized".into()));}
            let row=transaction.query_one("WITH identity AS (SELECT hashtextextended(\
                'mainrag.storage-v2-ingest-source:'||$1::BIGINT::TEXT,0) AS lock_key) \
                SELECT jsonb_build_object('source_id',$1::BIGINT,'generation_id',$2::BIGINT,'file_id',$3::BIGINT,\
                    'producer_running',EXISTS(SELECT 1 FROM pg_locks held JOIN pg_stat_activity activity ON activity.pid=held.pid \
                        CROSS JOIN identity WHERE held.locktype='advisory' AND held.granted AND held.objsubid=1 \
                        AND held.classid::BIGINT=((identity.lock_key>>32)&4294967295::BIGINT) \
                        AND held.objid::BIGINT=(identity.lock_key&4294967295::BIGINT) \
                        AND activity.application_name='mainrag.legacy-hit-producer:'||$1::BIGINT::TEXT),\
                    'completed_hits',(SELECT count(*) FROM storage_v2_legacy_hit_proof WHERE source_id=$1 \
                        AND generation_id=$2 AND proof->>'file_id'=$3::BIGINT::TEXT))",
                &[&source_id,&request.generation_id,&request.file_id]).await.map_err(database_error)?;
            Ok(Json(row.get(0)))
        })
    }).await
}

pub async fn admin_replace_legacy_hit_mappings(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<LegacyHitMappingRequest>,
) -> Result<Json<Value>> {
    if !claims.is_admin {
        return Err(AppError::Forbidden(
            "administrator authority required".into(),
        ));
    }
    if source_id <= 0
        || request
            .records
            .as_array()
            .is_none_or(|records| records.is_empty() || records.len() > 512)
    {
        return Err(AppError::BadRequest(
            "a positive source and bounded mapping batch are required".into(),
        ));
    }
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".into()))?;
    state
        .rls_client
        .with_rls(user_id, true, move |transaction| {
            Box::pin(async move {
                transaction
                    .execute(
                        "SELECT storage_v2_require_test_scope($1,$2)",
                        &[&Some(source_id), &request.include_test],
                    )
                    .await
                    .map_err(database_error)?;
                let row = transaction
                    .query_one(
                        "SELECT storage_v2_replace_legacy_hit_mappings($1,$2)",
                        &[&source_id, &request.records],
                    )
                    .await
                    .map_err(database_error)?;
                Ok(Json(row.get(0)))
            })
        })
        .await
}

pub async fn admin_legacy_hit_mapping_states(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<LegacyHitMappingStateRequest>,
) -> Result<Json<Value>> {
    if !claims.is_admin {
        return Err(AppError::Forbidden(
            "administrator authority required".into(),
        ));
    }
    if source_id <= 0
        || request.old_hit_ids.is_empty()
        || request.old_hit_ids.len() > 512
        || request
            .old_hit_ids
            .iter()
            .any(|id| id.is_empty() || id.len() > 512)
    {
        return Err(AppError::BadRequest(
            "a positive source and bounded legacy hit IDs are required".into(),
        ));
    }
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".into()))?;
    state
        .rls_client
        .with_rls(user_id, true, move |transaction| {
            Box::pin(async move {
                transaction
                    .execute(
                        "SELECT storage_v2_require_test_scope($1,$2)",
                        &[&Some(source_id), &request.include_test],
                    )
                    .await
                    .map_err(database_error)?;
                let row = transaction
                    .query_one(
                        "SELECT storage_v2_legacy_hit_mapping_states($1,$2)",
                        &[&source_id, &request.old_hit_ids],
                    )
                    .await
                    .map_err(database_error)?;
                Ok(Json(row.get(0)))
            })
        })
        .await
}
