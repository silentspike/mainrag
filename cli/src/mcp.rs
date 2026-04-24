//! Model Context Protocol (MCP) Server
//!
//! Simple MCP server that delegates to the MAINRAG API server.

use anyhow::Result;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use crate::client::ApiClient;

/// MCP Request
#[derive(Debug, Deserialize)]
struct McpRequest {
    #[allow(dead_code)]
    jsonrpc: String,
    id: Option<Value>,
    method: String,
    params: Option<Value>,
}

/// MCP Response
#[derive(Debug, Serialize)]
struct McpResponse {
    jsonrpc: String,
    id: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<McpError>,
}

#[derive(Debug, Serialize)]
struct McpError {
    code: i32,
    message: String,
}

/// Tool definition
#[derive(Debug, Serialize)]
struct Tool {
    name: String,
    description: String,
    input_schema: Value,
}

/// MCP Server
pub struct McpServer {
    client: ApiClient,
}

impl McpServer {
    pub fn new(client: ApiClient) -> Self {
        Self { client }
    }

    /// Run MCP server on stdio
    pub async fn run_stdio(&self) -> Result<()> {
        let stdin = std::io::stdin();
        let mut stdout = std::io::stdout();
        let reader = BufReader::new(stdin.lock());

        for line in reader.lines() {
            let line = line?;
            if line.is_empty() {
                continue;
            }

            let request: McpRequest = match serde_json::from_str(&line) {
                Ok(r) => r,
                Err(e) => {
                    eprintln!("Invalid JSON: {}", e);
                    continue;
                }
            };

            let response = self.handle_request(request).await;

            let response_json = serde_json::to_string(&response)?;
            writeln!(stdout, "{}", response_json)?;
            stdout.flush()?;
        }

        Ok(())
    }

    /// Handle a single MCP request
    async fn handle_request(&self, request: McpRequest) -> McpResponse {
        let result = match request.method.as_str() {
            "initialize" => self.handle_initialize(),
            "tools/list" => self.handle_tools_list(),
            "tools/call" => self.handle_tools_call(request.params).await,
            _ => Err(McpError {
                code: -32601,
                message: format!("Unknown method: {}", request.method),
            }),
        };

        match result {
            Ok(r) => McpResponse {
                jsonrpc: "2.0".to_string(),
                id: request.id,
                result: Some(r),
                error: None,
            },
            Err(e) => McpResponse {
                jsonrpc: "2.0".to_string(),
                id: request.id,
                result: None,
                error: Some(e),
            },
        }
    }

