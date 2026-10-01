//! Actor-scoped adapters for bounded active symbol and call inspection.

use crate::{auth::Claims, error::AppError, AppState};
use axum::{http::StatusCode, response::IntoResponse};
use serde_json::{json, Value};
use std::sync::Arc;
use uuid::Uuid;

pub(super) fn error_status(error: AppError) -> StatusCode {
    error.into_response().status()
}

pub(super) async fn resolve_source_name(
    state: &Arc<AppState>,
    claims: &Arc<Claims>,
    source: Option<&Value>,
) -> Result<Option<i64>, StatusCode> {
    let Some(source) = source.filter(|value| !value.is_null()) else {
        return Ok(None);
    };
    let name = source
        .as_str()
        .filter(|name| !name.trim().is_empty())
        .ok_or(StatusCode::BAD_REQUEST)?
        .to_owned();
    let uid = Uuid::parse_str(&claims.sub).map_err(|_| StatusCode::UNAUTHORIZED)?;
    let row = state
        .rls_client
        .with_rls(uid, claims.is_admin, move |txn| {
            Box::pin(async move {
                Ok(txn
                    .query_opt(
                        "SELECT id FROM sources WHERE name=$1 AND NOT is_test",
                        &[&name],
                    )
                    .await?)
            })
        })
        .await
        .map_err(error_status)?
        .ok_or(StatusCode::NOT_FOUND)?;
    Ok(Some(row.get(0)))
}

pub(super) async fn envelope(
    state: &Arc<AppState>,
    claims: &Arc<Claims>,
    command: &str,
    query: Value,
    source_id: Option<i64>,
) -> Result<Value, StatusCode> {
    let manifest = state
        .config
        .server
        .storage_v2_default_read_manifest_sha256
        .clone()
        .ok_or(StatusCode::CONFLICT)?;
    let uid = Uuid::parse_str(&claims.sub).map_err(|_| StatusCode::UNAUTHORIZED)?;
    let command = command.to_owned();
    let value = state
        .rls_client
        .with_rls(uid, claims.is_admin, move |txn| {
            Box::pin(async move {
                let row = txn
                    .query_one(
                        "SELECT storage_v2_active_intelligence_command($1,$2,$3,$4,false)",
                        &[&manifest, &command, &query, &source_id],
                    )
                    .await
                    .map_err(database_error)?;
                Ok(row.get::<_, Value>(0))
            })
        })
        .await
        .map_err(error_status)?;
    Ok(value)
}

pub(super) async fn rows(
    state: &Arc<AppState>,
    claims: &Arc<Claims>,
    command: &str,
    query: Value,
    source_id: Option<i64>,
) -> Result<Vec<Value>, StatusCode> {
    let value = envelope(state, claims, command, query, source_id).await?;
    let envelopes = value
        .get("results")
        .and_then(Value::as_array)
        .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    let mut rows = Vec::new();
    for envelope in envelopes {
        let values = envelope
            .get("value")
            .and_then(Value::as_array)
            .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
        rows.extend(values.iter().cloned());
    }
    Ok(rows)
}

pub(super) fn delegation_chains(
    value: &Value,
) -> Result<Vec<crate::db::models::DelegationChain>, StatusCode> {
    use crate::db::models::{DelegationChain, DelegationStep};
    let results = value["results"]
        .as_array()
        .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
    let complete = results
        .iter()
        .all(|r| r["value"]["complete"].as_bool() == Some(true));
    let mut chains = Vec::new();
    for result in results {
        let paths = result["value"]["paths"]
            .as_array()
            .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;
        for path in paths {
            let root = &path["root"];
            let mut steps = Vec::new();
            for node in path["steps"]
                .as_array()
                .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?
            {
                let edge = &node["edge"];
                let role = node["card"]["layer"]
                    .as_str()
                    .filter(|s| *s != "unknown")
                    .or_else(|| edge["call_kind"].as_str())
                    .unwrap_or("call");
                steps.push(DelegationStep {
                    symbol: serde_json::from_value(node["card"].clone())
                        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?,
                    role: role.to_owned(),
                    dispatch_via: edge["evidence"]["dispatch_via"].as_str().map(str::to_owned),
                    code_snippet: edge["evidence"]["code_snippet"].as_str().map(str::to_owned),
                    step_annotations: serde_json::from_value(node["annotations"].clone())
                        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?,
                    call_evidence: Some(edge.clone()),
                    annotations_complete: node["annotations_complete"].as_bool(),
                });
            }
            chains.push(DelegationChain {
                entry_point:serde_json::from_value(root["card"].clone()).map_err(|_|StatusCode::INTERNAL_SERVER_ERROR)?,
                annotations:serde_json::from_value(root["annotations"].clone()).map_err(|_|StatusCode::INTERNAL_SERVER_ERROR)?,
                steps,
                termination:path["termination"].as_str().map(str::to_owned),
                terminal_evidence:path.get("terminal_evidence").cloned(),
                complete:Some(complete),
                annotations_complete:root["annotations_complete"].as_bool(),
                read_provenance:Some(json!({"read_path":value["read_path"],
                    "activation_manifest_sha256":value["activation_manifest_sha256"],
                    "source_id":result["source_id"],"generation_seq":result["value"]["generation_seq"],
                    "symbol_key":root["symbol"]["symbol_key"]})),
            });
        }
    }
    Ok(chains)
}

