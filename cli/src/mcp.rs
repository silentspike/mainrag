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

        // For now, return a placeholder since we don't have a direct symbol search endpoint
        Ok(json!({
            "content": [{
                "type": "text",
                "text": format!("Symbol search for '{}' not yet implemented", query)
            }]
        }))
    }

    async fn tool_find_callers(&self, args: &Value) -> Result<Value, McpError> {
        let function = args["function"].as_str().ok_or_else(|| McpError {
            code: -32602,
            message: "Missing function".to_string(),
        })?;

        // For now, return a placeholder since we don't have a direct callers endpoint
        Ok(json!({
            "content": [{
                "type": "text",
                "text": format!("Callers search for '{}' not yet implemented", function)
            }]
        }))
    }

    async fn tool_find_callees(&self, args: &Value) -> Result<Value, McpError> {
        let function = args["function"].as_str().ok_or_else(|| McpError {
            code: -32602,
            message: "Missing function".to_string(),
        })?;

        // For now, return a placeholder since we don't have a direct callees endpoint
        Ok(json!({
            "content": [{
                "type": "text",
                "text": format!("Callees search for '{}' not yet implemented", function)
            }]
        }))
    }
}
