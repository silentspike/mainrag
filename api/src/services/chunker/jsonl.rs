//! Conversation Chunker for Claude Code (JSONL) / Codex (JSONL) / Gemini (JSON)
//!
//! Optimized for conversation files:
//! - Parses JSONL line-by-line (Claude, Codex) or JSON messages array (Gemini)
//! - Groups messages until target size
//! - Preserves conversation context (user/assistant pairs)
//! - Extracts metadata (role, tools, timestamps)

use super::{Chunk, ChunkType, Chunker, ChunkerConfig};
use serde_json::Value;

/// Target chunk size in characters (not tokens for speed)
const TARGET_CHUNK_SIZE: usize = 4000;

/// Minimum chunk size before creating new chunk
const MIN_CHUNK_SIZE: usize = 500;

/// Safely truncate a string at a valid UTF-8 character boundary
fn safe_truncate(s: &str, max_bytes: usize) -> &str {
    if s.len() <= max_bytes {
        return s;
    }
    // Find the last valid char boundary at or before max_bytes
    let mut end = max_bytes;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
}

pub struct JsonlChunker {
    target_size: usize,
    min_chunk_size: usize,
}

impl JsonlChunker {
    pub fn new(config: ChunkerConfig) -> Self {
        let target_size = config.max_tokens.map(|t| t * 4).unwrap_or(TARGET_CHUNK_SIZE);
        Self {
            target_size,
            // Adaptive min: never exceed half of target, otherwise splits become impossible
            min_chunk_size: MIN_CHUNK_SIZE.min(target_size / 2),
        }
    }

    /// Extract message content from Claude Code JSONL format
    fn extract_message_content(json: &Value) -> Option<(String, String)> {
        // Claude Code JSONL format:
        // {"type":"user","message":{"role":"user","content":[{"type":"text","text":"..."}]}}
        // {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"..."},{"type":"tool_use",...}]}}

        let msg_type = json.get("type").and_then(|t| t.as_str())?;

        // Skip non-message types (file-history-snapshot, etc.)
        if msg_type != "user" && msg_type != "assistant" && msg_type != "summary" {
            return None;
        }

        let message = json.get("message")?;
        let role = message.get("role").and_then(|r| r.as_str()).unwrap_or(msg_type);

        // Extract content from content array
        let content = message.get("content")?;
        let mut text_parts = Vec::new();

        if let Some(content_array) = content.as_array() {
            for item in content_array {
                if let Some(item_type) = item.get("type").and_then(|t| t.as_str()) {
                    match item_type {
                        "text" => {
                            if let Some(text) = item.get("text").and_then(|t| t.as_str()) {
                                text_parts.push(text.to_string());
                            }
                        }
                        "tool_use" => {
                            // Include tool calls in content
                            if let Some(name) = item.get("name").and_then(|n| n.as_str()) {
                                let input = item.get("input")
                                    .map(|i| serde_json::to_string_pretty(i).unwrap_or_default())
                                    .unwrap_or_default();
                                text_parts.push(format!("[tool:{}] {}", name,
                                    safe_truncate(&input, 200)));
                            }
                        }
                        "tool_result" => {
                            // Include tool results (truncated)
                            if let Some(content) = item.get("content").and_then(|c| c.as_str()) {
                                let preview = if content.len() > 500 {
                                    format!("{}...", safe_truncate(content, 500))
                                } else {
                                    content.to_string()
                                };
                                text_parts.push(format!("[result] {}", preview));
                            }
                        }
                        _ => {}
                    }
                }
            }
        } else if let Some(text) = content.as_str() {
            // Simple string content
            text_parts.push(text.to_string());
        }

        if text_parts.is_empty() {
            return None;
        }

        Some((role.to_string(), text_parts.join("\n")))
    }