pub(super) async fn execute_mcp_symbols(
    state: &Arc<AppState>,
    claims: &Arc<Claims>,
    request: &super::mcp::ExecuteToolRequest,
) -> Result<Option<super::mcp::ExecuteToolResponse>, StatusCode> {
    if request.tool_name == "explore" {
        let query = request
            .params
            .get("query")
            .and_then(Value::as_str)
            .ok_or(StatusCode::BAD_REQUEST)?;
        let source = match request.params.get("source").filter(|v| !v.is_null()) {
            Some(value) => Some(value.as_str().ok_or(StatusCode::BAD_REQUEST)?),
            None => None,
        };
        let result = explore(state, claims, query, source).await?;
        return Ok(Some(super::mcp::ExecuteToolResponse {
            tool_name: request.tool_name.clone(),
            result: json!({"formatted":result.formatted,"paths_count":result.candidate_paths.len(),
                "read_provenance":result.read_provenance}),
            success: true,
            error: None,
        }));
    }
    if request.tool_name == "report_dead_end" {
        let text = |key: &str| {
            request
                .params
                .get(key)
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or(StatusCode::BAD_REQUEST)
        };
        let source_id = resolve_source_name(state, claims, request.params.get("source")).await?;
        let note = super::intelligence::CreateNegativeEvidenceRequest {
            source_id,
            concept: text("concept")?,
            path_description: text("path_description")?,
            reason: text("reason")?,
            symbols: request
                .params
                .get("symbols")
                .cloned()
                .unwrap_or_else(|| json!([])),
            severity: "warning".to_owned(),
            created_by: Some("mcp".to_owned()),
        };
        let id = create_note(state, claims, &note).await?;
        return Ok(Some(super::mcp::ExecuteToolResponse {
            tool_name: request.tool_name.clone(),
            result: json!({"id":id,"stored":true}),
            success: true,
            error: None,
        }));
    }
    if request.tool_name == "get_symbol_callgraph" {
        let id = request
            .params
            .get("symbol_id")
            .and_then(Value::as_i64)
            .ok_or(StatusCode::BAD_REQUEST)?;
        let mut graph = checked_read(state, claims, CheckedRead::Callgraph(id, 200)).await?;
        let symbol = &mut graph["symbol"];
        symbol["type"] = symbol["symbol_type"].clone();
        symbol["file"] = symbol["file_path"].clone();
        if let Some(callers) = graph["callers"].as_array_mut() {
            for caller in callers {
                caller["id"] = caller["symbol_id"].clone();
                caller["type"] = caller["symbol_type"].clone();
                caller["file"] = caller["file_path"].clone();
                caller["line"] = caller["line_start"].clone();
            }
        }
        return Ok(Some(super::mcp::ExecuteToolResponse {
            tool_name: request.tool_name.clone(),
            result: graph,
            success: true,
            error: None,
        }));
    }
    let (command, key) = match request.tool_name.as_str() {
        "search_symbols" => ("symbols", "query"),
        "find_callers" => ("callers", "function_name"),
        "find_callees" => ("callees", "function_name"),
        _ => return Ok(None),
    };
    let name = request
        .params
        .get(key)
        .and_then(Value::as_str)
        .filter(|name| !name.trim().is_empty())
        .ok_or(StatusCode::BAD_REQUEST)?;
    let limit = match request.params.get("limit").filter(|value| !value.is_null()) {
        None => 50,
        Some(value) => value
            .as_u64()
            .filter(|limit| (1..=200).contains(limit))
            .ok_or(StatusCode::BAD_REQUEST)?,
    };
    let language = match request
        .params
        .get("language")
        .filter(|value| !value.is_null())
    {
        Some(value) => Some(value.as_str().ok_or(StatusCode::BAD_REQUEST)?),
        None => None,
    };
    let source_id = resolve_source_name(state, claims, request.params.get("source")).await?;
    let values = rows(
        state,
        claims,
        command,
        json!({"name":name,"language":language,"limit":limit}),
        source_id,
    )
    .await?;
    let result = match command {
        "symbols" => Value::Array(
            values
                .into_iter()
                .map(|row| {
                    json!({
                        "id":row["id"],"name":row["name"],"type":row["symbol_type"],
                        "file":row["file_path"],"line":row["line_start"],
                        "file_id":row["file_id"],"symbol_key":row["symbol_key"],
                        "source_id":row["source_id"],"generation_seq":row["generation_seq"],
                        "identity_namespace":row["identity_namespace"],
                    })
                })
                .collect(),
        ),
        "callers" => {
            let callers: Vec<_> = values
                .into_iter()
                .map(|mut row| {
                    row["calls_function"] = row["callee_name"].clone();
                    row
                })
                .collect();
            json!({"function_name":name,"count":callers.len(),"callers":callers})
        }
        _ => {
            let callees: Vec<_> = values
                .into_iter()
                .map(|mut row| {
                    row["called_by"] = row["caller_name"].clone();
                    row
                })
                .collect();
            json!({"function_name":name,"count":callees.len(),"callees":callees})
        }
    };
    Ok(Some(super::mcp::ExecuteToolResponse {
        tool_name: request.tool_name.clone(),
        result,
        success: true,
        error: None,
    }))
}

