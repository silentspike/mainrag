//! Code Intelligence Handlers - Symbol search and call graph queries

use crate::db::models::{
    DelegationChain, ExploreResponse, NegativeEvidence, OwnershipInfo, SymbolCard,
};
use crate::AppState;
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    Json,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::Arc;

#[derive(Debug, Serialize, Deserialize)]
pub struct SymbolSearchRequest {
    pub query: String,
    pub language: Option<String>,
    pub symbol_type: Option<String>,
    pub limit: Option<i64>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct SymbolInfo {
    pub id: i64,
    pub name: String,
    pub symbol_type: String,
    pub file_path: String,
    pub line_start: i32,
    pub line_end: i32,
    pub context: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct CallGraphNode {
    pub symbol_id: i64,
    pub name: String,
    pub symbol_type: String,
    pub file_path: String,
    pub line_start: i32,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct CallGraphResponse {
    pub symbol: SymbolInfo,
    pub callers: Vec<CallGraphNode>,
    pub callees: Vec<String>,
}

/// Search for symbols by name or type
pub async fn search_symbols(
    State(state): State<Arc<AppState>>,
    Query(req): Query<SymbolSearchRequest>,
) -> Result<Json<Vec<SymbolInfo>>, StatusCode> {
    let limit = req.limit.unwrap_or(50).min(200);
    let search_pattern = format!("%{}%", req.query);

    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let mut query = String::from(
            "SELECT s.id, s.name, s.type, f.path as file_path, s.line_start, s.line_end, s.context
             FROM symbols s
             JOIN files f ON s.file_id = f.id
             WHERE s.name ILIKE $1"
        );

                // Sprint 4.4: Parametrized LIMIT instead of format!()
                let limit_i64 = limit;
                let rows = if let Some(lang) = &req.language {
                    if let Some(sym_type) = &req.symbol_type {
                        query.push_str(" AND f.language = $2 AND s.type = $3 LIMIT $4");
                        txn.query(&query, &[&search_pattern, lang, sym_type, &limit_i64])
                            .await
                    } else {
                        query.push_str(" AND f.language = $2 LIMIT $3");
                        txn.query(&query, &[&search_pattern, lang, &limit_i64])
                            .await
                    }
                } else if let Some(sym_type) = &req.symbol_type {
                    query.push_str(" AND s.type = $2 LIMIT $3");
                    txn.query(&query, &[&search_pattern, sym_type, &limit_i64])
                        .await
                } else {
                    query.push_str(" LIMIT $2");
                    txn.query(&query, &[&search_pattern, &limit_i64]).await
                }?;

                let symbols = rows
                    .iter()
                    .map(|row| SymbolInfo {
                        id: row.get(0),
                        name: row.get(1),
                        symbol_type: row.get(2),
                        file_path: row.get(3),
                        line_start: row.get(4),
                        line_end: row.get(5),
                        context: row.get(6),
                    })
                    .collect();

                Ok(Json(symbols))
            })
        })
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

/// Get symbol details with call graph (who calls this, who it calls)
pub async fn get_symbol_callgraph(
    State(state): State<Arc<AppState>>,
    Path(symbol_id): Path<i64>,
) -> Result<Json<CallGraphResponse>, StatusCode> {
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let symbol_row = txn.query_one(
            "SELECT s.id, s.name, s.type, f.path as file_path, s.line_start, s.line_end, s.context
             FROM symbols s
             JOIN files f ON s.file_id = f.id
             WHERE s.id = $1",
            &[&symbol_id],
        ).await?;

                let symbol = SymbolInfo {
                    id: symbol_row.get(0),
                    name: symbol_row.get(1),
                    symbol_type: symbol_row.get(2),
                    file_path: symbol_row.get(3),
                    line_start: symbol_row.get(4),
                    line_end: symbol_row.get(5),
                    context: symbol_row.get(6),
                };

                let callers = txn
                    .query(
                        "SELECT DISTINCT s.id, s.name, s.type, f.path, s.line_start
             FROM call_graph cg
             JOIN symbols s ON cg.caller_symbol_id = s.id
             JOIN files f ON s.file_id = f.id
             WHERE cg.callee_symbol_id = $1",
                        &[&symbol_id],
                    )
                    .await?;

                let caller_nodes = callers
                    .iter()
                    .map(|row| CallGraphNode {
                        symbol_id: row.get(0),
                        name: row.get(1),
                        symbol_type: row.get(2),
                        file_path: row.get(3),
                        line_start: row.get(4),
                    })
                    .collect();

                let callees = txn
                    .query(
                        "SELECT DISTINCT callee_name FROM call_graph
             WHERE caller_symbol_id = $1",
                        &[&symbol_id],
                    )
                    .await?;

                let callee_names: Vec<String> = callees.iter().map(|row| row.get(0)).collect();

                Ok(Json(CallGraphResponse {
                    symbol,
                    callers: caller_nodes,
                    callees: callee_names,
                }))
            })
        })
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

