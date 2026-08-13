use axum::http::{HeaderMap, HeaderValue};
use axum::{extract::State, Extension, Json};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::Instant;
use uuid::Uuid;

use crate::api::JsonBody;
use crate::db::models::SearchResult;
use crate::error::{AppError, Result};
use crate::services::search::TenantContext;
use crate::services::{CompressorConfig, ContextualCompressor, QualityTier};
use crate::AppState;

#[derive(Debug, Deserialize)]
pub struct SearchRequest {
    pub query: String,
    pub source_id: Option<i64>,
    pub limit: Option<u32>,
    /// Quality tier: fast|balanced (default: balanced)
    pub quality: Option<String>,
    /// Enable contextual compression to reduce tokens (default: false)
    #[serde(default)]
    pub compress: bool,
    /// Explicit read selector. Omitted/current preserves the legacy path.
    pub read_path: Option<String>,
    /// Required when read_path=storage_v2; never inferred from the active pointer.
    pub generation: Option<String>,
    pub path_prefix: Option<String>,
    pub occurred_from: Option<String>,
    pub occurred_to: Option<String>,
    pub role: Option<String>,
    pub graph_profile: Option<String>,
    pub semantic_profile: Option<String>,
    pub rerank_profile: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct SearchResponse {
    /// LLM context explaining how to interpret the results
    pub llm_context: String,
    pub results: Vec<SearchResult>,
    pub total: usize,
    pub took_ms: u64,
    /// Quality tier used for this search
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quality_tier: Option<String>,
    /// Whether results were reranked
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reranked: Option<bool>,
    /// Compression ratio if compression was applied (0.0-1.0, lower = more compressed)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub compression_ratio: Option<f32>,
    /// Expanded FTS query with synonyms (if query expansion was applied)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expanded_query: Option<String>,
    /// Expansion terms found during query expansion
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub expansion_terms: Vec<String>,
    /// Actual selected read implementation.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub read_path: Option<String>,
    /// Named storage-v2 generation sequence, when selected.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub generation: Option<i64>,
    /// Complete views scored before the final result limit.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fully_scored_views: Option<i64>,
}

impl SearchResponse {
    /// Generate LLM context string explaining the search results
    fn generate_llm_context(total: usize, results_shown: usize) -> String {
        format!(
            "Found {} results (showing {}). \
            Ranked by hybrid score (FTS + vector + reranking). Higher score = more relevant. \
            [source] = knowledge base. For code: context shows file > class > function hierarchy. \
            Use `mainrag call-graph SYMBOL` for callers/callees. \
            Use `mainrag explore \"concept\"` for delegation chains.",
            total, results_shown
        )
    }
}

pub async fn hybrid_search(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    JsonBody(req): JsonBody<SearchRequest>,
) -> Result<(HeaderMap, Json<SearchResponse>)> {
    // Validate query is not empty
    let query = req.query.trim();
    if query.is_empty() {
        return Err(AppError::BadRequest("Query cannot be empty".to_string()));
    }

    // K3: RLS context is handled by SearchService internally.
    // The handler delegates all DB queries to state.search.* methods.

    // K4: Derive TenantContext from auth claims for Qdrant isolation
    let tenant = if claims.is_admin {
        TenantContext::Admin
    } else {
        let user_id = Uuid::parse_str(&claims.sub)
            .map_err(|_| AppError::Internal("Invalid user_id in claims".to_string()))?;
        TenantContext::Agent { user_id }
    };

    // Sprint 7.6: Get current search mode for response header
    let search_mode = state.search.search_mode();

    let start = Instant::now();
    let default_limit = state.config.server.search_default_limit.unwrap_or(20);
    let max_limit = state.config.server.search_max_limit.unwrap_or(100);
    let limit = req.limit.unwrap_or(default_limit).min(max_limit);

    if req.read_path.as_deref() == Some("storage_v2") {
        return storage_v2_search(state, claims, &req, query, limit, start).await;
    }
    validate_current_selector(&req)?;

    // Parse quality tier from request (fast = no rerank, balanced = with rerank)
    let tier = QualityTier::parse(req.quality.as_deref());
    let should_rerank = tier.should_rerank();

    // Execute hybrid search with optional reranking based on tier
    // K2: Pass agent/user ID for tenant-scoped cache keys
    // K4: Pass tenant context for Qdrant data isolation
    let search_results = state
        .search
        .hybrid_search(
            query,
            req.source_id,
            limit,
            should_rerank,
            Some(&claims.sub),
            &tenant,
        )
        .await?;

    // Apply contextual compression if requested
    let (results, compression_ratio) = if req.compress {
        let compressor = ContextualCompressor::new(CompressorConfig::default());
        let (compressed, ratio) = compressor.compress_results(search_results.results);
        (compressed, Some(ratio))
    } else {
        (search_results.results, None)
    };

    // Optimize results for LLM consumption (with query for snippet fallback)
    let results: Vec<_> = results
        .into_iter()
        .map(|r| r.optimize_for_llm_with_query(query))
        .collect();

    let took_ms = start.elapsed().as_millis() as u64;

    // Sprint 7.6: Set X-Search-Mode response header
    let mut headers = HeaderMap::new();
    headers.insert(
        "X-Search-Mode",
        HeaderValue::from_static(search_mode.header_value()),
    );

    Ok((
        headers,
        Json(SearchResponse {
            llm_context: SearchResponse::generate_llm_context(search_results.total, results.len()),
            results,
            total: search_results.total,
            took_ms,
            quality_tier: Some(tier.as_str().to_string()),
            reranked: Some(should_rerank),
            compression_ratio,
            expanded_query: search_results.expanded_query,
            expansion_terms: search_results.expansion_terms,
            read_path: Some("current".to_string()),
            generation: None,
            fully_scored_views: None,
        }),
    ))
}

pub async fn keyword_search(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    JsonBody(req): JsonBody<SearchRequest>,
) -> Result<(HeaderMap, Json<SearchResponse>)> {
    // Validate query is not empty
    let query = req.query.trim();
    if query.is_empty() {
        return Err(AppError::BadRequest("Query cannot be empty".to_string()));
    }

    // FIX-2: Derive TenantContext from auth claims (same pattern as hybrid_search)
    let tenant = if claims.is_admin {
        TenantContext::Admin
    } else {
        let user_id = Uuid::parse_str(&claims.sub)
            .map_err(|_| AppError::Internal("Invalid user_id in claims".to_string()))?;
        TenantContext::Agent { user_id }
    };

    // Sprint 7.6: Get current search mode for response header
    let search_mode = state.search.search_mode();

    let start = Instant::now();
    let default_limit = state.config.server.search_default_limit.unwrap_or(20);
    let max_limit = state.config.server.search_max_limit.unwrap_or(100);
    let limit = req.limit.unwrap_or(default_limit).min(max_limit);

    if req.read_path.as_deref() == Some("storage_v2") {
        return storage_v2_search(state, claims, &req, query, limit, start).await;
    }
    validate_current_selector(&req)?;

    // Keyword search always uses fast tier (no reranking)
    // FIX-1/FIX-2: Pass tenant context and use trimmed query
    let search_results = state
        .search
        .keyword_search(query, req.source_id, limit, &tenant)
        .await?;

    // Apply contextual compression if requested
    let (results, compression_ratio) = if req.compress {
        let compressor = ContextualCompressor::new(CompressorConfig::default());
        let (compressed, ratio) = compressor.compress_results(search_results.results);
        (compressed, Some(ratio))
    } else {
        (search_results.results, None)
    };

    // Optimize results for LLM consumption (with query for snippet fallback)
    let results: Vec<_> = results
        .into_iter()
        .map(|r| r.optimize_for_llm_with_query(query))
        .collect();

    let took_ms = start.elapsed().as_millis() as u64;

    // Sprint 7.6: Set X-Search-Mode response header
    let mut headers = HeaderMap::new();
    headers.insert(
        "X-Search-Mode",
        HeaderValue::from_static(search_mode.header_value()),
    );

    Ok((
        headers,
        Json(SearchResponse {
            llm_context: SearchResponse::generate_llm_context(search_results.total, results.len()),
            results,
            total: search_results.total,
            took_ms,
            quality_tier: Some("fast".to_string()),
            reranked: None, // Keyword search doesn't use reranking
            compression_ratio,
            expanded_query: None,
            expansion_terms: vec![],
            read_path: Some("current".to_string()),
            generation: None,
            fully_scored_views: None,
        }),
    ))
}

fn validate_current_selector(req: &SearchRequest) -> Result<()> {
    match req.read_path.as_deref().unwrap_or("current") {
        "current" => {}
        value => {
            return Err(AppError::BadRequest(format!(
                "unsupported search read path: {value}"
            )))
        }
    }
    if req.generation.is_some() {
        return Err(AppError::BadRequest(
            "generation requires read_path=storage_v2".to_string(),
        ));
    }
    if [
        req.path_prefix.as_ref(),
        req.occurred_from.as_ref(),
        req.occurred_to.as_ref(),
        req.role.as_ref(),
        req.graph_profile.as_ref(),
        req.semantic_profile.as_ref(),
        req.rerank_profile.as_ref(),
    ]
    .iter()
    .any(|value| value.is_some())
    {
        return Err(AppError::BadRequest(
            "storage-v2 filters require read_path=storage_v2".to_string(),
        ));
    }
    Ok(())
}

#[cfg(feature = "storage-v2-retrieval")]
async fn storage_v2_search(
    state: Arc<AppState>,
    claims: Arc<crate::auth::Claims>,
    req: &SearchRequest,
    query: &str,
    limit: u32,
    start: Instant,
) -> Result<(HeaderMap, Json<SearchResponse>)> {
    use crate::services::retrieval_v2::{
        parse_query, ExactRetrievalBackend, ExactSearchRequest, PostgresExactRetrievalBackend,
    };

    let source_id = req
        .source_id
        .ok_or_else(|| AppError::BadRequest("storage_v2 search requires source_id".to_string()))?;
    let generation = req
        .generation
        .as_deref()
        .filter(|value| {
            !value.is_empty()
                && !value.starts_with('0')
                && value.bytes().all(|byte| byte.is_ascii_digit())
        })
        .ok_or_else(|| {
            AppError::BadRequest(
                "storage_v2 search requires a positive named generation sequence".to_string(),
            )
        })?;
    if limit == 0 {
        return Err(AppError::BadRequest(
            "storage_v2 search limit must be positive".to_string(),
        ));
    }
    for (name, value) in [
        ("occurred_from", req.occurred_from.as_deref()),
        ("occurred_to", req.occurred_to.as_deref()),
    ] {
        if let Some(value) = value {
            chrono::DateTime::parse_from_rfc3339(value).map_err(|_| {
                AppError::BadRequest(format!("{name} must be an RFC3339 timestamp"))
            })?;
        }
    }
    let ast = parse_query(query)
        .map_err(|error| AppError::BadRequest(format!("invalid exact query: {error}")))?;
    let mut filters = serde_json::Map::new();
    for (name, value) in [
        ("path_prefix", req.path_prefix.as_deref()),
        ("occurred_from", req.occurred_from.as_deref()),
        ("occurred_to", req.occurred_to.as_deref()),
        ("role", req.role.as_deref()),
        ("graph_profile", req.graph_profile.as_deref()),
        ("semantic_profile", req.semantic_profile.as_deref()),
        ("rerank_profile", req.rerank_profile.as_deref()),
    ] {
        if let Some(value) = value.filter(|value| !value.is_empty()) {
            filters.insert(
                name.to_string(),
                serde_json::Value::String(value.to_string()),
            );
        }
    }
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".to_string()))?;
    let request = ExactSearchRequest {
        source_id,
        generation: generation.to_string(),
        ast,
        filters: serde_json::Value::Object(filters),
        limit: i64::from(limit),
    };
    let envelope = state
        .rls_client
        .with_rls(user_id, claims.is_admin, move |transaction| {
            Box::pin(async move {
                PostgresExactRetrievalBackend::new(transaction)
                    .search(&request)
                    .await
                    .map_err(|error| {
                        if let Some(database) = error.downcast_ref::<tokio_postgres::Error>() {
                            if database.code()
                                == Some(&tokio_postgres::error::SqlState::INSUFFICIENT_PRIVILEGE)
                            {
                                return AppError::Forbidden(
                                    "storage_v2 generation is not authorized".to_string(),
                                );
                            }
                        }
                        AppError::Internal(format!("storage_v2 exact retrieval failed: {error}"))
                    })
            })
        })
        .await?;

    let reranked = envelope.results.iter().any(|hit| {
        hit.score_explanation["rerank"]["status"] == serde_json::Value::String("available".into())
    });
    let mut results = Vec::with_capacity(envelope.results.len());
    for hit in envelope.results {
        let line_start = hit.locator["line_start"]
            .as_i64()
            .and_then(|value| i32::try_from(value).ok())
            .unwrap_or(0);
        let line_end = hit.locator["line_end"]
            .as_i64()
            .and_then(|value| i32::try_from(value).ok())
            .unwrap_or(line_start);
        let language = hit.locator["language"].as_str().map(str::to_string);
        let level = hit.locator["level"]
            .as_i64()
            .and_then(|value| i16::try_from(value).ok());
        let degradation = serde_json::json!({
            "graph": hit.score_explanation["graph"]["status"],
            "semantic": hit.score_explanation["semantic"]["status"],
            "rerank": hit.score_explanation["rerank"]["status"],
        });
        results.push(SearchResult {
            chunk_id: hit.occurrence_id,
            file_path: hit.source_path.clone(),
            content: hit.content,
            snippet: None,
            line_start,
            line_end,
            source_name: hit.source_name,
            language,
            score: hit.score as f32,
            context_prefix: Some(format!("[storage-v2] {}", hit.source_path)),
            location: None,
            chunk_type: Some(hit.role),
            level,
            parent_context: None,
            external_hit_id: Some(hit.external_hit_id),
            successor_metadata: if hit.legacy_successors.is_empty() {
                None
            } else {
                Some(
                    serde_json::to_value(hit.legacy_successors).map_err(|error| {
                        AppError::Internal(format!("serialize successor metadata: {error}"))
                    })?,
                )
            },
            score_explanation: Some(hit.score_explanation),
            degradation: Some(degradation),
        });
    }
    let (results, compression_ratio) = if req.compress {
        let compressor = ContextualCompressor::new(CompressorConfig::default());
        let (compressed, ratio) = compressor.compress_results(results);
        (compressed, Some(ratio))
    } else {
        (results, None)
    };
    let results = results
        .into_iter()
        .map(|result| result.optimize_for_llm_with_query(query))
        .collect::<Vec<_>>();
    let mut headers = HeaderMap::new();
    headers.insert(
        "X-Search-Mode",
        HeaderValue::from_static("storage-v2-exact"),
    );
    headers.insert("X-Search-Read-Path", HeaderValue::from_static("storage-v2"));
    Ok((
        headers,
        Json(SearchResponse {
            llm_context: format!(
                "Found {} exact storage-v2 results (showing {}) in named generation {}. \
                 Scores use complete occurrence-scoped evaluation; unavailable optional stages are explicit.",
                envelope.total,
                results.len(),
                envelope.generation_seq
            ),
            results,
            total: usize::try_from(envelope.total).unwrap_or(usize::MAX),
            took_ms: start.elapsed().as_millis() as u64,
            quality_tier: Some("exact".to_string()),
            reranked: Some(reranked),
            compression_ratio,
            expanded_query: None,
            expansion_terms: vec![],
            read_path: Some("storage_v2".to_string()),
            generation: Some(envelope.generation_seq),
            fully_scored_views: Some(envelope.fully_scored_views),
        }),
    ))
}

#[cfg(not(feature = "storage-v2-retrieval"))]
async fn storage_v2_search(
    _state: Arc<AppState>,
    _claims: Arc<crate::auth::Claims>,
    _req: &SearchRequest,
    _query: &str,
    _limit: u32,
    _start: Instant,
) -> Result<(HeaderMap, Json<SearchResponse>)> {
    Err(AppError::BadRequest(
        "storage_v2 retrieval is not enabled".to_string(),
    ))
}
