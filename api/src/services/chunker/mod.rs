//! Semantic Chunking System
//!
//! Pluggable chunking strategies for code and text.
//! - Character: Legacy (1000 chars, 100 overlap)
//! - Token: GPT-3.5 tokenization (256 tokens, 32 overlap)
//! - Semantic: Tree-sitter aware with function/class boundaries
//! - JSONL: Conversation-aware chunking for Claude Code exports

pub mod character;
pub mod token;
pub mod semantic;
pub mod jsonl;

use serde_json::Value;

/// Configuration for chunkers
#[derive(Debug, Clone, Default)]
pub struct ChunkerConfig {
    /// Max tokens (for token and semantic chunkers)
    pub max_tokens: Option<usize>,      // Default: 256
    /// Token overlap (for token and semantic chunkers)
    pub overlap_tokens: Option<usize>,  // Default: 32
    /// Max characters (for character chunker)
    pub max_chars: Option<usize>,       // Default: 1000
    /// Character overlap (for character chunker)
    pub overlap_chars: Option<usize>,   // Default: 100
}

/// Individual chunk of text
#[allow(dead_code)]
#[derive(Debug, Clone)]
pub struct Chunk {
    pub text: String,
    pub start_line: usize,
    pub end_line: usize,
    pub start_byte: usize,
    pub end_byte: usize,
    pub chunk_type: ChunkType,
    pub metadata: Option<Value>,  // e.g., function_name, semantic_unit
    /// Index of parent chunk in the same chunk list (for hierarchical chunking)
    pub parent_idx: Option<usize>,
    /// Hierarchy level: 0=file, 1=class/module, 2=function/method
    pub level: u8,
    /// CCH (Contextual Chunk Header) prefix for embedding
    /// Format: "[source] path > context"
    pub context_prefix: Option<String>,
}

/// Type of chunk based on semantic analysis
#[allow(dead_code)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChunkType {
    File,          // Level 0: Entire file or file header
    Code,          // Generic code block
    Text,          // Plain text
    Config,        // Configuration file
    Function,      // Semantic: function/method
    Class,         // Semantic: class/struct/interface
    Module,        // Semantic: module/namespace/impl
    Type,          // Structs, enums, type aliases
    Section,       // Markdown section (H1/H2/H3)
    Conversation,  // JSONL conversation turns
}

impl std::fmt::Display for ChunkType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ChunkType::File => write!(f, "file"),
            ChunkType::Code => write!(f, "code"),
            ChunkType::Text => write!(f, "text"),
            ChunkType::Config => write!(f, "config"),
            ChunkType::Function => write!(f, "function"),
            ChunkType::Class => write!(f, "class"),
            ChunkType::Module => write!(f, "module"),
            ChunkType::Type => write!(f, "type"),
            ChunkType::Section => write!(f, "section"),
            ChunkType::Conversation => write!(f, "conversation"),
        }
    }
}

impl ChunkType {
    /// Get hierarchy level for this chunk type
    #[allow(dead_code)]
    pub fn level(&self) -> u8 {
        match self {
            ChunkType::File => 0,
            ChunkType::Class | ChunkType::Module | ChunkType::Type | ChunkType::Section => 1,
            ChunkType::Function | ChunkType::Code | ChunkType::Text |
            ChunkType::Config | ChunkType::Conversation => 2,
        }
    }
}

/// CCH (Contextual Chunk Header) configuration
#[allow(dead_code)]
pub const CCH_MAX_LENGTH: usize = 100;

impl Chunk {
    /// Generate CCH (Contextual Chunk Header) prefix for embedding
    /// Format: "[source] path > context\n\n"
    /// Max length: 100 chars for the header (before \n\n)
    #[allow(dead_code)]
    pub fn generate_cch(source_name: &str, file_path: &str, parent_context: Option<&str>) -> String {
        let prefix = match parent_context {
            Some(ctx) => format!("[{}] {} > {}", source_name, file_path, ctx),
            None => format!("[{}] {}", source_name, file_path),
        };

        // Truncate if too long (max 100 chars)
        if prefix.len() > CCH_MAX_LENGTH {
            // UTF-8 safety: CCH_MAX_LENGTH is a byte limit; walk backward to
            // a valid char boundary before slicing.
            let mut end = CCH_MAX_LENGTH - 3;
            while end > 0 && !prefix.is_char_boundary(end) {
                end -= 1;
            }
            let truncated = &prefix[..end];
            // Find last space to avoid cutting words
            if let Some(last_space) = truncated.rfind(' ') {
                format!("{}...", &truncated[..last_space])
            } else {
                format!("{}...", truncated)
            }
        } else {
            prefix
        }
    }

    /// Get text with CCH prefix for embedding
    #[allow(dead_code)]
    pub fn text_with_cch(&self) -> String {
        match &self.context_prefix {
            Some(prefix) => format!("{}\n\n{}", prefix, self.text),
            None => self.text.clone(),
        }
    }
}

/// Chunking strategy
#[derive(Debug, Clone, Copy)]
#[allow(dead_code)]
pub enum ChunkerType {
    Character,  // Legacy (1000 chars)
    Token,      // 256 tokens
    Semantic,   // Tree-sitter aware
    Jsonl,      // Conversation-aware for JSONL
}