pub(super) enum CheckedRead {
    Callgraph(i64, i64),
    FileSymbols(i64, i64),
    CalleeNames(String, Option<i64>, i64),
}

pub(super) async fn create_note(
    state: &Arc<AppState>,
    claims: &Arc<Claims>,
    request: &super::intelligence::CreateNegativeEvidenceRequest,
) -> Result<i64, StatusCode> {
    let manifest = state
        .config
        .server
        .storage_v2_default_read_manifest_sha256
        .clone()
        .ok_or(StatusCode::CONFLICT)?;
    let uid = Uuid::parse_str(&claims.sub).map_err(|_| StatusCode::UNAUTHORIZED)?;
    let record = json!({"source_id":request.source_id,"concept":request.concept,
        "path_description":request.path_description,"reason":request.reason,
        "symbols":request.symbols,"severity":request.severity,"created_by":request.created_by});
    state
        .rls_client
        .with_rls(uid, claims.is_admin, move |txn| {
            Box::pin(async move {
                let row = txn
                    .query_one(
                        "SELECT storage_v2_create_intelligence_note($1,$2)",
                        &[&manifest, &record],
                    )
                    .await
                    .map_err(database_error)?;
                Ok(row.get::<_, i64>(0))
            })
        })
        .await
        .map_err(error_status)
}

pub(super) async fn search_notes(
    state: &Arc<AppState>,
    claims: &Arc<Claims>,
    concept: &str,
    source_id: Option<i64>,
) -> Result<Vec<crate::db::models::NegativeEvidence>, StatusCode> {
    let manifest = state
        .config
        .server
        .storage_v2_default_read_manifest_sha256
        .clone()
        .ok_or(StatusCode::CONFLICT)?;
    let uid = Uuid::parse_str(&claims.sub).map_err(|_| StatusCode::UNAUTHORIZED)?;
    let concept = concept.to_owned();
    let value = state
        .rls_client
        .with_rls(uid, claims.is_admin, move |txn| {
            Box::pin(async move {
                let row = txn
                    .query_one(
                        "SELECT storage_v2_search_intelligence_notes($1,$2,$3)",
                        &[&manifest, &concept, &source_id],
                    )
                    .await
                    .map_err(database_error)?;
                Ok(row.get::<_, Value>(0))
            })
        })
        .await
        .map_err(error_status)?;
    serde_json::from_value(value).map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
}