    /// Extract content from Codex format (supports both old and new format)
    fn extract_codex_content(json: &Value) -> Option<(String, String)> {
        // Legacy format: {"session_id":"...","ts":123,"text":"..."}
        if let Some(text) = json.get("text").and_then(|t| t.as_str()) {
            if !text.trim().is_empty() {
                return Some(("message".to_string(), text.to_string()));
            }
        }

        // New Codex CLI format (2025+):
        // Messages:       {"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"..."}]}}
        // Tool calls:     {"type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"{...}"}}
        // Tool output:    {"type":"response_item","payload":{"type":"function_call_output","output":"..."}}
        // Reasoning:      {"type":"response_item","payload":{"type":"reasoning","summary":[{"text":"..."}]}}
        // Custom tools:   {"type":"response_item","payload":{"type":"custom_tool_call","name":"...","arguments":"..."}}
        let msg_type = json.get("type").and_then(|t| t.as_str())?;
        if msg_type != "response_item" {
            return None; // Skip session_meta, event_msg, turn_context
        }

        let payload = json.get("payload")?;
        let payload_type = payload.get("type").and_then(|t| t.as_str()).unwrap_or("");

        match payload_type {
            "message" => {
                // Regular user/assistant message
                let role = payload.get("role").and_then(|r| r.as_str()).unwrap_or("assistant");
                let content = payload.get("content")?;
                let mut text_parts = Vec::new();

                if let Some(arr) = content.as_array() {
                    for item in arr {
                        match item.get("type").and_then(|t| t.as_str()).unwrap_or("") {
                            "input_text" | "output_text" => {
                                if let Some(text) = item.get("text").and_then(|t| t.as_str()) {
                                    if !text.trim().is_empty() {
                                        text_parts.push(text.to_string());
                                    }
                                }
                            }
                            _ => {}
                        }
                    }
                } else if let Some(text) = content.as_str() {
                    if !text.trim().is_empty() {
                        text_parts.push(text.to_string());
                    }
                }

                if text_parts.is_empty() { return None; }
                Some((role.to_string(), text_parts.join("\n")))
            }
            "function_call" | "custom_tool_call" => {
                let name = payload.get("name").and_then(|n| n.as_str()).unwrap_or("unknown");
                let args = payload.get("arguments").and_then(|a| a.as_str()).unwrap_or("");
                if args.is_empty() { return None; }
                Some(("assistant".to_string(), format!("[tool:{}] {}", name, safe_truncate(args, 200))))
            }
            "function_call_output" | "custom_tool_call_output" => {
                let output = payload.get("output").and_then(|o| o.as_str()).unwrap_or("");
                if output.is_empty() { return None; }
                Some(("user".to_string(), format!("[result] {}", safe_truncate(output, 500))))
            }
            "reasoning" => {
                // Extract reasoning summary
                let summary = payload.get("summary").and_then(|s| s.as_array());
                let mut texts = Vec::new();
                if let Some(items) = summary {
                    for item in items {
                        if let Some(text) = item.get("text").and_then(|t| t.as_str()) {
                            if !text.trim().is_empty() {
                                texts.push(text.to_string());
                            }
                        }
                    }
                }
                if texts.is_empty() { return None; }
                Some(("assistant".to_string(), format!("[thinking] {}", texts.join("\n"))))
            }
            _ => None,
        }
    }

