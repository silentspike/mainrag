use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Source {
    pub id: i64,
    pub name: String,
    pub source_type: String,
    pub path: String,
    pub config: Option<serde_json::Value>,
    pub last_synced: Option<DateTime<Utc>>,
    pub file_count: i32,
    pub total_size: i64,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Chunk {
    pub id: i64,
    pub file_id: i64,
    pub content: String,
    pub line_start: i32,
    pub line_end: i32,
    pub source_id: i64,
    pub language: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Symbol {
    pub id: i64,
    pub file_id: i64,
    pub name: String,
    #[serde(rename = "type")]
    pub symbol_type: String,
    pub line_start: i32,
    pub line_end: i32,
    pub context: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct User {
    pub id: Uuid,
    pub username: String,
    pub email: Option<String>,
    pub is_active: bool,
    pub is_admin: bool,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResult {
    pub chunk_id: i64,
    pub file_path: String,
    pub content: String,
    /// Highlighted snippet showing match context (with **term** markers)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub snippet: Option<String>,
    pub line_start: i32,
    pub line_end: i32,
    pub source_name: String,
    pub language: Option<String>,
    pub score: f32,
    /// CCH (Contextual Chunk Header) prefix for hierarchical context
    /// Format: "[source] path > parent_context"
    #[serde(skip_serializing_if = "Option::is_none")]
    pub context_prefix: Option<String>,
    /// Compact location reference (e.g., "src/main.rs:10-25")
    #[serde(skip_serializing_if = "Option::is_none")]
    pub location: Option<String>,
    /// Chunk type for relevance boosting (function, code, type, class, text, etc.)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub chunk_type: Option<String>,
    /// Hierarchy level (0=file, 1=class/section, 2=function)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub level: Option<i16>,
    /// Parent context (e.g., class signature for a function chunk)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_context: Option<String>,
}

impl SearchResult {
    /// Optimize result for LLM consumption with query-aware snippet generation:
    /// - Convert <<<>>> FTS markers to **markdown** bold
    /// - If no FTS markers found, create snippet around query match
    /// - Unescape file_path (- → /)
    /// - Generate compact location field
    /// - Truncate content if too large (max 16000 chars ≈ 4000 tokens)
    pub fn optimize_for_llm_with_query(mut self, query: &str) -> Self {
        const MAX_CONTENT_CHARS: usize = 16000;
        const SNIPPET_CONTEXT: usize = 80; // chars before/after match

        // 1. Convert FTS markers to markdown bold in snippet
        if let Some(ref mut snippet) = self.snippet {
            *snippet = snippet.replace("<<<", "**").replace(">>>", "**");
        }

        // 2. If snippet has no highlights, create one manually around query match
        let has_highlights = self
            .snippet
            .as_ref()
            .map(|s| s.contains("**"))
            .unwrap_or(false);

        if !has_highlights {
            // Find query term in content (case-insensitive, zero-alloc)
            // Uses ASCII case-insensitive byte comparison instead of to_lowercase() allocation

            // Try each word in query
            for word in query.split_whitespace() {
                if word.len() < 2 {
                    continue;
                }

                if let Some(pos) = find_ascii_case_insensitive(&self.content, word) {
                    // Find char boundaries for context window
                    let start = self.content[..pos]
                        .char_indices()
                        .rev()
                        .nth(SNIPPET_CONTEXT)
                        .map(|(i, _)| i)
                        .unwrap_or(0);

                    let end_pos = pos + word.len();
                    let end = self.content[end_pos..]
                        .char_indices()
                        .nth(SNIPPET_CONTEXT)
                        .map(|(i, _)| end_pos + i)
                        .unwrap_or(self.content.len());

                    // Extract snippet and highlight the match
                    let prefix = if start > 0 { "..." } else { "" };
                    let suffix = if end < self.content.len() { "..." } else { "" };

                    // Get the actual matched text (preserve original case)
                    let matched_text = &self.content[pos..pos + word.len()];
                    let before = &self.content[start..pos];
                    let after = &self.content[pos + word.len()..end];

                    self.snippet = Some(format!(
                        "{}{}**{}**{}{}",
                        prefix, before, matched_text, after, suffix
                    ));
                    break;
                }
            }
        }

        // 3. Unescape file_path: dashes before first / represent escaped slashes
        if self.file_path.starts_with('-') {
            if let Some(first_slash) = self.file_path.find('/') {
                let prefix = &self.file_path[..first_slash].replace('-', "/");
                let suffix = &self.file_path[first_slash..];
                self.file_path = format!("{}{}", prefix, suffix);
            } else {
                self.file_path = self.file_path.replacen('-', "/", 1);
            }
        }

        // 4. Generate compact location
        self.location = Some(format!(
            "{}:{}-{}",
            self.file_path, self.line_start, self.line_end
        ));

        // 5. Truncate content if too large for LLM context
        if self.content.len() > MAX_CONTENT_CHARS {
            // UTF-8 safety: MAX_CONTENT_CHARS is a byte limit and may land
            // inside a multi-byte character.  Walk backward to the nearest
            // char boundary to avoid a panic.
            let mut end = MAX_CONTENT_CHARS;
            while end > 0 && !self.content.is_char_boundary(end) {
                end -= 1;
            }
            self.content = format!(
                "{}...\n[truncated: {} chars total]",
                &self.content[..end],
                self.content.len()
            );
        }

        self
    }

    /// Legacy method for backward compatibility (no query-aware snippets)
    pub fn optimize_for_llm(self) -> Self {
        self.optimize_for_llm_with_query("")
    }
}

/// Zero-allocation ASCII case-insensitive substring search.
/// Returns byte offset of first match (safe for UTF-8 since we only match ASCII patterns).
fn find_ascii_case_insensitive(haystack: &str, needle: &str) -> Option<usize> {
    let needle_bytes = needle.as_bytes();
    let needle_len = needle_bytes.len();
    if needle_len == 0 || needle_len > haystack.len() {
        return None;
    }
    haystack
        .as_bytes()
        .windows(needle_len)
        .position(|window| window.eq_ignore_ascii_case(needle_bytes))
}

// =============================================================================
// Intelligence Layer Models
// =============================================================================

/// Enriched symbol metadata — generisch, domain-agnostisch.
/// Alle Enrichment-Felder sind Option (nicht jedes Symbol hat eine Card).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SymbolCard {
    pub symbol_id: i64,
    pub name: String,
    pub qualified_name: Option<String>,
    pub symbol_type: String,
    pub signature: Option<String>,
    pub file_path: String,
    pub line_start: i32,
    pub line_end: i32,
    pub source_name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub visibility: Option<String>,
    // Enrichment fields
    #[serde(skip_serializing_if = "Option::is_none")]
    pub layer: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub side_effect_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub affected_resource: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub delegation_targets: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thread_requirement: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub preconditions: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub classification_confidence: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub domain_profile: Option<String>,
}

/// A step in a delegation chain (entry → proxy → dispatch → mutation).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DelegationStep {
    pub symbol: SymbolCard,
    pub role: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dispatch_via: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code_snippet: Option<String>,
    #[serde(default)]
    pub step_annotations: Vec<AnnotationInfo>,
}

/// Complete delegation chain from entry point through proxy/dispatch to mutation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DelegationChain {
    pub entry_point: SymbolCard,
    pub steps: Vec<DelegationStep>,
    #[serde(default)]
    pub annotations: Vec<AnnotationInfo>,
}

/// Code-level annotation extracted from source (thread requirements, dispatch patterns, etc.)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnnotationInfo {
    pub annotation_type: String,
    pub value: String,
    pub confidence: f32,
}