    fn handle_initialize(&self) -> Result<Value, McpError> {
        Ok(json!({
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": "mainrag",
                "version": "0.1.0"
            },
            "capabilities": {
                "tools": {}
            }
        }))
    }

    fn handle_tools_list(&self) -> Result<Value, McpError> {
        let tools = vec![
            Tool {
                name: "mainrag_search".to_string(),
                description: "Semantic + keyword search across indexed code".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "query": { "type": "string", "description": "Search query" },
                        "limit": { "type": "number", "description": "Max results (default: 10)" },
                        "source": { "type": "string", "description": "Filter by source name" }
                    },
                    "required": ["query"]
                }),
            },
            Tool {
                name: "mainrag_find_symbols".to_string(),
                description: "Find function/class definitions".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "query": { "type": "string", "description": "Symbol name pattern" },
                        "type": { "type": "string", "description": "Filter by type (function, class, etc)" },
                        "source": { "type": "string", "description": "Filter by source name" }
                    },
                    "required": ["query"]
                }),
            },
            Tool {
                name: "mainrag_find_callers".to_string(),
                description: "Find all callers of a function".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "function": { "type": "string", "description": "Function name" },
                        "source": { "type": "string", "description": "Filter by source name" }
                    },
                    "required": ["function"]
                }),
            },
            Tool {
                name: "mainrag_find_callees".to_string(),
                description: "Find all functions called by a function".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "function": { "type": "string", "description": "Function name" },
                        "source": { "type": "string", "description": "Filter by source name" }
                    },
                    "required": ["function"]
                }),
            },
            Tool {
                name: "mainrag_browse_layers".to_string(),
                description: "Browse symbol cards by API layer, resource type, or side-effect.".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "layer": { "type": "string", "description": "Layer filter (controller_api, proxy, internal, etc.)" },
                        "resource": { "type": "string", "description": "Resource filter (clip, track, device, etc.)" },
                        "side_effect": { "type": "string", "description": "Side-effect filter (create, delete, get, etc.)" },
                        "limit": { "type": "number", "description": "Max results (default: 20)" }
                    }
                }),
            },
            Tool {
                name: "mainrag_get_ownership".to_string(),
                description: "Get ownership/containment relations for a symbol (who owns it, what it contains).".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "symbol": { "type": "string", "description": "Symbol or class name" }
                    },
                    "required": ["symbol"]
                }),
            },
            Tool {
                name: "mainrag_explore".to_string(),
                description: "Explore a concept: rewrites query, traces delegation chains, returns candidate paths + dead ends. Best for 'how do I...' questions.".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "query": { "type": "string", "description": "Natural language question" },
                        "source": { "type": "string", "description": "Source name filter" }
                    },
                    "required": ["query"]
                }),
            },
            Tool {
                name: "mainrag_get_symbol_card".to_string(),
                description: "Get enriched symbol card with layer, delegation, side effects, thread requirements and classification confidence.".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "name": { "type": "string", "description": "Symbol name" },
                        "source": { "type": "string", "description": "Filter by source name" }
                    },
                    "required": ["name"]
                }),
            },
            Tool {
                name: "mainrag_explain_path".to_string(),
                description: "Trace delegation chain from a symbol through proxy -> dispatch -> mutation. Shows code snippets and thread requirements.".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "symbol_name": { "type": "string", "description": "Symbol name to trace" },
                        "source": { "type": "string", "description": "Filter by source name" },
                        "max_depth": { "type": "number", "description": "Max chain depth (default: 6)" }
                    },
                    "required": ["symbol_name"]
                }),
            },
            Tool {
                name: "mainrag_report_dead_end".to_string(),
                description: "Report a known dead-end path to prevent repeating failed approaches.".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "concept": { "type": "string", "description": "What was attempted" },
                        "path_description": { "type": "string", "description": "The path that failed" },
                        "reason": { "type": "string", "description": "Why it fails" },
                        "symbols": { "type": "array", "items": { "type": "string" }, "description": "Involved symbols" }
                    },
                    "required": ["concept", "path_description", "reason"]
                }),
            },
        ];

        Ok(json!({ "tools": tools }))
    }

    async fn handle_tools_call(&self, params: Option<Value>) -> Result<Value, McpError> {
        let params = params.ok_or_else(|| McpError {
            code: -32602,
            message: "Missing params".to_string(),
        })?;

        let tool_name = params["name"].as_str().ok_or_else(|| McpError {
            code: -32602,
            message: "Missing tool name".to_string(),
        })?;

        let arguments = &params["arguments"];

        match tool_name {
            "mainrag_search" => self.tool_search(arguments).await,
            "mainrag_find_symbols" => self.tool_find_symbols(arguments).await,
            "mainrag_find_callers" => self.tool_find_callers(arguments).await,
            "mainrag_find_callees" => self.tool_find_callees(arguments).await,
            "mainrag_get_symbol_card" => self.tool_get_symbol_card(arguments).await,
            "mainrag_explain_path" => self.tool_explain_path(arguments).await,
            "mainrag_report_dead_end" => self.tool_report_dead_end(arguments).await,
            "mainrag_explore" => self.tool_explore(arguments).await,
            "mainrag_browse_layers" => self.tool_browse_layers(arguments).await,
            "mainrag_get_ownership" => self.tool_get_ownership(arguments).await,
            _ => Err(McpError {
                code: -32602,
                message: format!("Unknown tool: {}", tool_name),
            }),
        }
    }

    async fn tool_search(&self, args: &Value) -> Result<Value, McpError> {
        let query = args["query"].as_str().ok_or_else(|| McpError {
            code: -32602,
            message: "Missing query".to_string(),
        })?;
        let limit = args["limit"].as_u64().unwrap_or(10);
        let source = args["source"].as_str();

        let result = self.client.search(query, "hybrid", limit as u32, source)
            .await
            .map_err(|e| McpError {
                code: -32000,
                message: e.to_string(),
            })?;

        Ok(json!({
            "content": [{
                "type": "text",
                "text": serde_json::to_string_pretty(&result).unwrap_or_default()
            }]
        }))
    }

    async fn tool_find_symbols(&self, args: &Value) -> Result<Value, McpError> {
        let query = args["query"].as_str().ok_or_else(|| McpError {
            code: -32602,
            message: "Missing query".to_string(),
        })?;
        let symbol_type = args["type"].as_str();
        let limit = args["limit"].as_u64().unwrap_or(20) as u32;

        let result = self.client.search_symbols(query, symbol_type, limit)
            .await
            .map_err(|e| McpError {
                code: -32000,
                message: e.to_string(),
            })?;

        Ok(json!({
            "content": [{
                "type": "text",
                "text": serde_json::to_string_pretty(&result).unwrap_or_default()
            }]
        }))
    }

    async fn tool_find_callers(&self, args: &Value) -> Result<Value, McpError> {
        let function = args["function"].as_str().ok_or_else(|| McpError {
            code: -32602,
            message: "Missing function".to_string(),
        })?;

        let source = args.get("source").and_then(|v| v.as_str());

        let result = self.client.find_callers(function, source)
            .await
            .map_err(|e| McpError {
                code: -32000,
                message: e.to_string(),
            })?;

        if result.is_empty() {
            Ok(json!({
                "content": [{
                    "type": "text",
                    "text": format!("No callers found for function '{}'", function)
                }]
            }))
        } else {
            Ok(json!({
                "content": [{
                    "type": "text",
                    "text": serde_json::to_string_pretty(&result).unwrap_or_default()
                }]
            }))
        }
    }

    async fn tool_find_callees(&self, args: &Value) -> Result<Value, McpError> {
        let function = args["function"].as_str().ok_or_else(|| McpError {
            code: -32602,
            message: "Missing function".to_string(),
        })?;
        let source = args.get("source").and_then(|v| v.as_str());

        let result = self.client.find_callees(function, source)
            .await
            .map_err(|e| McpError {
                code: -32000,
                message: e.to_string(),
            })?;

        if result.is_empty() {
            Ok(json!({
                "content": [{
                    "type": "text",
                    "text": format!("No callees found for function '{}'", function)
                }]
            }))
        } else {
            Ok(json!({
                "content": [{
                    "type": "text",
                    "text": serde_json::to_string_pretty(&result).unwrap_or_default()
                }]
            }))
        }
    }

    async fn tool_browse_layers(&self, args: &Value) -> Result<Value, McpError> {
        // browse_layers uses the cards endpoint with filters
        let layer = args.get("layer").and_then(|v| v.as_str());
        let resource = args.get("resource").and_then(|v| v.as_str());
        let side_effect = args.get("side_effect").and_then(|v| v.as_str());

        // Build query params — pass "*" as name wildcard
        let mut url = format!("{}/api/v1/intelligence/cards?name=%25", self.client.base_url());
        if let Some(l) = layer { url.push_str(&format!("&layer={}", l)); }
        if let Some(r) = resource { url.push_str(&format!("&resource={}", r)); }
        if let Some(s) = side_effect { url.push_str(&format!("&side_effect={}", s)); }

        // Use get_symbol_cards with a wildcard — the API supports layer/resource/side_effect filters
        let cards = self.client.get_symbol_cards("%", None)
            .await
            .map_err(|e| McpError { code: -32000, message: e.to_string() })?;

        // Filter client-side if API doesn't support all params yet
        let filtered: Vec<_> = cards.into_iter().filter(|c| {
            layer.is_none_or(|l| c.layer.as_deref() == Some(l))
                && resource.is_none_or(|r| c.affected_resource.as_deref() == Some(r))
                && side_effect.is_none_or(|s| c.side_effect_type.as_deref() == Some(s))
        }).take(20).collect();

        Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string_pretty(&filtered).unwrap_or_default() }] }))
    }

    async fn tool_get_ownership(&self, args: &Value) -> Result<Value, McpError> {
        let symbol = args["symbol"].as_str().ok_or_else(|| McpError {
            code: -32602, message: "Missing symbol".to_string(),
        })?;

        // Call ownership endpoint via the API
        let url = format!("{}/api/v1/intelligence/ownership?symbol={}",
            self.client.base_url(), urlencoding::encode(symbol));
        let response = self.client.raw_get(&url)
            .await
            .map_err(|e| McpError { code: -32000, message: e.to_string() })?;

        Ok(json!({ "content": [{ "type": "text", "text": response }] }))
    }

    async fn tool_explore(&self, args: &Value) -> Result<Value, McpError> {
        let query = args["query"].as_str().ok_or_else(|| McpError {
            code: -32602, message: "Missing query".to_string(),
        })?;
        let source = args.get("source").and_then(|v| v.as_str());

        let result = self.client.explore(query, source)
            .await
            .map_err(|e| McpError { code: -32000, message: e.to_string() })?;

        // Return formatted text directly — structured for LLM consumption
        Ok(json!({ "content": [{ "type": "text", "text": result.formatted }] }))
    }

    async fn tool_get_symbol_card(&self, args: &Value) -> Result<Value, McpError> {
        let name = args["name"].as_str().ok_or_else(|| McpError {
            code: -32602,
            message: "Missing name".to_string(),
        })?;
        let source = args.get("source").and_then(|v| v.as_str());

        let cards = self.client.get_symbol_cards(name, source)
            .await
            .map_err(|e| McpError { code: -32000, message: e.to_string() })?;

        if cards.is_empty() {
            Ok(json!({ "content": [{ "type": "text", "text": format!("No symbol card found for '{}'", name) }] }))
        } else {
            Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string_pretty(&cards).unwrap_or_default() }] }))
        }
    }

    async fn tool_explain_path(&self, args: &Value) -> Result<Value, McpError> {
        let symbol_name = args["symbol_name"].as_str().ok_or_else(|| McpError {
            code: -32602,
            message: "Missing symbol_name".to_string(),
        })?;
        let source = args.get("source").and_then(|v| v.as_str());
        let max_depth = args.get("max_depth").and_then(|v| v.as_u64()).map(|d| d as u32);

        let chains = self.client.explain_path(symbol_name, source, max_depth)
            .await
            .map_err(|e| McpError { code: -32000, message: e.to_string() })?;

        if chains.is_empty() {
            Ok(json!({ "content": [{ "type": "text", "text": format!("No delegation chain found for '{}'", symbol_name) }] }))
        } else {
            Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string_pretty(&chains).unwrap_or_default() }] }))
        }
    }

    async fn tool_report_dead_end(&self, args: &Value) -> Result<Value, McpError> {
        let concept = args["concept"].as_str().ok_or_else(|| McpError {
            code: -32602, message: "Missing concept".to_string(),
        })?;
        let path_description = args["path_description"].as_str().ok_or_else(|| McpError {
            code: -32602, message: "Missing path_description".to_string(),
        })?;
        let reason = args["reason"].as_str().ok_or_else(|| McpError {
            code: -32602, message: "Missing reason".to_string(),
        })?;
        let symbols: Vec<String> = args.get("symbols")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().filter_map(|v| v.as_str().map(String::from)).collect())
            .unwrap_or_default();

        let id = self.client.create_negative_evidence(concept, path_description, reason, &symbols, None)
            .await
            .map_err(|e| McpError { code: -32000, message: e.to_string() })?;

        Ok(json!({ "content": [{ "type": "text", "text": format!("Dead-end recorded (id: {})", id) }] }))
    }
}