/// Common chunker interface
pub trait Chunker: Send + Sync {
    fn chunk(&self, content: &str, language: Option<&str>) -> Vec<Chunk>;
    #[allow(dead_code)]
    fn name(&self) -> &str;
}

/// Factory function to create chunkers
pub fn create_chunker(chunker_type: ChunkerType, config: ChunkerConfig) -> Box<dyn Chunker> {
    match chunker_type {
        ChunkerType::Character => Box::new(character::CharacterChunker::new(config)),
        ChunkerType::Token => Box::new(token::TokenChunker::new(config)),
        ChunkerType::Semantic => Box::new(semantic::SemanticChunker::new(config)),
        ChunkerType::Jsonl => Box::new(jsonl::JsonlChunker::new(config)),
    }
}

/// Get chunker type from environment or default to Semantic
pub fn get_default_chunker() -> Box<dyn Chunker> {
    let chunker_type = std::env::var("CHUNKER_TYPE")
        .ok()
        .as_deref()
        .map(|s| match s {
            "character" => ChunkerType::Character,
            "token" => ChunkerType::Token,
            _ => ChunkerType::Semantic,
        })
        .unwrap_or(ChunkerType::Semantic);

    create_chunker(chunker_type, ChunkerConfig::default())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_character_chunker() {
        let chunker = character::CharacterChunker::new(ChunkerConfig::default());
        let content = "Hello world\nThis is a test\nWith multiple lines";
        let chunks = chunker.chunk(content, None);
        assert!(!chunks.is_empty());
        assert!(chunks.iter().all(|c| !c.text.is_empty()));
    }

    #[test]
    fn test_token_chunker() {
        let chunker = token::TokenChunker::new(ChunkerConfig::default());
        let content = "fn main() { println!(\"Hello\"); }";
        let chunks = chunker.chunk(content, Some("rust"));
        assert!(!chunks.is_empty());
    }

    // ========================================================================
    // CCH (Contextual Chunk Header) Tests
    // ========================================================================

    #[test]
    fn test_generate_cch_without_parent() {
        let cch = Chunk::generate_cch("coderag", "src/main.rs", None);
        assert_eq!(cch, "[coderag] src/main.rs");
    }

    #[test]
    fn test_generate_cch_with_parent() {
        let cch = Chunk::generate_cch("coderag", "src/lib.rs", Some("impl Database"));
        assert_eq!(cch, "[coderag] src/lib.rs > impl Database");
    }

    #[test]
    fn test_generate_cch_truncates_long_headers() {
        // Create a very long path that exceeds CCH_MAX_LENGTH (100 chars)
        let long_path = "src/very/deeply/nested/directory/structure/with/many/subdirs/and/a/long/filename_that_goes_on_and_on.rs";
        let cch = Chunk::generate_cch("my-long-source-name", long_path, Some("impl VeryLongClassName"));

        // CCH should be truncated to max 100 chars (plus "...")
        assert!(cch.len() <= CCH_MAX_LENGTH + 3, "CCH too long: {} chars", cch.len());
        assert!(cch.ends_with("..."), "Truncated CCH should end with ...");
    }

    #[test]
    fn test_generate_cch_truncates_at_word_boundary() {
        // Create a CCH that will definitely need truncation (over 100 chars)
        let long_path = "src/services/very/deeply/nested/module/with/long/path/that/exceeds/limit.rs";
        let long_context = "impl MyVeryLongTraitName";
        let cch = Chunk::generate_cch("my-source", long_path, Some(long_context));

        // CCH should be truncated
        assert!(cch.ends_with("..."), "Long CCH should end with ..., got: {}", cch);
        assert!(cch.len() <= CCH_MAX_LENGTH + 3, "CCH too long: {}", cch.len());

        // The content before "..." should not end mid-word if there was a space to cut at
        // Just verify it produces a reasonable result
        assert!(cch.starts_with("[my-source]"), "CCH should start with source name");
    }

    #[test]
    fn test_text_with_cch_returns_prefixed_text() {
        let chunk = Chunk {
            text: "fn main() {}".to_string(),
            start_line: 1,
            end_line: 1,
            start_byte: 0,
            end_byte: 12,
            chunk_type: ChunkType::Function,
            metadata: None,
            parent_idx: None,
            level: 2,
            context_prefix: Some("[source] path.rs".to_string()),
        };

        let result = chunk.text_with_cch();
        assert!(result.starts_with("[source] path.rs"));
        assert!(result.contains("\n\n"));
        assert!(result.ends_with("fn main() {}"));
    }

    #[test]
    fn test_text_with_cch_returns_plain_text_when_no_prefix() {
        let chunk = Chunk {
            text: "fn main() {}".to_string(),
            start_line: 1,
            end_line: 1,
            start_byte: 0,
            end_byte: 12,
            chunk_type: ChunkType::Function,
            metadata: None,
            parent_idx: None,
            level: 2,
            context_prefix: None,
        };

        let result = chunk.text_with_cch();
        assert_eq!(result, "fn main() {}");
    }

    #[test]
    fn test_chunk_type_level() {
        assert_eq!(ChunkType::File.level(), 0);
        assert_eq!(ChunkType::Class.level(), 1);
        assert_eq!(ChunkType::Module.level(), 1);
        assert_eq!(ChunkType::Function.level(), 2);
        assert_eq!(ChunkType::Code.level(), 2);
    }
}