    /// Extract content from a Gemini message object
    /// Format: {"type":"user"|"gemini", "content": [{text:"..."}] | "string", "thoughts":[...], "toolCalls":[...]}
    fn extract_gemini_content(msg: &Value) -> Option<(String, String)> {
        let msg_type = msg.get("type").and_then(|t| t.as_str())?;

        let role = match msg_type {
            "user" => "user",
            "gemini" => "assistant",
            // Skip error/info system messages — low search value
            _ => return None,
        };

        let mut text_parts = Vec::new();

        // User content is [{text: "..."}], gemini content is a plain string
        if let Some(content) = msg.get("content") {
            if let Some(s) = content.as_str() {
                if !s.trim().is_empty() {
                    text_parts.push(s.to_string());
                }
            } else if let Some(arr) = content.as_array() {
                for item in arr {
                    if let Some(text) = item.get("text").and_then(|t| t.as_str()) {
                        if !text.trim().is_empty() {
                            text_parts.push(text.to_string());
                        }
                    }
                }
            }
        }

        // Include thinking blocks (valuable for understanding reasoning)
        if let Some(thoughts) = msg.get("thoughts").and_then(|t| t.as_array()) {
            for thought in thoughts {
                let subject = thought.get("subject").and_then(|s| s.as_str()).unwrap_or("");
                let desc = thought.get("description").and_then(|d| d.as_str()).unwrap_or("");
                if !desc.is_empty() {
                    text_parts.push(format!("[thinking: {}] {}", subject, desc));
                }
            }
        }

        // Include tool calls (name + truncated args + truncated result)
        if let Some(tool_calls) = msg.get("toolCalls").and_then(|t| t.as_array()) {
            for tc in tool_calls {
                let name = tc.get("name").and_then(|n| n.as_str()).unwrap_or("unknown");
                let display = tc.get("displayName").and_then(|d| d.as_str()).unwrap_or(name);
                let args = tc.get("args")
                    .map(|a| serde_json::to_string(a).unwrap_or_default())
                    .unwrap_or_default();
                text_parts.push(format!("[tool:{}] {}", display, safe_truncate(&args, 200)));

                // Extract tool result content
                if let Some(results) = tc.get("result").and_then(|r| r.as_array()) {
                    for res in results {
                        if let Some(output) = res.pointer("/functionResponse/response/output")
                            .and_then(|o| o.as_str())
                        {
                            text_parts.push(format!("[result] {}", safe_truncate(output, 500)));
                        }
                    }
                }
            }
        }

        if text_parts.is_empty() {
            return None;
        }

        Some((role.to_string(), text_parts.join("\n")))
    }

