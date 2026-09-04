//! Export plugin for ChatGPT and Claude conversation exports
//!
//! Supports:
//! - ChatGPT exports: Nested `mapping` structure with tree-based messages
//! - Claude exports: Flat `conversations[]` array with linear messages
//!
//! Both formats are converted to Markdown for indexing and search.

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::Value;
use std::collections::HashMap;
use tracing::{info, warn};

use super::{RawFile, SourcePlugin, SyncResult};

// ============ CHATGPT FORMAT ============

#[derive(Debug, Deserialize)]
struct ChatGptExport {
    title: Option<String>,
    #[allow(dead_code)]
    create_time: Option<f64>,
    mapping: HashMap<String, ChatGptNode>,
}

#[derive(Debug, Deserialize)]
struct ChatGptNode {
    message: Option<ChatGptMessage>,
    parent: Option<String>,
    children: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct ChatGptMessage {
    author: ChatGptAuthor,
    content: ChatGptContent,
}

#[derive(Debug, Deserialize)]
struct ChatGptAuthor {
    role: String,
}

#[derive(Debug, Deserialize)]
struct ChatGptContent {
    parts: Option<Vec<Value>>, // Can be strings or other types (images, etc.)
}

// ============ CLAUDE FORMAT ============

#[derive(Debug, Deserialize)]
struct ClaudeExport {
    conversations: Vec<ClaudeConversation>,
}

#[derive(Debug, Deserialize)]
struct ClaudeConversation {
    #[allow(dead_code)]
    id: Option<String>,
    #[serde(alias = "name")]
    title: Option<String>,
    #[serde(alias = "chat_messages")]
    messages: Vec<ClaudeMessage>,
}

#[derive(Debug, Deserialize)]
struct ClaudeMessage {
    #[serde(alias = "sender")]
    role: Option<String>,
    #[serde(alias = "text")]
    content: Option<String>,
}

// ============ UNIFIED MESSAGE ============

#[derive(Debug)]
struct UnifiedMessage {
    role: String,
    content: String,
}

// ============ PLUGIN IMPLEMENTATION ============

pub struct ExportPlugin;

impl Default for ExportPlugin {
    fn default() -> Self {
        Self::new()
    }
}

impl ExportPlugin {
    pub fn new() -> Self {
        Self
    }

    /// Try parsing as ChatGPT format first, then Claude
    fn parse_export(&self, content: &str) -> anyhow::Result<Vec<(String, Vec<UnifiedMessage>)>> {
        // Try ChatGPT format (has "mapping" field)
        if let Ok(chatgpt) = serde_json::from_str::<ChatGptExport>(content) {
            if !chatgpt.mapping.is_empty() {
                info!("Parsed as ChatGPT export format");
                return Ok(vec![self.parse_chatgpt_conversation(chatgpt)]);
            }
        }

        // Try Claude format (has "conversations" array)
        if let Ok(claude) = serde_json::from_str::<ClaudeExport>(content) {
            if !claude.conversations.is_empty() {
                info!(
                    "Parsed as Claude export format ({} conversations)",
                    claude.conversations.len()
                );
                return Ok(claude
                    .conversations
                    .into_iter()
                    .map(|c| {
                        let title = c.title.unwrap_or_else(|| "Untitled".to_string());
                        let messages = c
                            .messages
                            .into_iter()
                            .filter_map(|m| {
                                let role = m.role.unwrap_or_else(|| "unknown".to_string());
                                let content = m.content.unwrap_or_default();
                                if content.is_empty() {
                                    None
                                } else {
                                    Some(UnifiedMessage { role, content })
                                }
                            })
                            .collect();
                        (title, messages)
                    })
                    .collect());
            }
        }

        // Try as array of ChatGPT conversations (some exports have multiple conversations)
        if let Ok(conversations) = serde_json::from_str::<Vec<ChatGptExport>>(content) {
            if !conversations.is_empty() {
                info!(
                    "Parsed as ChatGPT export array ({} conversations)",
                    conversations.len()
                );
                return Ok(conversations
                    .into_iter()
                    .map(|c| self.parse_chatgpt_conversation(c))
                    .collect());
            }
        }

        anyhow::bail!("Unrecognized export format (not ChatGPT or Claude)")
    }

    /// Parse ChatGPT's nested mapping structure into linear messages
    fn parse_chatgpt_conversation(&self, export: ChatGptExport) -> (String, Vec<UnifiedMessage>) {
        let title = export.title.unwrap_or_else(|| "Untitled".to_string());

        // Find root node (parent == null)
        let root_id = export
            .mapping
            .iter()
            .find(|(_, node)| node.parent.is_none())
            .map(|(id, _)| id.clone());

        let mut messages = Vec::new();
        if let Some(start_id) = root_id {
            self.traverse_chatgpt_tree(&export.mapping, &start_id, &mut messages);
        }

        (title, messages)
    }