pub(super) async fn explore(
    state: &Arc<AppState>,
    claims: &Arc<Claims>,
    query: &str,
    source: Option<&str>,
) -> Result<crate::db::models::ExploreResponse, StatusCode> {
    if query.trim().is_empty() || query.len() > 8192 {
        return Err(StatusCode::BAD_REQUEST);
    }
    let source_value = source.map(|s| json!(s));
    let source_id = resolve_source_name(state, claims, source_value.as_ref()).await?;
    let manifest = state
        .config
        .server
        .storage_v2_default_read_manifest_sha256
        .clone()
        .ok_or(StatusCode::CONFLICT)?;
    let uid = Uuid::parse_str(&claims.sub).map_err(|_| StatusCode::UNAUTHORIZED)?;
    let expansion = state
        .domain_registry
        .as_ref()
        .and_then(|registry| source.and_then(|source| registry.expand_query(query, source)));
    let intent = expansion.as_ref().and_then(|e| e.intent.clone());
    let domain = expansion.as_ref().map(|e| e.domain.clone());
    let mut queries = vec![query.to_owned()];
    let mut operations = Vec::new();
    if let Some(expansion) = expansion {
        for name in expansion.operation_symbols {
            operations.push(name.to_lowercase());
            queries.push(name);
        }
        queries.extend(expansion.symbol_expansions);
    }
    let mut seen = std::collections::HashSet::new();
    queries.retain(|q| !q.trim().is_empty() && seen.insert(q.clone()));
    let expansions_complete = queries.len() <= 12 && operations.len() <= 100;
    queries.truncate(12);
    operations.truncate(100);
    let request =
        json!({"concept":query,"queries":queries,"operation_symbols":operations,"intent":intent});
    let value = state
        .rls_client
        .with_rls(uid, claims.is_admin, move |txn| {
            Box::pin(async move {
                let row = txn
                    .query_one(
                        "SELECT storage_v2_active_explore($1,$2,$3)",
                        &[&manifest, &request, &source_id],
                    )
                    .await
                    .map_err(database_error)?;
                Ok(row.get::<_, Value>(0))
            })
        })
        .await
        .map_err(error_status)?;
    explore_response(query, source, intent, domain, value, expansions_complete)
}

fn explore_response(
    query: &str,
    source: Option<&str>,
    intent: Option<String>,
    domain: Option<String>,
    value: Value,
    expansions_complete: bool,
) -> Result<crate::db::models::ExploreResponse, StatusCode> {
    use crate::db::models::{CandidatePath, ExploreResponse, NegativeEvidence, SuggestedQuery};
    let negative: Vec<NegativeEvidence> =
        serde_json::from_value(value["negative_evidence"].clone())
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    let mut candidate_paths = Vec::new();
    for chain in delegation_chains(&value)? {
        let source_id = chain
            .read_provenance
            .as_ref()
            .and_then(|p| p["source_id"].as_i64());
        let key = chain
            .read_provenance
            .as_ref()
            .and_then(|p| p["symbol_key"].as_str());
        let mut reason = note_reason(&negative, &chain.entry_point, source_id, key);
        for step in &chain.steps {
            if reason.is_some() {
                break;
            }
            let key = step
                .call_evidence
                .as_ref()
                .and_then(|e| e["callee_symbol_key"].as_str());
            reason = note_reason(&negative, &step.symbol, source_id, key);
        }
        let card = &chain.entry_point;
        let confidence = match card.classification_confidence {
            Some(c) if c >= 0.8 => "high",
            Some(c) if c >= 0.5 => "medium",
            Some(_) => "low",
            None => "unknown",
        };
        candidate_paths.push(CandidatePath {
            rank: (candidate_paths.len() + 1) as u32,
            title: format!(
                "Via {} [{}]",
                card.name,
                card.layer.as_deref().unwrap_or("unknown")
            ),
            confidence: confidence.to_owned(),
            why_relevant: card.summary.clone(),
            why_might_not_work: reason,
            chain,
        });
    }
    let mut suggested = Vec::new();
    if let Some(first) = candidate_paths.first() {
        let source_option = source
            .map(|s| format!(" --source {}", shell_argument(s)))
            .unwrap_or_default();
        suggested.push(SuggestedQuery {
            query: format!(
                "mainrag call-graph {}{}",
                shell_argument(&first.chain.entry_point.name),
                source_option
            ),
            rationale: "Inspect callers and callees for this entry point".to_owned(),
        });
    }
    let mut formatted = crate::services::intelligence::format_explore_response(
        query,
        intent.as_deref(),
        domain.as_deref(),
        source,
        &candidate_paths,
        &negative,
        &suggested,
    );
    let complete =
        expansions_complete && value["cards_complete"] == true && value["chains_complete"] == true;
    if !complete {
        formatted.push_str("\nSearch or call traversal reached a limit; results are partial.\n");
    }
    Ok(ExploreResponse {
        query: query.to_owned(),
        intent,
        domain,
        candidate_paths,
        negative_evidence: negative,
        suggested_next: suggested,
        formatted,
        read_provenance: Some(json!({"read_path":value["read_path"],
            "activation_manifest_sha256":value["activation_manifest_sha256"],"complete":complete,
            "expansions_complete":expansions_complete,"cards_complete":value["cards_complete"],
            "chains_complete":value["chains_complete"],"card_rows_read":value["card_rows_read"],
            "chain_work":value["chain_work"]})),
    })
}