/// List all symbols in a file
pub async fn list_file_symbols(
    State(state): State<Arc<AppState>>,
    Path(file_id): Path<i64>,
    Query(params): Query<std::collections::HashMap<String, String>>,
) -> Result<Json<Vec<SymbolInfo>>, StatusCode> {
    let limit = params
        .get("limit")
        .and_then(|s| s.parse::<i64>().ok())
        .unwrap_or(100)
        .min(200);

    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let rows = txn.query(
            "SELECT s.id, s.name, s.type, f.path as file_path, s.line_start, s.line_end, s.context
             FROM symbols s
             JOIN files f ON s.file_id = f.id
             WHERE s.file_id = $1
             ORDER BY s.line_start ASC
             LIMIT $2",
            &[&file_id, &limit],
        ).await?;

                let symbols = rows
                    .iter()
                    .map(|row| SymbolInfo {
                        id: row.get(0),
                        name: row.get(1),
                        symbol_type: row.get(2),
                        file_path: row.get(3),
                        line_start: row.get(4),
                        line_end: row.get(5),
                        context: row.get(6),
                    })
                    .collect();

                Ok(Json(symbols))
            })
        })
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

/// Find callers of a function by name (for MCP tools)
#[derive(Debug, Deserialize)]
pub struct CallerQuery {
    pub function: String,
    /// Optional source name filter (e.g. "internal-java-corpus")
    pub source: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct CallerInfo {
    pub name: String,
    pub file_path: String,
    pub line: i32,
}

pub async fn find_callers_by_name(
    State(state): State<Arc<AppState>>,
    Query(req): Query<CallerQuery>,
) -> Result<Json<Vec<CallerInfo>>, StatusCode> {
    let source = req.source.clone();
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let rows = if let Some(ref source_name) = source {
                    txn.query(
                        "SELECT DISTINCT s.name, f.path, cg.call_line
                 FROM call_graph cg
                 JOIN symbols s ON cg.caller_symbol_id = s.id
                 JOIN files f ON s.file_id = f.id
                 JOIN sources src ON f.source_id = src.id
                 WHERE cg.callee_name = $1 AND src.name = $2
                 ORDER BY f.path, cg.call_line
                 LIMIT 100",
                        &[&req.function, source_name],
                    )
                    .await?
                } else {
                    txn.query(
                        "SELECT DISTINCT s.name, f.path, cg.call_line
                 FROM call_graph cg
                 JOIN symbols s ON cg.caller_symbol_id = s.id
                 JOIN files f ON s.file_id = f.id
                 WHERE cg.callee_name = $1
                 ORDER BY f.path, cg.call_line
                 LIMIT 100",
                        &[&req.function],
                    )
                    .await?
                };

                let callers = rows
                    .iter()
                    .map(|row| CallerInfo {
                        name: row.get(0),
                        file_path: row.get(1),
                        line: row.get(2),
                    })
                    .collect();

                Ok(Json(callers))
            })
        })
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