    /// DFS traversal of ChatGPT's tree structure.
    ///
    /// # Data Loss Warning
    /// ChatGPT exports can have multiple conversation paths (branching) when users
    /// edit messages or regenerate responses. This linearization follows ONLY the
    /// FIRST child at each node. Alternative branches are silently ignored.
    ///
    /// This is a conscious design decision for V1 simplicity. If branch preservation
    /// is needed, consider a flatten-all-branches approach (but may cause duplicates).
    fn traverse_chatgpt_tree(
        &self,
        mapping: &HashMap<String, ChatGptNode>,
        node_id: &str,
        messages: &mut Vec<UnifiedMessage>,
    ) {
        if let Some(node) = mapping.get(node_id) {
            // Extract message if present
            if let Some(msg) = &node.message {
                let content = msg
                    .content
                    .parts
                    .as_ref()
                    .map(|parts| {
                        parts
                            .iter()
                            .filter_map(|p| p.as_str())
                            .collect::<Vec<_>>()
                            .join("\n")
                    })
                    .unwrap_or_default();

                if !content.is_empty() {
                    messages.push(UnifiedMessage {
                        role: msg.author.role.clone(),
                        content,
                    });
                }
            }

            // Traverse children (take first child for linear path)
            // WARNING: This ignores alternative branches!
            if let Some(child_id) = node.children.first() {
                self.traverse_chatgpt_tree(mapping, child_id, messages);
            } else if node.children.len() > 1 {
                warn!(
                    "ChatGPT export has {} branches at node {}, only following first",
                    node.children.len(),
                    node_id
                );
            }
        }
    }

    /// Convert conversation to Markdown format for indexing
    fn conversation_to_markdown(&self, title: &str, messages: &[UnifiedMessage]) -> String {
        let mut md = format!("# {}\n\n---\n\n", title);

        for (idx, msg) in messages.iter().enumerate() {
            let emoji = match msg.role.as_str() {
                "user" | "human" => "**User**",
                "assistant" | "bot" => "**Assistant**",
                "system" => "**System**",
                _ => "**Unknown**",
            };
            md.push_str(&format!(
                "## Message {}: {}\n\n{}\n\n---\n\n",
                idx + 1,
                emoji,
                msg.content
            ));
        }

        md
    }

    /// Create URL-safe slug from title
    fn slugify(&self, title: &str) -> String {
        title
            .to_lowercase()
            .chars()
            .map(|c| if c.is_alphanumeric() { c } else { '-' })
            .collect::<String>()
            .split('-')
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join("-")
            .chars()
            .take(100) // Limit filename length
            .collect()
    }
}

#[async_trait]
impl SourcePlugin for ExportPlugin {
    async fn sync(&self, source_path: &str) -> anyhow::Result<SyncResult> {
        info!("Export plugin syncing: {}", source_path);

        // Read the export file
        let content = tokio::fs::read_to_string(source_path)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to read export file: {}", e))?;

        // Parse the export (supports ChatGPT and Claude formats)
        let conversations = self.parse_export(&content)?;

        if conversations.is_empty() {
            warn!("No conversations found in export");
            return Ok(SyncResult {
                files: vec![],
                errors: vec!["No conversations found in export".to_string()],
            });
        }

        // Convert conversations to Markdown files
        // Use enumerate to ensure unique filenames even if titles are identical
        let files: Vec<RawFile> = conversations
            .iter()
            .enumerate()
            .filter(|(_, (_, messages))| !messages.is_empty())
            .map(|(idx, (title, messages))| {
                let markdown = self.conversation_to_markdown(title, messages);
                let slug = self.slugify(title);
                let base = if slug.is_empty() {
                    "untitled".to_string()
                } else {
                    slug
                };
                // Add index suffix to prevent filename collisions
                let path = format!("{}-{:03}.md", base, idx + 1);

                RawFile {
                    path,
                    size: markdown.len(),
                    content: markdown,
                    language: Some("markdown".to_string()),
                    last_modified: None,
                    source_path: None,
                    source_range: None,
                }
            })
            .collect();

        info!(
            "Export plugin: {} conversations converted to Markdown",
            files.len()
        );

        Ok(SyncResult {
            files,
            errors: vec![],
        })
    }

    fn source_type(&self) -> &'static str {
        "export"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_claude_export_parsing() {
        let plugin = ExportPlugin::new();
        let content = r#"{
            "conversations": [
                {
                    "id": "test-123",
                    "title": "Test Conversation",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi there!"}
                    ]
                }
            ]
        }"#;

