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

    let start = Instant::now();
    let default_limit = state.config.server.search_default_limit.unwrap_or(20);
    let max_limit = state.config.server.search_max_limit.unwrap_or(100);
    let limit = req.limit.unwrap_or(default_limit).min(max_limit);

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
        HeaderValue::from_static(search_results.search_mode.header_value()),
    );

    Ok((
        headers,
        Json(SearchResponse {
            llm_context: SearchResponse::generate_llm_context(search_results.total, results.len()),
            results,
            total: search_results.total,
            took_ms,
            quality_tier: Some(tier.as_str().to_string()),
            reranked: Some(search_results.reranked),
            compression_ratio,
            expanded_query: search_results.expanded_query,
            expansion_terms: search_results.expansion_terms,
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

    let start = Instant::now();
    let default_limit = state.config.server.search_default_limit.unwrap_or(20);
    let max_limit = state.config.server.search_max_limit.unwrap_or(100);
    let limit = req.limit.unwrap_or(default_limit).min(max_limit);

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
        HeaderValue::from_static(search_results.search_mode.header_value()),
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
        }),
    ))
}
