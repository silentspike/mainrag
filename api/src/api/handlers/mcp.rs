//! MCP-compatible Handlers - Claude and LLM integration endpoints

use axum::{
    extract::State,
    http::StatusCode,
    Extension,
    Json,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::sync::Arc;
use uuid::Uuid;
use crate::api::JsonBody;
use crate::services::qdrant::TenantContext;
use crate::AppState;

#[derive(Debug, Serialize)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    pub input_schema: Value,
}

#[derive(Debug, Serialize)]
pub struct ToolsListResponse {
    pub tools: Vec<ToolDefinition>,
    pub version: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SearchQuery {
    pub query: String,
    pub search_type: Option<String>, // "hybrid", "semantic", "keyword"
    pub source_id: Option<i64>,
    pub limit: Option<u32>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct SymbolQuery {
    pub query: String,
    pub language: Option<String>,
    pub limit: Option<i64>,
}

#[allow(dead_code)]
#[derive(Debug, Serialize)]
pub struct McpToolResponse<T: Serialize> {
    pub result: T,
    pub success: bool,
}

/// Get list of available MCP tools
pub async fn list_mcp_tools(
    State(_state): State<Arc<AppState>>,
) -> Result<Json<ToolsListResponse>, StatusCode> {
    let tools = vec![
        ToolDefinition {
            name: "search_code".to_string(),
            description: "Search codebase with hybrid (semantic + keyword) search".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "query": { "type": "string", "description": "Search query" },
                    "search_type": { "type": "string", "enum": ["hybrid", "semantic", "keyword"] },
                    "source_id": { "type": "integer", "description": "Optional source ID filter" },
                    "limit": { "type": "integer", "minimum": 1, "maximum": 200 }
                },
                "required": ["query"]
            }),
        },
        ToolDefinition {
            name: "search_symbols".to_string(),
            description: "Search for function/class definitions by name".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "query": { "type": "string", "description": "Symbol name to search" },
                    "language": { "type": "string", "description": "Programming language filter" },
                    "limit": { "type": "integer", "minimum": 1, "maximum": 200 }
                },
                "required": ["query"]
            }),
        },
        ToolDefinition {
            name: "get_symbol_callgraph".to_string(),
            description: "Get call graph for a symbol (who calls it, what it calls)".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "symbol_id": { "type": "integer", "description": "Symbol ID" }
                },
                "required": ["symbol_id"]
            }),
        },
        ToolDefinition {
            name: "list_sources".to_string(),
            description: "List all indexed sources (repos/directories)".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {}
            }),
        },
        ToolDefinition {
            name: "get_source_stats".to_string(),
            description: "Get statistics for a source (file count, size, etc)".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "source_id": { "type": "integer", "description": "Source ID" }
                },
                "required": ["source_id"]
            }),
        },
        ToolDefinition {
            name: "find_callers".to_string(),
            description: "Find all functions that call a given function by name".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "function_name": { "type": "string", "description": "Name of the function to find callers for" },
                    "limit": { "type": "integer", "minimum": 1, "maximum": 200, "default": 50 }
                },
                "required": ["function_name"]
            }),
        },
        ToolDefinition {
            name: "find_callees".to_string(),
            description: "Find all functions called by a given function by name".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "function_name": { "type": "string", "description": "Name of the function to find callees for" },
                    "limit": { "type": "integer", "minimum": 1, "maximum": 200, "default": 50 }
                },
                "required": ["function_name"]
            }),
        },
        ToolDefinition {
            name: "get_symbol_card".to_string(),
            description: "Get enriched symbol card with layer, delegation, side effects, thread requirements. Returns classification confidence for obfuscated symbols.".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "name": { "type": "string", "description": "Symbol name to look up" },
                    "source": { "type": "string", "description": "Optional source name filter (e.g. 'bitwig6-decompiled')" }
                },
                "required": ["name"]
            }),
        },
        ToolDefinition {
            name: "explain_path".to_string(),
            description: "Trace delegation chain from a symbol through proxy -> dispatch -> mutation. Shows code snippets at call sites and thread requirements.".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "symbol_name": { "type": "string", "description": "Symbol name to trace" },
                    "source": { "type": "string", "description": "Optional source name filter" },
                    "max_depth": { "type": "integer", "minimum": 1, "maximum": 10, "default": 6 }
                },
                "required": ["symbol_name"]
            }),
        },
        ToolDefinition {
            name: "browse_layers".to_string(),
            description: "Browse symbol cards by API layer, resource type, or side-effect. Useful for understanding how a codebase is structured.".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "layer": { "type": "string", "description": "Filter by layer (e.g. 'controller_api', 'proxy', 'internal')" },
                    "resource": { "type": "string", "description": "Filter by resource (e.g. 'clip', 'track', 'device')" },
                    "side_effect": { "type": "string", "description": "Filter by side effect (e.g. 'create', 'delete', 'get')" },
                    "source": { "type": "string", "description": "Source name filter" },
                    "limit": { "type": "integer", "minimum": 1, "maximum": 100, "default": 20 }
                }
            }),
        },
        ToolDefinition {
            name: "get_ownership".to_string(),
            description: "Get ownership/containment relations for a symbol (who owns it, what it contains, what it wraps).".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "symbol": { "type": "string", "description": "Symbol or class name" },
                    "source": { "type": "string", "description": "Source name filter" }
                },
                "required": ["symbol"]
            }),
        },
        ToolDefinition {
            name: "explore".to_string(),
            description: "Explore a concept in the codebase. Rewrites your query into targeted searches, traces delegation chains, and returns candidate paths with dead-end warnings. Best for 'how do I...' questions.".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "query": { "type": "string", "description": "Natural language question about the codebase" },
                    "source": { "type": "string", "description": "Source name (e.g. 'bitwig6-decompiled')" }
                },
                "required": ["query"]
            }),
        },
        ToolDefinition {
            name: "report_dead_end".to_string(),
            description: "Report a known dead-end path. Prevents other agents from repeating the same failed approach.".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "concept": { "type": "string", "description": "What was being attempted (e.g. 'delete arranger clip')" },
                    "path_description": { "type": "string", "description": "The path that failed (e.g. 'clearTime')" },
                    "reason": { "type": "string", "description": "Why it fails" },
                    "symbols": { "type": "array", "items": { "type": "string" }, "description": "Involved symbol names" },
                    "source": { "type": "string", "description": "Source name" }
                },
                "required": ["concept", "path_description", "reason"]
            }),
        },
    ];

    Ok(Json(ToolsListResponse {
        tools,
        version: "1.0.0".to_string(),
    }))
}