/// Find callees (functions called by) a function by name (for MCP tools)
pub async fn find_callees_by_name(
    State(state): State<Arc<AppState>>,
    Query(req): Query<CallerQuery>,
) -> Result<Json<Vec<String>>, StatusCode> {
    let source = req.source.clone();
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let rows = if let Some(ref source_name) = source {
                    txn.query(
                        "SELECT DISTINCT cg.callee_name
                 FROM call_graph cg
                 JOIN symbols s ON cg.caller_symbol_id = s.id
                 JOIN files f ON s.file_id = f.id
                 JOIN sources src ON f.source_id = src.id
                 WHERE s.name = $1 AND src.name = $2
                 ORDER BY cg.callee_name
                 LIMIT 100",
                        &[&req.function, source_name],
                    )
                    .await?
                } else {
                    txn.query(
                        "SELECT DISTINCT cg.callee_name
                 FROM call_graph cg
                 JOIN symbols s ON cg.caller_symbol_id = s.id
                 WHERE s.name = $1
                 ORDER BY cg.callee_name
                 LIMIT 100",
                        &[&req.function],
                    )
                    .await?
                };

                let callees: Vec<String> = rows.iter().map(|row| row.get(0)).collect();

                Ok(Json(callees))
            })
        })
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

/// N-hop call chain traversal
#[derive(Debug, Deserialize)]
pub struct CallChainQuery {
    pub function: String,
    #[serde(default = "default_direction")]
    pub direction: String, // "callers" or "callees"
    #[serde(default = "default_depth")]
    pub depth: i32,
    pub source: Option<String>,
}
fn default_direction() -> String {
    "callees".to_string()
}
fn default_depth() -> i32 {
    3
}

pub async fn find_call_chain(
    State(state): State<Arc<AppState>>,
    Query(req): Query<CallChainQuery>,
) -> Result<Json<serde_json::Value>, StatusCode> {
    let source_id = if let Some(ref source_name) = req.source {
        state
            .rls_client
            .with_system(|txn| {
                let sn = source_name.clone();
                Box::pin(async move {
                    let row = txn
                        .query_opt("SELECT id FROM sources WHERE name = $1", &[&sn])
                        .await?;
                    Ok::<_, crate::error::AppError>(row.map(|r| r.get::<_, i64>(0)))
                })
            })
            .await
            .unwrap_or(None)
    } else {
        None
    };

    let chain = state
        .intelligence
        .find_call_chain(&req.function, &req.direction, req.depth, source_id)
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    Ok(Json(serde_json::json!({
        "function": req.function,
        "direction": req.direction,
        "depth": req.depth,
        "entries": chain,
    })))
}

// =============================================================================
// Intelligence Layer Handlers: Symbol Cards, Path Explanation, Negative Evidence
// =============================================================================

#[derive(Debug, Deserialize)]
pub struct SymbolCardQuery {
    pub name: Option<String>,
    pub source_id: Option<i64>,
    pub layer: Option<String>,
    pub resource: Option<String>,
    pub side_effect: Option<String>,
    pub limit: Option<i32>,
}