/// Ownership/containment relationship between symbols.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnershipInfo {
    pub symbol_name: String,
    pub relation_type: String,
    pub direction: String, // "outgoing" | "incoming"
    pub target_name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target_file: Option<String>,
    pub confidence: f32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub evidence_line: Option<i32>,
}

/// Explore response — orchestrated result from query rewriting + symbol search + path tracing.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExploreResponse {
    pub query: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub intent: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub domain: Option<String>,
    pub candidate_paths: Vec<CandidatePath>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub negative_evidence: Vec<NegativeEvidence>,
    pub suggested_next: Vec<SuggestedQuery>,
    /// Structured text summary for direct LLM consumption
    pub formatted: String,
}

/// A candidate delegation path with explanation
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CandidatePath {
    pub rank: u32,
    pub title: String,
    pub confidence: String,
    pub chain: DelegationChain,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub why_relevant: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub why_might_not_work: Option<String>,
}

/// Suggested follow-up query
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SuggestedQuery {
    pub query: String,
    pub rationale: String,
}

/// Negative evidence — a known dead-end path.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NegativeEvidence {
    pub id: i64,
    pub concept: String,
    pub path_description: String,
    pub reason: String,
    #[serde(default)]
    pub symbols: serde_json::Value,
    pub severity: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_by: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub domain_profile: Option<String>,
}