/// Execute an MCP tool call (Claude calls this with tool name + params)
#[derive(Debug, Deserialize)]
pub struct ExecuteToolRequest {
    pub tool_name: String,
    pub params: Value,
}

#[derive(Debug, Serialize)]
pub struct ExecuteToolResponse {
    pub tool_name: String,
    pub result: Value,
    pub success: bool,
    pub error: Option<String>,
}

pub async fn execute_mcp_tool(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    JsonBody(req): JsonBody<ExecuteToolRequest>,
) -> Result<Json<ExecuteToolResponse>, StatusCode> {
    // Derive TenantContext from auth claims — agents see only their own data
    let tenant = if claims.is_admin {
        TenantContext::Admin
    } else {
        let user_id = Uuid::parse_str(&claims.sub)
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
        TenantContext::Agent { user_id }
    };
    let user_id_for_sql = Uuid::parse_str(&claims.sub)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    match req.tool_name.as_str() {
        "search_code" => {
            let search_req: SearchQuery = serde_json::from_value(req.params)
                .map_err(|_| StatusCode::BAD_REQUEST)?;

            let search_type = search_req.search_type.as_deref().unwrap_or("hybrid");
            let limit = search_req.limit.unwrap_or(50);

            let search_results = match search_type {
                "semantic" => {
                    state.search.semantic_search(
                        &search_req.query,
                        search_req.source_id,
                        limit,
                        &tenant,
                    ).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?.results
                },
                "keyword" => {
                    // FIX-1: Pass tenant context for RLS isolation
                    state.search.keyword_search(
                        &search_req.query,
                        search_req.source_id,
                        limit,
                        &tenant,
                    ).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?.results
                },
                _ => {
                    // K2: No agent_id available in MCP context
                    // K4: Use admin tenant context for MCP
                    state.search.hybrid_search(
                        &search_req.query,
                        search_req.source_id,
                        limit,
                        true,
                        None,
                        &tenant,
                    ).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?.results
                },
            };

            Ok(Json(ExecuteToolResponse {
                tool_name: "search_code".to_string(),
                result: serde_json::to_value(&search_results).unwrap_or_else(|_| json!([])),
                success: true,
                error: None,
            }))
        },
        "search_symbols" => {
            let sym_req: SymbolQuery = serde_json::from_value(req.params)
                .map_err(|_| StatusCode::BAD_REQUEST)?;
            let limit = sym_req.limit.unwrap_or(50).min(200);
            let search_pattern = format!("%{}%", sym_req.query);
            let is_admin = claims.is_admin;
            let uid = user_id_for_sql;

            state.rls_client.with_system(|txn| Box::pin(async move {
                let rows = if is_admin {
                    if let Some(lang) = sym_req.language {
                        txn.query(
                            "SELECT s.id, s.name, s.type, f.path, s.line_start
                             FROM symbols s
                             JOIN files f ON s.file_id = f.id
                             WHERE s.name ILIKE $1 AND f.language = $2
                             LIMIT $3",
                            &[&search_pattern, &lang, &limit],
                        ).await
                    } else {
                        txn.query(
                            "SELECT s.id, s.name, s.type, f.path, s.line_start
                             FROM symbols s
                             JOIN files f ON s.file_id = f.id
                             WHERE s.name ILIKE $1
                             LIMIT $2",
                            &[&search_pattern, &limit],
                        ).await
                    }
                } else if let Some(lang) = sym_req.language {
                    txn.query(
                        "SELECT s.id, s.name, s.type, f.path, s.line_start
                         FROM symbols s
                         JOIN files f ON s.file_id = f.id
                         JOIN sources src ON f.source_id = src.id
                         WHERE s.name ILIKE $1 AND f.language = $2 AND src.user_id = $3
                         LIMIT $4",
                        &[&search_pattern, &lang, &uid, &limit],
                    ).await
                } else {
                    txn.query(
                        "SELECT s.id, s.name, s.type, f.path, s.line_start
                         FROM symbols s
                         JOIN files f ON s.file_id = f.id
                         JOIN sources src ON f.source_id = src.id
                         WHERE s.name ILIKE $1 AND src.user_id = $2
                         LIMIT $3",
                        &[&search_pattern, &uid, &limit],
                    ).await
                }?;

                let symbols: Vec<Value> = rows.iter().map(|row| {
                    json!({
                        "id": row.get::<_, i64>(0),
                        "name": row.get::<_, String>(1),
                        "type": row.get::<_, String>(2),
                        "file": row.get::<_, String>(3),
                        "line": row.get::<_, i32>(4),
                    })
                }).collect();

                Ok(Json(ExecuteToolResponse {
                    tool_name: "search_symbols".to_string(),
                    result: json!(symbols),
                    success: true,
                    error: None,
                }))
            })).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
        },
        "list_sources" => {
            let is_admin = claims.is_admin;
            let uid = user_id_for_sql;
            state.rls_client.with_system(|txn| Box::pin(async move {
                let rows = if is_admin {
                    txn.query(
                        "SELECT id, name, type, path, file_count, total_size
                         FROM sources ORDER BY name ASC",
                        &[],
                    ).await?
                } else {
                    txn.query(
                        "SELECT id, name, type, path, file_count, total_size
                         FROM sources WHERE user_id = $1 ORDER BY name ASC",
                        &[&uid],
                    ).await?
                };

                let sources: Vec<Value> = rows.iter().map(|row| {
                    json!({
                        "id": row.get::<_, i64>(0),
                        "name": row.get::<_, String>(1),
                        "type": row.get::<_, String>(2),
                        "path": row.get::<_, String>(3),
                        "file_count": row.get::<_, i32>(4),
                        "total_size": row.get::<_, i64>(5),
                    })
                }).collect();

                Ok(Json(ExecuteToolResponse {
                    tool_name: "list_sources".to_string(),
                    result: json!(sources),
                    success: true,
                    error: None,
                }))
            })).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
        },
        "get_source_stats" => {
            #[derive(Debug, Deserialize)]
            struct SourceStatsParams {
                source_id: i64,
            }
            let params: SourceStatsParams = serde_json::from_value(req.params)
                .map_err(|_| StatusCode::BAD_REQUEST)?;
            let is_admin = claims.is_admin;
            let uid = user_id_for_sql;

            state.rls_client.with_system(|txn| Box::pin(async move {
                let row = if is_admin {
                    txn.query_opt(
                        "SELECT id, name, type, path, file_count, total_size, last_synced
                         FROM sources WHERE id = $1",
                        &[&params.source_id],
                    ).await?
                } else {
                    txn.query_opt(
                        "SELECT id, name, type, path, file_count, total_size, last_synced
                         FROM sources WHERE id = $1 AND user_id = $2",
                        &[&params.source_id, &uid],
                    ).await?
                };

                match row {
                    Some(r) => {
                        let stats = json!({
                            "id": r.get::<_, i64>(0),
                            "name": r.get::<_, String>(1),
                            "type": r.get::<_, String>(2),
                            "path": r.get::<_, String>(3),
                            "file_count": r.get::<_, i32>(4),
                            "total_size": r.get::<_, i64>(5),
                            "last_synced": r.get::<_, Option<chrono::DateTime<chrono::Utc>>>(6),
                        });
                        Ok(Json(ExecuteToolResponse {
                            tool_name: "get_source_stats".to_string(),
                            result: stats,
                            success: true,
                            error: None,
                        }))
                    },
                    None => {
                        Ok(Json(ExecuteToolResponse {
                            tool_name: "get_source_stats".to_string(),
                            result: json!(null),
                            success: false,
                            error: Some("Source not found".to_string()),
                        }))
                    }
                }
            })).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
        },
        "get_symbol_callgraph" => {
            #[derive(Debug, Deserialize)]
            struct CallgraphParams {
                symbol_id: i64,
            }
            let params: CallgraphParams = serde_json::from_value(req.params)
                .map_err(|_| StatusCode::BAD_REQUEST)?;
            let is_admin = claims.is_admin;
            let uid = user_id_for_sql;

            state.rls_client.with_system(|txn| Box::pin(async move {
                let symbol_row = if is_admin {
                    txn.query_opt(
                        "SELECT s.id, s.name, s.type, f.path, s.line_start, s.line_end, s.context
                         FROM symbols s
                         JOIN files f ON s.file_id = f.id
                         WHERE s.id = $1",
                        &[&params.symbol_id],
                    ).await?
                } else {
                    txn.query_opt(
                        "SELECT s.id, s.name, s.type, f.path, s.line_start, s.line_end, s.context
                         FROM symbols s
                         JOIN files f ON s.file_id = f.id
                         JOIN sources src ON f.source_id = src.id
                         WHERE s.id = $1 AND src.user_id = $2",
                        &[&params.symbol_id, &uid],
                    ).await?
                };

                match symbol_row {
                    Some(sym) => {
                        let callers = txn.query(
                            "SELECT DISTINCT s.id, s.name, s.type, f.path, s.line_start
                             FROM call_graph cg
                             JOIN symbols s ON cg.caller_symbol_id = s.id
                             JOIN files f ON s.file_id = f.id
                             WHERE cg.callee_symbol_id = $1",
                            &[&params.symbol_id],
                        ).await?;

                        let caller_list: Vec<Value> = callers.iter().map(|r| {
                            json!({
                                "id": r.get::<_, i64>(0),
                                "name": r.get::<_, String>(1),
                                "type": r.get::<_, String>(2),
                                "file": r.get::<_, String>(3),
                                "line": r.get::<_, i32>(4),
                            })
                        }).collect();

                        let callees = txn.query(
                            "SELECT DISTINCT callee_name FROM call_graph
                             WHERE caller_symbol_id = $1",
                            &[&params.symbol_id],
                        ).await?;

                        let callee_list: Vec<String> = callees.iter()
                            .map(|r| r.get(0))
                            .collect();

                        let result = json!({
                            "symbol": {
                                "id": sym.get::<_, i64>(0),
                                "name": sym.get::<_, String>(1),
                                "type": sym.get::<_, String>(2),
                                "file": sym.get::<_, String>(3),
                                "line_start": sym.get::<_, i32>(4),
                                "line_end": sym.get::<_, i32>(5),
                                "context": sym.get::<_, Option<String>>(6),
                            },
                            "callers": caller_list,
                            "callees": callee_list,
                        });

                        Ok(Json(ExecuteToolResponse {
                            tool_name: "get_symbol_callgraph".to_string(),
                            result,
                            success: true,
                            error: None,
                        }))
                    },
                    None => {
                        Ok(Json(ExecuteToolResponse {
                            tool_name: "get_symbol_callgraph".to_string(),
                            result: json!(null),
                            success: false,
                            error: Some("Symbol not found".to_string()),
                        }))
                    }
                }
            })).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
        },
        "find_callers" => {
            #[derive(Debug, Deserialize)]
            struct FindCallersParams {
                function_name: String,
                limit: Option<i64>,
            }
            let params: FindCallersParams = serde_json::from_value(req.params)
                .map_err(|_| StatusCode::BAD_REQUEST)?;
            let limit = params.limit.unwrap_or(50).min(200);
            let search_pattern = format!("%{}%", params.function_name);
            let is_admin = claims.is_admin;
            let uid = user_id_for_sql;

            let callers = state.rls_client.with_system(|txn| Box::pin(async move {
                let rows = if is_admin {
                    txn.query(
                        "SELECT DISTINCT s.id, s.name, s.type, f.path, s.line_start, cg.callee_name
                         FROM call_graph cg
                         JOIN symbols s ON cg.caller_symbol_id = s.id
                         JOIN files f ON s.file_id = f.id
                         WHERE cg.callee_name ILIKE $1
                         ORDER BY s.name
                         LIMIT $2",
                        &[&search_pattern, &limit],
                    ).await?
                } else {
                    txn.query(
                        "SELECT DISTINCT s.id, s.name, s.type, f.path, s.line_start, cg.callee_name
                         FROM call_graph cg
                         JOIN symbols s ON cg.caller_symbol_id = s.id
                         JOIN files f ON s.file_id = f.id
                         JOIN sources src ON f.source_id = src.id
                         WHERE cg.callee_name ILIKE $1 AND src.user_id = $2
                         ORDER BY s.name
                         LIMIT $3",
                        &[&search_pattern, &uid, &limit],
                    ).await?
                };

                let callers: Vec<Value> = rows.iter().map(|row| {
                    json!({
                        "caller_id": row.get::<_, i64>(0),
                        "caller_name": row.get::<_, String>(1),
                        "caller_type": row.get::<_, String>(2),
                        "file_path": row.get::<_, String>(3),
                        "line": row.get::<_, i32>(4),
                        "calls_function": row.get::<_, String>(5),
                    })
                }).collect();

                Ok(callers)
            })).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

            Ok(Json(ExecuteToolResponse {
                tool_name: "find_callers".to_string(),
                result: json!({
                    "function_name": params.function_name,
                    "callers": callers,
                    "count": callers.len()
                }),
                success: true,
                error: None,
            }))
        },
        "find_callees" => {
            #[derive(Debug, Deserialize)]
            struct FindCalleesParams {
                function_name: String,
                limit: Option<i64>,
            }
            let params: FindCalleesParams = serde_json::from_value(req.params)
                .map_err(|_| StatusCode::BAD_REQUEST)?;
            let limit = params.limit.unwrap_or(50).min(200);
            let search_pattern = format!("%{}%", params.function_name);
            let is_admin = claims.is_admin;
            let uid = user_id_for_sql;

            let callees = state.rls_client.with_system(|txn| Box::pin(async move {
                let rows = if is_admin {
                    txn.query(
                        "SELECT DISTINCT cg.callee_name, s.name as caller_name, f.path, cg.call_line
                         FROM call_graph cg
                         JOIN symbols s ON cg.caller_symbol_id = s.id
                         JOIN files f ON s.file_id = f.id
                         WHERE s.name ILIKE $1
                         ORDER BY cg.callee_name
                         LIMIT $2",
                        &[&search_pattern, &limit],
                    ).await?
                } else {
                    txn.query(
                        "SELECT DISTINCT cg.callee_name, s.name as caller_name, f.path, cg.call_line
                         FROM call_graph cg
                         JOIN symbols s ON cg.caller_symbol_id = s.id
                         JOIN files f ON s.file_id = f.id
                         JOIN sources src ON f.source_id = src.id
                         WHERE s.name ILIKE $1 AND src.user_id = $2
                         ORDER BY cg.callee_name
                         LIMIT $3",
                        &[&search_pattern, &uid, &limit],
                    ).await?
                };

                let callees: Vec<Value> = rows.iter().map(|row| {
                    json!({
                        "callee_name": row.get::<_, String>(0),
                        "called_by": row.get::<_, String>(1),
                        "file_path": row.get::<_, String>(2),
                        "call_line": row.get::<_, Option<i32>>(3),
                    })
                }).collect();

                Ok(callees)
            })).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

            Ok(Json(ExecuteToolResponse {
                tool_name: "find_callees".to_string(),
                result: json!({
                    "function_name": params.function_name,
                    "callees": callees,
                    "count": callees.len()
                }),
                success: true,
                error: None,
            }))
        },
        "get_symbol_card" => {
            let name = req.params["name"].as_str().ok_or(StatusCode::BAD_REQUEST)?;
            let source_name = req.params.get("source").and_then(|v| v.as_str());

            // Resolve source name to source_id
            let source_id = if let Some(sn) = source_name {
                let sn_owned = sn.to_string();
                state.rls_client.with_system(|txn| Box::pin(async move {
                    let row = txn.query_opt("SELECT id FROM sources WHERE name = $1", &[&sn_owned]).await?;
                    Ok(row.map(|r| r.get::<_, i64>(0)))
                })).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
            } else {
                None
            };

            let cards = state.intelligence.search_symbol_cards(name, source_id, None, None, None, 10)
                .await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

            Ok(Json(ExecuteToolResponse {
                tool_name: "get_symbol_card".to_string(),
                result: serde_json::to_value(&cards).unwrap_or(json!([])),
                success: true,
                error: None,
            }))
        },
        "explain_path" => {
            let symbol_name = req.params["symbol_name"].as_str().ok_or(StatusCode::BAD_REQUEST)?;
            let max_depth = req.params.get("max_depth").and_then(|v| v.as_u64()).unwrap_or(6) as u32;
            let source_name = req.params.get("source").and_then(|v| v.as_str());

            let source_id = if let Some(sn) = source_name {
                let sn_owned = sn.to_string();
                state.rls_client.with_system(|txn| Box::pin(async move {
                    let row = txn.query_opt("SELECT id FROM sources WHERE name = $1", &[&sn_owned]).await?;
                    Ok(row.map(|r| r.get::<_, i64>(0)))
                })).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
            } else {
                None
            };

            let chains = state.intelligence.trace_delegation_chain(symbol_name, source_id, max_depth)
                .await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

            Ok(Json(ExecuteToolResponse {
                tool_name: "explain_path".to_string(),
                result: serde_json::to_value(&chains).unwrap_or(json!([])),
                success: true,
                error: None,
            }))
        },
        "browse_layers" => {
            let layer = req.params.get("layer").and_then(|v| v.as_str());
            let resource = req.params.get("resource").and_then(|v| v.as_str());
            let side_effect = req.params.get("side_effect").and_then(|v| v.as_str());
            let limit = req.params.get("limit").and_then(|v| v.as_i64()).unwrap_or(20) as i32;

            let cards = state.intelligence.search_symbol_cards(
                "%", None, layer, resource, side_effect, limit,
            ).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

            Ok(Json(ExecuteToolResponse {
                tool_name: "browse_layers".to_string(),
                result: serde_json::to_value(&cards).unwrap_or(json!([])),
                success: true,
                error: None,
            }))
        },
        "get_ownership" => {
            let symbol = req.params["symbol"].as_str().ok_or(StatusCode::BAD_REQUEST)?;

            let results = state.intelligence.get_ownership(symbol, None)
                .await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

            Ok(Json(ExecuteToolResponse {
                tool_name: "get_ownership".to_string(),
                result: serde_json::to_value(&results).unwrap_or(json!([])),
                success: true,
                error: None,
            }))
        },
        "explore" => {
            let query = req.params["query"].as_str().ok_or(StatusCode::BAD_REQUEST)?;
            let source = req.params.get("source").and_then(|v| v.as_str());

            let result = state.intelligence.explore(
                query, source, state.domain_registry.as_ref(),
            ).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

            // Return formatted text for LLM, not raw JSON
            Ok(Json(ExecuteToolResponse {
                tool_name: "explore".to_string(),
                result: json!({"formatted": result.formatted, "paths_count": result.candidate_paths.len()}),
                success: true,
                error: None,
            }))
        },
        "report_dead_end" => {
            let concept = req.params["concept"].as_str().ok_or(StatusCode::BAD_REQUEST)?;
            let path_description = req.params["path_description"].as_str().ok_or(StatusCode::BAD_REQUEST)?;
            let reason = req.params["reason"].as_str().ok_or(StatusCode::BAD_REQUEST)?;
            let symbols = req.params.get("symbols").cloned().unwrap_or(json!([]));

            let id = state.intelligence.create_negative_evidence(
                None, None, concept, path_description, reason, &symbols, "warning", Some("mcp"),
            ).await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

            Ok(Json(ExecuteToolResponse {
                tool_name: "report_dead_end".to_string(),
                result: json!({"id": id, "stored": true}),
                success: true,
                error: None,
            }))
        },
        _ => {
            Ok(Json(ExecuteToolResponse {
                tool_name: req.tool_name,
                result: json!(null),
                success: false,
                error: Some("Unknown tool".to_string()),
            }))
        },
    }
}

/// Get MCP protocol information (for Claude integration metadata)
#[derive(Debug, Serialize)]
pub struct McpProtocolInfo {
    pub version: String,
    pub name: String,
    pub description: String,
    pub capabilities: Vec<String>,
}

pub async fn get_mcp_protocol_info(
    State(_state): State<Arc<AppState>>,
) -> Result<Json<McpProtocolInfo>, StatusCode> {
    Ok(Json(McpProtocolInfo {
        version: "1.0.0".to_string(),
        name: "MainRAG MCP Server".to_string(),
        description: "Model Context Protocol integration for Claude and LLMs".to_string(),
        capabilities: vec![
            "search_code".to_string(),
            "search_symbols".to_string(),
            "call_graph".to_string(),
            "list_sources".to_string(),
            "get_stats".to_string(),
        ],
    }))
}