    /// Try to parse content as Gemini JSON conversation (single JSON with messages array).
    /// Returns None if the content is not Gemini format.
    ///
    /// STREAMING: Does NOT build a full DOM of the JSON. Instead, finds individual
    /// message objects by bracket-counting and parses each one separately.
    /// Memory: O(largest_single_message) instead of O(entire_file).
    fn try_chunk_gemini_json(&self, content: &str) -> Option<Vec<Chunk>> {
        // Quick check before expensive work — handle both compact and pretty JSON
        let has_messages = content.contains(r#""messages""#);
        let has_gemini = content.contains(r#""type":"gemini""#)
            || content.contains(r#""type": "gemini""#);
        if !has_messages || !has_gemini {
            return None;
        }

        // Find the start of the "messages" array without parsing the full JSON.
        // Look for "messages" followed by : and then [
        let messages_key = content.find(r#""messages""#)?;
        let after_key = &content[messages_key + 10..]; // skip past "messages"
        let colon_pos = after_key.find(':')?;
        let after_colon = &after_key[colon_pos + 1..];
        let bracket_pos = after_colon.find('[')?;
        let array_start = messages_key + 10 + colon_pos + 1 + bracket_pos;

        // Now stream through the array, extracting individual message objects by
        // tracking brace depth. Each top-level {...} inside the array is one message.
        let mut chunks = Vec::new();
        let mut current_content = String::new();
        let mut current_metadata = serde_json::json!({"message_count": 0, "roles": []});
        let mut chunk_start_line = 1usize;
        let mut message_count = 0usize;
        let mut msg_idx = 0usize;
        let mut roles: Vec<String> = Vec::new();

        // Approximate line numbers (count newlines up to array_start, then per-message)
        let base_line = content[..array_start].matches('\n').count() + 1;
        let bytes = content.as_bytes();
        let len = bytes.len();
        let mut pos = array_start + 1; // skip the '['

        // Count total lines lazily for the final chunk
        let total_lines = content.len() / 60; // rough estimate, avoids full scan

        loop {
            // Skip whitespace and commas between messages
            while pos < len && (bytes[pos] == b' ' || bytes[pos] == b'\n' || bytes[pos] == b'\r'
                || bytes[pos] == b'\t' || bytes[pos] == b',') {
                pos += 1;
            }

            if pos >= len || bytes[pos] == b']' {
                break; // end of messages array
            }

            if bytes[pos] != b'{' {
                pos += 1;
                continue; // unexpected char, skip
            }

            // Found start of a message object — find matching closing brace
            let msg_start = pos;
            let mut depth = 0i32;
            let mut in_string = false;
            let mut escape_next = false;

            while pos < len {
                let b = bytes[pos];
                if escape_next {
                    escape_next = false;
                    pos += 1;
                    continue;
                }
                if b == b'\\' && in_string {
                    escape_next = true;
                    pos += 1;
                    continue;
                }
                if b == b'"' {
                    in_string = !in_string;
                } else if !in_string {
                    if b == b'{' { depth += 1; }
                    else if b == b'}' {
                        depth -= 1;
                        if depth == 0 {
                            pos += 1; // include closing brace
                            break;
                        }
                    }
                }
                pos += 1;
            }

            let msg_str = &content[msg_start..pos];

            // Parse ONLY this single message (small DOM, immediately dropped)
            if let Ok(msg) = serde_json::from_str::<Value>(msg_str) {
                let approx_line = base_line + content[array_start..msg_start].matches('\n').count();

                if let Some((role, text)) = Self::extract_gemini_content(&msg) {
                    if !current_content.is_empty()
                        && current_content.len() + text.len() > self.target_size
                        && current_content.len() >= self.min_chunk_size
                    {
                        current_metadata["message_count"] = message_count.into();
                        current_metadata["roles"] = roles.clone().into();

                        chunks.push(Chunk {
                            text: current_content.clone(),
                            start_line: chunk_start_line,
                            end_line: approx_line.saturating_sub(1).max(chunk_start_line),
                            start_byte: 0,
                            end_byte: 0,
                            chunk_type: ChunkType::Conversation,
                            metadata: Some(current_metadata.clone()),
                            parent_idx: None,
                            level: 2,
                            context_prefix: None,
                        });

                        current_content = format!("[{}] {}", role, text);
                        chunk_start_line = approx_line;
                        message_count = 1;
                        roles = vec![role];
                        current_metadata = serde_json::json!({"message_count": 0, "roles": []});
                    } else {
                        if !current_content.is_empty() {
                            current_content.push_str("\n\n");
                        }
                        current_content.push_str(&format!("[{}] {}", role, text));
                        message_count += 1;
                        if !roles.contains(&role) {
                            roles.push(role);
                        }
                    }
                }
            }
            // msg DOM is dropped here — only one message in memory at a time

            msg_idx += 1;
        }

        // Final chunk
        if !current_content.is_empty() {
            current_metadata["message_count"] = message_count.into();
            current_metadata["roles"] = roles.into();

            chunks.push(Chunk {
                text: current_content,
                start_line: chunk_start_line,
                end_line: total_lines.max(chunk_start_line),
                start_byte: 0,
                end_byte: 0,
                chunk_type: ChunkType::Conversation,
                metadata: Some(current_metadata),
                parent_idx: None,
                level: 2,
                context_prefix: None,
            });
        }

        if chunks.is_empty() {
            return None;
        }

        tracing::info!(
            "Gemini JSON streaming chunker produced {} chunks from {} messages",
            chunks.len(),
            msg_idx
        );

        Some(chunks)
    }
}

impl Default for JsonlChunker {
    fn default() -> Self {
        Self {
            target_size: TARGET_CHUNK_SIZE,
            min_chunk_size: MIN_CHUNK_SIZE,
        }
    }
}

impl Chunker for JsonlChunker {
    fn chunk(&self, content: &str, _language: Option<&str>) -> Vec<Chunk> {
        // Try Gemini JSON format first (single JSON with messages array)
        if let Some(gemini_chunks) = self.try_chunk_gemini_json(content) {
            return gemini_chunks;
        }

        // Fall through to JSONL line-by-line parsing (Claude Code, Codex)
        let mut chunks = Vec::new();
        let mut current_content = String::new();
        let mut current_metadata = serde_json::json!({
            "message_count": 0,
            "roles": []
        });
        let mut chunk_start_line = 1usize;
        let mut message_count = 0usize;
        let mut roles: Vec<String> = Vec::new();

        for (line_idx, line) in content.lines().enumerate() {
            let line_num = line_idx + 1;

            // Skip empty lines
            if line.trim().is_empty() {
                continue;
            }

            // Try to parse JSONL line
            if let Ok(json) = serde_json::from_str::<Value>(line) {
                // Try Claude Code format first, then Codex
                let extracted = Self::extract_message_content(&json)
                    .or_else(|| Self::extract_codex_content(&json));

                if let Some((role, text)) = extracted {
                    // Check if adding this message would exceed target size
                    if !current_content.is_empty()
                        && current_content.len() + text.len() > self.target_size
                        && current_content.len() >= self.min_chunk_size
                    {
                        // Save current chunk
                        current_metadata["message_count"] = message_count.into();
                        current_metadata["roles"] = roles.clone().into();

                        chunks.push(Chunk {
                            text: current_content.clone(),
                            start_line: chunk_start_line,
                            end_line: line_num - 1,
                            start_byte: 0, // Approximation
                            end_byte: 0,
                            chunk_type: ChunkType::Conversation,
                            metadata: Some(current_metadata.clone()),
                            parent_idx: None,  // JSONL: flat structure
                            level: 2,          // Default to leaf level
                            context_prefix: None,
                        });

                        // Start new chunk
                        current_content = format!("[{}] {}", role, text);
                        chunk_start_line = line_num;
                        message_count = 1;
                        roles = vec![role];
                        current_metadata = serde_json::json!({
                            "message_count": 0,
                            "roles": []
                        });
                    } else {
                        // Add to current chunk
                        if !current_content.is_empty() {
                            current_content.push_str("\n\n");
                        }
                        current_content.push_str(&format!("[{}] {}", role, text));
                        message_count += 1;
                        if !roles.contains(&role) {
                            roles.push(role);
                        }
                    }
                }
            }
        }

        // Save final chunk
        if !current_content.is_empty() {
            current_metadata["message_count"] = message_count.into();
            current_metadata["roles"] = roles.into();

            let total_lines = content.lines().count();
            chunks.push(Chunk {
                text: current_content,
                start_line: chunk_start_line,
                end_line: total_lines,
                start_byte: 0,
                end_byte: 0,
                chunk_type: ChunkType::Conversation,
                metadata: Some(current_metadata),
                parent_idx: None,  // JSONL: flat structure
                level: 2,          // Default to leaf level
                context_prefix: None,
            });
        }

        // Fallback if no chunks created (not a valid JSONL conversation)
        if chunks.is_empty() {
            tracing::warn!("JSONL chunker produced 0 chunks, content may not be conversation format");
        }

        tracing::info!(
            "JSONL chunker produced {} chunks from {} lines",
            chunks.len(),
            content.lines().count()
        );

        chunks
    }

    fn name(&self) -> &str {
        "jsonl"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_claude_code_format() {
        let chunker = JsonlChunker::default();
        let content = r#"{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Hello, how are you?"}]}}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I'm doing well, thank you!"}]}}"#;

        let chunks = chunker.chunk(content, Some("jsonl"));

        assert_eq!(chunks.len(), 1);
        assert!(chunks[0].text.contains("[user] Hello"));
        assert!(chunks[0].text.contains("[assistant] I'm doing"));
        assert_eq!(chunks[0].chunk_type, ChunkType::Conversation);
    }

    #[test]
    fn test_codex_format() {
        let chunker = JsonlChunker::default();
        let content = r#"{"session_id":"abc","ts":123,"text":"First message"}
{"session_id":"abc","ts":124,"text":"Second message"}"#;

        let chunks = chunker.chunk(content, Some("jsonl"));

        assert_eq!(chunks.len(), 1);
        assert!(chunks[0].text.contains("First message"));
        assert!(chunks[0].text.contains("Second message"));
    }

    #[test]
    fn test_codex_new_format() {
        let chunker = JsonlChunker::default();
        let content = r#"{"timestamp":"2025-10-26T11:54:26.604Z","type":"session_meta","payload":{"id":"test-id"}}
{"timestamp":"2025-10-26T11:58:11.515Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"welche md files siehst du"}]}}
{"timestamp":"2025-10-26T12:00:00.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Ich sehe folgende Dateien:"}]}}"#;

        let chunks = chunker.chunk(content, Some("jsonl"));

        assert_eq!(chunks.len(), 1);
        assert!(chunks[0].text.contains("[user] welche md files"));
        assert!(chunks[0].text.contains("[assistant] Ich sehe folgende Dateien"));
        // session_meta should be skipped
        assert!(!chunks[0].text.contains("session_meta"));
        assert_eq!(chunks[0].chunk_type, ChunkType::Conversation);
    }

    #[test]
    fn test_large_conversation_splits() {
        let chunker = JsonlChunker::new(ChunkerConfig {
            max_tokens: Some(100), // ~400 chars target
            ..Default::default()
        });

        // Create messages that exceed target size
        let msg1 = "A".repeat(300);
        let msg2 = "B".repeat(300);
        let content = format!(
            r#"{{"type":"user","message":{{"role":"user","content":[{{"type":"text","text":"{}"}}]}}}}
{{"type":"assistant","message":{{"role":"assistant","content":[{{"type":"text","text":"{}"}}]}}}}"#,
            msg1, msg2
        );

        let chunks = chunker.chunk(&content, Some("jsonl"));

        // Should split into 2 chunks
        assert!(chunks.len() >= 2, "Expected >=2 chunks, got {}", chunks.len());
    }

    #[test]
    fn test_gemini_json_format() {
        let chunker = JsonlChunker::default();
        let content = r#"{
  "sessionId": "test-session",
  "messages": [
    {"type": "user", "content": [{"text": "How does the API work?"}]},
    {"type": "gemini", "content": "The API uses REST endpoints with JWT auth.", "thoughts": [{"subject": "Analyzing", "description": "Checking API structure"}]}
  ]
}"#;

        let chunks = chunker.chunk(content, Some("json"));

        assert_eq!(chunks.len(), 1);
        assert!(chunks[0].text.contains("[user] How does the API work?"));
        assert!(chunks[0].text.contains("[assistant] The API uses REST"));
        assert!(chunks[0].text.contains("[thinking: Analyzing]"));
        assert_eq!(chunks[0].chunk_type, ChunkType::Conversation);
    }

    #[test]
    fn test_gemini_tool_calls() {
        let chunker = JsonlChunker::default();
        let content = r#"{
  "sessionId": "test-session",
  "messages": [
    {"type": "user", "content": [{"text": "Read the config"}]},
    {"type": "gemini", "content": "Here is the config.", "toolCalls": [
      {"name": "read_file", "displayName": "ReadFile", "args": {"file_path": "/etc/config.toml"}, "result": [{"functionResponse": {"response": {"output": "key = value"}}}]}
    ]}
  ]
}"#;

        let chunks = chunker.chunk(content, Some("json"));

        assert_eq!(chunks.len(), 1);
        assert!(chunks[0].text.contains("[tool:ReadFile]"));
        assert!(chunks[0].text.contains("[result] key = value"));
    }

    #[test]
    fn test_gemini_skips_error_info() {
        let chunker = JsonlChunker::default();
        let content = r#"{
  "sessionId": "test",
  "messages": [
    {"type": "error", "content": "Update failed"},
    {"type": "info", "content": "Version 0.33.0"},
    {"type": "user", "content": [{"text": "Hello"}]},
    {"type": "gemini", "content": "Hi there"}
  ]
}"#;

        let chunks = chunker.chunk(content, Some("json"));

        assert_eq!(chunks.len(), 1);
        // error/info should be skipped
        assert!(!chunks[0].text.contains("Update failed"));
        assert!(!chunks[0].text.contains("Version 0.33"));
        assert!(chunks[0].text.contains("[user] Hello"));
        assert!(chunks[0].text.contains("[assistant] Hi there"));
    }

    #[test]
    fn test_tool_use_extraction() {
        let chunker = JsonlChunker::default();
        let content = r#"{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"Read","input":{"file_path":"/test.txt"}}]}}"#;

        let chunks = chunker.chunk(content, Some("jsonl"));

        assert_eq!(chunks.len(), 1);
        assert!(chunks[0].text.contains("[tool:Read]"));
    }
}