        let result = plugin.parse_export(content).unwrap();
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].0, "Test Conversation");
        assert_eq!(result[0].1.len(), 2);
        assert_eq!(result[0].1[0].role, "user");
        assert_eq!(result[0].1[0].content, "Hello");
    }

    #[test]
    fn test_chatgpt_export_parsing() {
        let plugin = ExportPlugin::new();
        let content = r#"{
            "title": "ChatGPT Test",
            "create_time": 1704067200,
            "mapping": {
                "root-id": {
                    "message": null,
                    "parent": null,
                    "children": ["msg-1"]
                },
                "msg-1": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["Hello ChatGPT"]}
                    },
                    "parent": "root-id",
                    "children": ["msg-2"]
                },
                "msg-2": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["Hello! How can I help?"]}
                    },
                    "parent": "msg-1",
                    "children": []
                }
            }
        }"#;

        let result = plugin.parse_export(content).unwrap();
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].0, "ChatGPT Test");
        assert_eq!(result[0].1.len(), 2);
        assert_eq!(result[0].1[0].role, "user");
        assert_eq!(result[0].1[0].content, "Hello ChatGPT");
    }

    #[test]
    fn test_conversation_to_markdown() {
        let plugin = ExportPlugin::new();
        let messages = vec![
            UnifiedMessage {
                role: "user".to_string(),
                content: "What is RAG?".to_string(),
            },
            UnifiedMessage {
                role: "assistant".to_string(),
                content: "RAG stands for...".to_string(),
            },
        ];

        let md = plugin.conversation_to_markdown("Test", &messages);
        assert!(md.contains("# Test"));
        assert!(md.contains("**User**"));
        assert!(md.contains("What is RAG?"));
        assert!(md.contains("**Assistant**"));
    }

    #[test]
    fn test_slugify() {
        let plugin = ExportPlugin::new();
        assert_eq!(plugin.slugify("Hello World!"), "hello-world");
        assert_eq!(plugin.slugify("Test 123 @#$"), "test-123");
        assert_eq!(plugin.slugify("  Spaces  "), "spaces");
    }

    #[test]
    fn test_invalid_format() {
        let plugin = ExportPlugin::new();
        let content = r#"{"invalid": "format"}"#;
        assert!(plugin.parse_export(content).is_err());
    }

    #[test]
    fn test_chatgpt_array_export() {
        let plugin = ExportPlugin::new();
        // ChatGPT exports can be an array of conversations
        let content = r#"[
            {
                "title": "First Conversation",
                "mapping": {
                    "root": {
                        "message": null,
                        "parent": null,
                        "children": ["msg-1"]
                    },
                    "msg-1": {
                        "message": {
                            "author": {"role": "user"},
                            "content": {"parts": ["Hello from conv 1"]}
                        },
                        "parent": "root",
                        "children": []
                    }
                }
            },
            {
                "title": "Second Conversation",
                "mapping": {
                    "root": {
                        "message": null,
                        "parent": null,
                        "children": ["msg-1"]
                    },
                    "msg-1": {
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"parts": ["Hello from conv 2"]}
                        },
                        "parent": "root",
                        "children": []
                    }
                }
            }
        ]"#;

        let result = plugin.parse_export(content).unwrap();
        assert_eq!(result.len(), 2, "Expected 2 conversations from array");
        assert_eq!(result[0].0, "First Conversation");
        assert_eq!(result[1].0, "Second Conversation");
        assert_eq!(result[0].1[0].content, "Hello from conv 1");
        assert_eq!(result[1].1[0].content, "Hello from conv 2");
    }

    #[test]
    fn test_chatgpt_branching_follows_first_child() {
        let plugin = ExportPlugin::new();
        // ChatGPT export with branching (multiple children)
        // Should only follow first child path
        let content = r#"{
            "title": "Branching Test",
            "mapping": {
                "root": {
                    "message": null,
                    "parent": null,
                    "children": ["msg-1"]
                },
                "msg-1": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["Original question"]}
                    },
                    "parent": "root",
                    "children": ["branch-a", "branch-b"]
                },
                "branch-a": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["First response (should be included)"]}
                    },
                    "parent": "msg-1",
                    "children": []
                },
                "branch-b": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["Alternative response (should be ignored)"]}
                    },
                    "parent": "msg-1",
                    "children": []
                }
            }
        }"#;

        let result = plugin.parse_export(content).unwrap();
        assert_eq!(result.len(), 1);
        assert_eq!(
            result[0].1.len(),
            2,
            "Should have 2 messages (user + first branch)"
        );

        // First branch should be included
        assert!(
            result[0]
                .1
                .iter()
                .any(|m| m.content.contains("First response")),
            "First branch should be included"
        );

        // Second branch should be ignored
        assert!(
            !result[0]
                .1
                .iter()
                .any(|m| m.content.contains("Alternative response")),
            "Second branch should be ignored (main path only)"
        );
    }
}