fn shell_argument(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

fn note_reason(
    notes: &[crate::db::models::NegativeEvidence],
    card: &crate::db::models::SymbolCard,
    source: Option<i64>,
    key: Option<&str>,
) -> Option<String> {
    notes
        .iter()
        .find(|note| {
            let Some(provenance) = note.read_provenance.as_ref() else {
                return false;
            };
            if provenance["source_id"]
                .as_i64()
                .is_some_and(|id| Some(id) != source)
            {
                return false;
            }
            let names: Vec<_> = note
                .symbols
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect();
            match provenance["symbols_namespace"].as_str() {
                Some("storage_v2_symbol_key") => key.is_some_and(|key| names.contains(&key)),
                Some("user_symbol_reference" | "legacy_symbol_reference") => {
                    note.path_description == card.name
                        || names.contains(&card.name.as_str())
                        || card
                            .qualified_name
                            .as_deref()
                            .is_some_and(|name| names.contains(&name))
                }
                _ => false,
            }
        })
        .map(|note| format!("KNOWN DEAD END: {}", note.reason))
}

fn database_error(error: tokio_postgres::Error) -> AppError {
    use tokio_postgres::error::SqlState;
    if error.code() == Some(&SqlState::INSUFFICIENT_PRIVILEGE) {
        AppError::Forbidden("active intelligence scope is not authorized".into())
    } else if error.code() == Some(&SqlState::INVALID_PARAMETER_VALUE) {
        AppError::BadRequest("invalid active intelligence request".into())
    } else if error.code() == Some(&SqlState::NO_DATA_FOUND) {
        AppError::NotFound("active object not found".into())
    } else {
        AppError::Database(error)
    }
}

pub(super) async fn checked_read(
    state: &Arc<AppState>,
    claims: &Arc<Claims>,
    request: CheckedRead,
) -> Result<Value, StatusCode> {
    let manifest = state
        .config
        .server
        .storage_v2_default_read_manifest_sha256
        .clone()
        .ok_or(StatusCode::CONFLICT)?;
    let uid = Uuid::parse_str(&claims.sub).map_err(|_| StatusCode::UNAUTHORIZED)?;
    state
        .rls_client
        .with_rls(uid, claims.is_admin, move |txn| {
            Box::pin(async move {
                let row = match request {
                    CheckedRead::Callgraph(id, limit) => {
                        txn.query_one(
                            "SELECT storage_v2_active_symbol_callgraph($1,$2,$3)",
                            &[&manifest, &id, &limit],
                        )
                        .await
                    }
                    CheckedRead::FileSymbols(id, limit) => {
                        txn.query_one(
                            "SELECT storage_v2_active_file_symbols($1,$2,$3)",
                            &[&manifest, &id, &limit],
                        )
                        .await
                    }
                    CheckedRead::CalleeNames(name, source, limit) => {
                        txn.query_one(
                            "SELECT storage_v2_active_callee_names($1,$2,$3,$4)",
                            &[&manifest, &name, &source, &limit],
                        )
                        .await
                    }
                }
                .map_err(database_error)?;
                Ok(row.get::<_, Value>(0))
            })
        })
        .await
        .map_err(error_status)
}

#[cfg(test)]
mod chain_tests {
    use super::*;

    #[test]
    fn explore_preserves_limits_and_does_not_invent_confidence_or_note_identity() {
        let card = json!({"symbol_id":-7,"name":"entry","symbol_type":"function",
            "file_path":"fixture.rs","line_start":1,"line_end":3,"source_name":"fixture"});
        let root = json!({"symbol":{"symbol_key":"entry-key"},"card":card,
            "annotations":[],"annotations_complete":true});
        let note = |source, namespace, symbols| {
            json!({"id":1,"concept":"fixture",
            "path_description":"different","reason":"wrong source or identity",
            "symbols":symbols,"severity":"warning","read_provenance":{
                "source_id":source,"symbols_namespace":namespace}})
        };
        let mut value = json!({"read_path":"storage_v2_active","activation_manifest_sha256":"fixture",
            "cards_complete":false,"chains_complete":false,"card_rows_read":10,"chain_work":1,
            "negative_evidence":[note(2,"user_symbol_reference",json!(["entry"])),
                note(1,"storage_v2_symbol_key",json!(["entry"]))],
            "results":[{"source_id":1,"value":{"generation_seq":2,"complete":false,
                "paths":[{"root":root,"steps":[],"termination":"result_limit"}]}}]});
        let result = explore_response("fixture", None, None, None, value.clone(), true).unwrap();
        assert_eq!(result.candidate_paths[0].confidence, "unknown");
        assert!(result.candidate_paths[0].why_might_not_work.is_none());
        assert_eq!(result.read_provenance.as_ref().unwrap()["complete"], false);
        assert!(result.formatted.contains("results are partial"));
        assert!(result.formatted.contains("Path ends: result_limit"));
        value["negative_evidence"] =
            json!([note(1, "storage_v2_symbol_key", json!(["entry-key"]))]);
        let result = explore_response("fixture", None, None, None, value, true).unwrap();
        assert!(result.candidate_paths[0].why_might_not_work.is_some());
        assert!(result.formatted.contains("Caution: KNOWN DEAD END"));
        assert_eq!(shell_argument("a'$HOME`x`"), "'a'\"'\"'$HOME`x`'");
    }

    #[test]
    fn typed_paths_preserve_unknown_confidence_and_terminal_call_evidence() {
        let card = json!({"symbol_id":-7,"name":"entry","symbol_type":"function",
            "file_path":"fixture.rs","line_start":1,"line_end":3,"source_name":"fixture"});
        let root = json!({"symbol":{"symbol_key":"entry-key"},"card":card,
            "annotations":[],"annotations_complete":true});
        let edge = json!({"proven":true,"call_kind":"call","callee_symbol_key":"target-key",
            "evidence":{"line":2,"code_snippet":"target();"}});
        let terminal =
            json!({"proven":false,"callee_name":"unknown","candidate_symbol_keys":["candidate"]});
        let result = json!({"read_path":"storage_v2_active","activation_manifest_sha256":"fixture-manifest",
            "results":[{"source_id":1,"value":{"generation_seq":2,"complete":false,
                "paths":[{"root":root,"steps":[{"card":card,"edge":edge,
                    "annotations":[{"annotation_type":"thread","value":"main","confidence":null,
                        "provenance":{"evidence":"parser"}}],"annotations_complete":true}],
                    "termination":"unresolved","terminal_evidence":terminal}]}}]});
        let chains = delegation_chains(&result).unwrap();
        assert_eq!(chains.len(), 1);
        assert_eq!(chains[0].complete, Some(false));
        assert_eq!(chains[0].termination.as_deref(), Some("unresolved"));
        assert_eq!(chains[0].terminal_evidence.as_ref(), Some(&terminal));
        assert_eq!(
            chains[0].steps[0].code_snippet.as_deref(),
            Some("target();")
        );
        assert_eq!(chains[0].steps[0].call_evidence.as_ref(), Some(&edge));
        assert!(chains[0].steps[0].step_annotations[0].confidence.is_none());
        assert_eq!(
            chains[0].steps[0].step_annotations[0].provenance.as_ref(),
            Some(&json!({"evidence":"parser"}))
        );
        assert!(delegation_chains(&json!({"results":[{"value":[]}]})).is_err());
    }
}