/// Browse/search symbol cards with optional filters.
pub async fn browse_symbol_cards(
    State(state): State<Arc<AppState>>,
    Query(req): Query<SymbolCardQuery>,
) -> Result<Json<Vec<SymbolCard>>, StatusCode> {
    let name = req.name.as_deref().unwrap_or("%");
    let limit = req.limit.unwrap_or(50).min(200);

    let cards = state
        .intelligence
        .search_symbol_cards(
            name,
            req.source_id,
            req.layer.as_deref(),
            req.resource.as_deref(),
            req.side_effect.as_deref(),
            limit,
        )
        .await
        .map_err(|e| {
            tracing::error!("browse_symbol_cards error: {}", e);
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    Ok(Json(cards))
}

/// Get a single symbol card by ID.
pub async fn get_symbol_card(
    State(state): State<Arc<AppState>>,
    Path(symbol_id): Path<i64>,
) -> Result<Json<Option<SymbolCard>>, StatusCode> {
    let card = state
        .intelligence
        .get_symbol_card(symbol_id)
        .await
        .map_err(|e| {
            tracing::error!("get_symbol_card error: {}", e);
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    Ok(Json(card))
}

#[derive(Debug, Deserialize)]
pub struct ExplainPathRequest {
    pub symbol_name: String,
    pub source_id: Option<i64>,
    pub max_depth: Option<u32>,
}

/// Trace delegation chain through proxy → dispatch → mutation.
pub async fn explain_path(
    State(state): State<Arc<AppState>>,
    Json(req): Json<ExplainPathRequest>,
) -> Result<Json<Vec<DelegationChain>>, StatusCode> {
    let max_depth = req.max_depth.unwrap_or(6);

    let chains = state
        .intelligence
        .trace_delegation_chain(&req.symbol_name, req.source_id, max_depth)
        .await
        .map_err(|e| {
            tracing::error!("explain_path error: {}", e);
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    Ok(Json(chains))
}

#[derive(Debug, Deserialize)]
pub struct CreateNegativeEvidenceRequest {
    pub source_id: Option<i64>,
    pub concept: String,
    pub path_description: String,
    pub reason: String,
    #[serde(default)]
    pub symbols: Value,
    #[serde(default = "default_severity")]
    pub severity: String,
    pub created_by: Option<String>,
}

fn default_severity() -> String {
    "warning".to_string()
}

#[derive(Debug, Serialize)]
pub struct CreateNegativeEvidenceResponse {
    pub id: i64,
}

/// Create a negative evidence entry (dead-end documentation).
pub async fn create_negative_evidence(
    State(state): State<Arc<AppState>>,
    Json(req): Json<CreateNegativeEvidenceRequest>,
) -> Result<Json<CreateNegativeEvidenceResponse>, StatusCode> {
    let id = state
        .intelligence
        .create_negative_evidence(
            req.source_id,
            None, // domain_profile resolved later from source_id
            &req.concept,
            &req.path_description,
            &req.reason,
            &req.symbols,
            &req.severity,
            req.created_by.as_deref(),
        )
        .await
        .map_err(|e| {
            tracing::error!("create_negative_evidence error: {}", e);
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    Ok(Json(CreateNegativeEvidenceResponse { id }))
}

#[derive(Debug, Deserialize)]
pub struct SearchNegativeEvidenceQuery {
    pub concept: String,
    pub source_id: Option<i64>,
}

/// Search negative evidence by concept.
pub async fn search_negative_evidence(
    State(state): State<Arc<AppState>>,
    Query(req): Query<SearchNegativeEvidenceQuery>,
) -> Result<Json<Vec<NegativeEvidence>>, StatusCode> {
    let results = state
        .intelligence
        .search_negative_evidence(&req.concept, req.source_id)
        .await
        .map_err(|e| {
            tracing::error!("search_negative_evidence error: {}", e);
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    Ok(Json(results))
}

#[derive(Debug, Deserialize)]
pub struct OwnershipQuery {
    pub symbol: String,
    pub source_id: Option<i64>,
}

/// Get ownership/containment relations for a symbol.
pub async fn get_ownership(
    State(state): State<Arc<AppState>>,
    Query(req): Query<OwnershipQuery>,
) -> Result<Json<Vec<OwnershipInfo>>, StatusCode> {
    let results = state
        .intelligence
        .get_ownership(&req.symbol, req.source_id)
        .await
        .map_err(|e| {
            tracing::error!("get_ownership error: {}", e);
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    Ok(Json(results))
}

#[derive(Debug, Deserialize)]
pub struct ExploreRequest {
    pub query: String,
    pub source: Option<String>,
}

/// Explore: orchestrated query combining domain rewriting, symbol search,
/// path tracing, and negative evidence.
pub async fn explore(
    State(state): State<Arc<AppState>>,
    Json(req): Json<ExploreRequest>,
) -> Result<Json<ExploreResponse>, StatusCode> {
    let result = state
        .intelligence
        .explore(
            &req.query,
            req.source.as_deref(),
            state.domain_registry.as_ref(),
        )
        .await
        .map_err(|e| {
            tracing::error!("explore error: {}", e);
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    Ok(Json(result))
}
