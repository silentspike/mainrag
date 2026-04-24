//! Semantic chunker using Tree-sitter AST
//! Respects function/class boundaries, fallback to token chunking for large units
//! Phase 2: Hierarchical chunking with parent-child relationships

use std::sync::Mutex;
use std::collections::HashMap;
use tree_sitter::{Parser, Tree, Node};
use tracing::{warn, info, debug};
use super::{Chunk, ChunkType, ChunkerConfig, Chunker, token::{TokenChunker, count_tokens}};

/// Semantic unit (function, class, etc.) extracted from AST
#[derive(Debug, Clone)]
struct SemanticUnit {
    kind: String,
    name: Option<String>,  // Function/class name for CCH
    text: String,
    start_byte: usize,
    end_byte: usize,
    start_line: usize,
    end_line: usize,
    /// Index of parent unit in the flat list (None for top-level)
    parent_idx: Option<usize>,
    /// Hierarchy level: 0=file, 1=class/module, 2=function
    level: u8,
}

pub struct SemanticChunker {
    max_tokens: usize,
    // Mutex for thread-safe interior mutability (Chunker trait requires Sync)
    parsers: HashMap<String, Mutex<Parser>>,
}

/// Sprint 8.1: Language-specific token limits for optimal chunk boundaries
/// Shorter functions (Python) get smaller chunks, verbose languages (Rust) get larger ones
fn language_token_limit(language: &str, default: usize) -> usize {
    match language {
        "python" | "javascript" | "js" | "jsx" | "ruby" | "lua" | "scheme" | "php" => 200,
        "rust" | "rs" | "c" | "cpp" | "cc" | "cxx" | "hpp" | "h" | "java"
        | "csharp" | "go" | "zig" | "typescript" | "ts" | "tsx" => 300,
        "markdown" | "md" | "text" | "txt" | "html" | "xml" | "css" | "sql" => 400,
        "json" | "yaml" | "toml" | "bash" => 300,
        _ => default,
    }
}

/// Sprint 8.1: Adaptive overlap based on chunk size
/// Larger chunks need more overlap for context continuity
fn adaptive_overlap(chunk_size: usize) -> usize {
    (chunk_size / 10).max(16)
}

impl SemanticChunker {
    pub fn new(config: ChunkerConfig) -> Self {
        let mut parsers = HashMap::new();

        // Initialize parser per language (wrapped in Mutex for Sync)
        // Must match parser.rs Lang enum and index.rs code_extensions
        let languages = [
            "rust", "python", "javascript", "typescript", "go", "c", "cpp", "java",
            "json", "toml", "yaml", "bash", "markdown",
            "csharp", "zig", "lua", "ruby", "php", "html", "css", "xml", "scheme", "sql",
        ];
        let total = languages.len();

        for lang in &languages {
            if let Some(parser) = Self::create_parser(lang) {
                parsers.insert(lang.to_string(), Mutex::new(parser));
                info!("Tree-sitter parser loaded: {}", lang);
            } else {
                warn!("Tree-sitter parser FAILED to load: {}", lang);
            }
        }

        info!("Tree-sitter: {}/{} parsers loaded", parsers.len(), total);

        Self {
            max_tokens: config.max_tokens.unwrap_or(256),
            parsers,
        }
    }

    fn create_parser(language: &str) -> Option<Parser> {
        let mut parser = Parser::new();
        let lang = match language {
            "rust" => tree_sitter_rust::LANGUAGE.into(),
            "python" => tree_sitter_python::LANGUAGE.into(),
            "javascript" => tree_sitter_javascript::LANGUAGE.into(),
            "typescript" => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
            "go" => tree_sitter_go::LANGUAGE.into(),
            "c" => tree_sitter_c::LANGUAGE.into(),
            "cpp" => tree_sitter_cpp::LANGUAGE.into(),
            "java" => tree_sitter_java::LANGUAGE.into(),
            "json" => tree_sitter_json::LANGUAGE.into(),
            "toml" => tree_sitter_toml_ng::LANGUAGE.into(),
            "yaml" => tree_sitter_yaml::LANGUAGE.into(),
            "bash" => tree_sitter_bash::LANGUAGE.into(),
            "markdown" => tree_sitter_md::LANGUAGE.into(),
            "csharp" => tree_sitter_c_sharp::LANGUAGE.into(),
            "zig" => tree_sitter_zig::LANGUAGE.into(),
            "lua" => tree_sitter_lua::LANGUAGE.into(),
            "ruby" => tree_sitter_ruby::LANGUAGE.into(),
            "php" => tree_sitter_php::LANGUAGE_PHP.into(),
            "html" => tree_sitter_html::LANGUAGE.into(),
            "css" => tree_sitter_css::LANGUAGE.into(),
            "xml" => tree_sitter_xml::LANGUAGE_XML.into(),
            "scheme" => tree_sitter_scheme::LANGUAGE.into(),
            "sql" => tree_sitter_sequel::LANGUAGE.into(),
            _ => return None,
        };
        parser.set_language(&lang).ok()?;
        Some(parser)
    }

    /// Extract hierarchical semantic units from AST
    /// Returns units with parent-child relationships preserved
    fn extract_hierarchical_units(&self, tree: &Tree, source: &[u8], lang: &str) -> Vec<SemanticUnit> {
        let mut units = vec![];
        let root = tree.root_node();

        // Recursively extract units starting from root
        self.extract_units_recursive(&root, source, lang, None, 0, &mut units);

        units
    }

    /// Recursively extract semantic units, tracking parent indices
    fn extract_units_recursive(
        &self,
        node: &Node,
        source: &[u8],
        lang: &str,
        parent_idx: Option<usize>,
        depth: u8,
        units: &mut Vec<SemanticUnit>,
    ) {
        let mut cursor = node.walk();

        for child in node.children(&mut cursor) {
            let kind = child.kind();

            // Check if this is a semantic unit we care about
            if let Some((chunk_type, level)) = Self::classify_node(kind, lang, depth) {
                let text = &source[child.start_byte()..child.end_byte()];
                let name = Self::extract_name(&child, source, lang);

                let unit_idx = units.len();
                units.push(SemanticUnit {
                    kind: kind.to_string(),
                    name,
                    text: String::from_utf8_lossy(text).to_string(),
                    start_byte: child.start_byte(),
                    end_byte: child.end_byte(),
                    start_line: child.start_position().row + 1,
                    end_line: child.end_position().row + 1,
                    parent_idx,
                    level,
                });

                // Only recurse into container types (classes, modules, impl blocks)
                // to find nested functions/methods
                if Self::is_container_type(chunk_type) {
                    self.extract_units_recursive(&child, source, lang, Some(unit_idx), depth + 1, units);
                }
            } else {
                // Not a semantic unit, but might contain one (e.g., expression_statement)
                self.extract_units_recursive(&child, source, lang, parent_idx, depth, units);
            }
        }
    }

    /// Classify AST node into ChunkType and hierarchy level
    fn classify_node(kind: &str, lang: &str, depth: u8) -> Option<(ChunkType, u8)> {
        // Wave 4a: SQL-specific nodes (tree-sitter-sequel uses short names, no _statement suffix)
        if lang == "sql" {
            match kind {
                "create_table" | "create_view" | "create_materialized_view" |
                "create_type" => return Some((ChunkType::Type, 1)),

                "create_function" | "create_trigger" =>
                    return Some((ChunkType::Function, 1)),

                "create_index" | "create_extension" | "create_sequence" |
                "create_schema" | "create_role" |
                "alter_table" | "alter_type" | "alter_view" | "alter_index" |
                "alter_sequence" | "alter_schema" | "alter_role" |
                "drop_table" | "drop_view" | "drop_function" | "drop_index" |
                "drop_type" | "drop_extension" |
                "insert" | "select" | "update" | "delete" |
                "comment_statement" | "set_statement" | "reset_statement" =>
                    return Some((ChunkType::Code, 1)),

                // Note: GRANT, REVOKE, CREATE POLICY are not in tree-sitter-sequel grammar.
                // They get captured by the gap-text collector instead.
                _ => {} // Fall through to generic matching
            }
        }

        match kind {
            // Functions/Methods (level 2, or level 1 if at top level)
            "function_item" | "function_definition" | "function_declaration" |
            "method_definition" | "method_declaration" | "arrow_function" => {
                let level = if depth == 0 { 1 } else { 2 };
                Some((ChunkType::Function, level))
            }

            // Classes (level 1)
            "class_definition" | "class_declaration" | "class_specifier" |
            "interface_declaration" => Some((ChunkType::Class, 1)),

            // Modules/Namespaces (level 1)
            "impl_item" | "module_definition" | "namespace_definition" => {
                Some((ChunkType::Module, 1))
            }

            // Types (level 1)
            "struct_item" | "type_declaration" | "type_spec" |
            "struct_specifier" | "union_specifier" | "enum_specifier" |
            "typedef_declaration" | "enum_item" | "enum_declaration" => {
                Some((ChunkType::Type, 1))
            }

            // Wave 4b: Top-level declarations (constants, imports, macros, type aliases)
            // Only at top level (depth==0) to avoid chunking nested items
            "const_item" | "const_declaration" | "static_item" |
            "type_item" | "type_alias_declaration" |
            "use_declaration" | "import_statement" | "import_declaration" |
            "macro_definition" | "export_statement" |
            "variable_declaration" | "lexical_declaration" => {
                if depth == 0 { Some((ChunkType::Code, 1)) } else { None }
            }

            _ => None,
        }
    }

    /// Check if a chunk type can contain nested definitions
    fn is_container_type(chunk_type: ChunkType) -> bool {
        matches!(chunk_type, ChunkType::Class | ChunkType::Module | ChunkType::Type)
    }

    /// Extract the name of a semantic unit (function name, class name, etc.)
    fn extract_name(node: &Node, source: &[u8], _lang: &str) -> Option<String> {
        // Look for identifier or name child
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            match child.kind() {
                "identifier" | "name" | "type_identifier" | "property_identifier" => {
                    let name_bytes = &source[child.start_byte()..child.end_byte()];
                    return Some(String::from_utf8_lossy(name_bytes).to_string());
                }
                // For function definitions, look one level deeper
                "function_declarator" | "declarator" => {
                    let mut inner_cursor = child.walk();
                    for inner_child in child.children(&mut inner_cursor) {
                        if inner_child.kind() == "identifier" {
                            let name_bytes = &source[inner_child.start_byte()..inner_child.end_byte()];
                            return Some(String::from_utf8_lossy(name_bytes).to_string());
                        }
                    }
                }
                _ => {}
            }
        }
        None
    }

    /// Map AST node kind to ChunkType (legacy, for non-hierarchical fallback)
    fn kind_to_type(kind: &str) -> ChunkType {
        match kind {
            // Functions/Methods
            "function_item" | "function_definition" | "function_declaration"
                | "method_definition" | "method_declaration" => ChunkType::Function,
            // Classes (OOP)
            "class_definition" | "class_declaration" | "class_specifier"
                | "interface_declaration" => ChunkType::Class,
            // Modules/Namespaces
            "impl_item" | "module_definition" | "namespace_definition" => ChunkType::Module,
            // Types (structs, enums, typedefs, unions)
            "struct_item" | "type_declaration" | "type_spec"
                | "struct_specifier" | "union_specifier" | "enum_specifier"
                | "typedef_declaration" | "enum_item" | "enum_declaration" => ChunkType::Type,
            _ => ChunkType::Code,
        }
    }

    /// Build parent context string for CCH
    fn build_parent_context(units: &[SemanticUnit], unit: &SemanticUnit) -> Option<String> {
        if let Some(parent_idx) = unit.parent_idx {
            let parent = &units[parent_idx];
            // Use parent's name or kind as context
            if let Some(ref name) = parent.name {
                Some(name.clone())
            } else {
                // Fallback to kind-based context
                Some(parent.kind.to_string())
            }
        } else {
            None
        }
    }
}

impl Chunker for SemanticChunker {
    fn chunk(&self, content: &str, language: Option<&str>) -> Vec<Chunk> {
        let lang = language.unwrap_or("text");

        // Conversation files get special conversation-aware chunking
        // Handles: Claude Code JSONL, Codex JSONL, Gemini JSON
        if lang == "jsonl" || lang == "json" {
            if content.contains(r#""type":"user""#) || content.contains(r#""type":"assistant""#)
               || content.contains(r#""type":"gemini""#) || content.contains(r#""type": "gemini""#)
               || content.contains(r#""session_id""#)
               || content.contains(r#""type":"response_item""#) || content.contains(r#""type": "response_item""#) {
                info!("Using conversation chunker for file (lang={})", lang);
                return super::jsonl::JsonlChunker::default().chunk(content, language);
            }
        }

        // Normalize language extension to parser key (e.g., "py" → "python")
        // Must match keys used in create_parser() and parser init loop
        let normalized_lang = match lang {
            "py" | "pyi" | "pyw" => "python",
            "rs" => "rust",
            "js" | "jsx" | "mjs" | "cjs" => "javascript",
            "ts" | "tsx" | "mts" | "cts" => "typescript",
            "h" => "c",
            "cpp" | "cc" | "cxx" | "c++" | "cp" | "hpp" | "hh" | "hxx" | "h++" => "cpp",
            "cs" => "csharp",
            "rb" | "rake" | "gemspec" => "ruby",
            "php" | "phtml" => "php",
            "sh" | "zsh" => "bash",
            "jsonc" => "json",
            "jsonl" => "json",
            "yml" => "yaml",
            "md" => "markdown",
            "htm" => "html",
            "scss" | "sass" => "css",
            "xsl" | "xslt" | "svg" | "xsd" => "xml",
            "scm" | "ss" | "rkt" => "scheme",
            other => other,
        };

        // Sprint 8.1: Language-adaptive token limits
        let effective_max_tokens = language_token_limit(normalized_lang, self.max_tokens);
        let effective_overlap = adaptive_overlap(effective_max_tokens);

        // Fallback to TokenChunker if no tree-sitter parser for this language
        // CRITICAL: Use normalized_lang for parser lookup, not raw extension
        let Some(parser_mutex) = self.parsers.get(normalized_lang) else {
            warn!(
                "No tree-sitter parser for '{}' (normalized: '{}'), falling back to token chunking",
                lang, normalized_lang
            );
            let fallback = TokenChunker::new(ChunkerConfig {
                max_tokens: Some(effective_max_tokens),
                overlap_tokens: Some(effective_overlap),
                ..Default::default()
            });
            return fallback.chunk(content, language);
        };

        // M1: Mutex::lock() — handle poison instead of panic
        let mut parser = match parser_mutex.lock() {
            Ok(guard) => guard,
            Err(poisoned) => {
                warn!("Tree-sitter parser mutex poisoned for '{}', recovering", lang);
                poisoned.into_inner()
            }
        };
        let tree = match parser.parse(content, None) {
            Some(t) => t,
            None => {
                warn!("Tree-sitter parse failed for '{}', falling back to token chunking", lang);
                return TokenChunker::default().chunk(content, language);
            }
        };

        // Extract hierarchical semantic units
        let units = self.extract_hierarchical_units(&tree, content.as_bytes(), normalized_lang);

        debug!("Extracted {} hierarchical semantic units", units.len());

        // Convert semantic units to chunks, preserving hierarchy
        let mut chunks = vec![];

        // Build a mapping from old unit index to new chunk index
        // (needed because large units get split into multiple chunks)
        let mut unit_to_chunk_idx: HashMap<usize, usize> = HashMap::new();

        for (unit_idx, unit) in units.iter().enumerate() {
            // Map parent index from unit space to chunk space
            let parent_chunk_idx = unit.parent_idx.and_then(|p| unit_to_chunk_idx.get(&p).copied());

            // Build parent context for CCH
            let parent_context = Self::build_parent_context(&units, unit);

            if count_tokens(&unit.text) > effective_max_tokens {
                // Unit too large → use token chunker for this unit (with adaptive limits)
                let sub_chunks = TokenChunker::new(ChunkerConfig {
                    max_tokens: Some(effective_max_tokens),
                    overlap_tokens: Some(effective_overlap),
                    ..Default::default()
                }).chunk(&unit.text, language);

                let first_chunk_idx = chunks.len();
                unit_to_chunk_idx.insert(unit_idx, first_chunk_idx);

                for (sub_idx, mut sub) in sub_chunks.into_iter().enumerate() {
                    sub.start_line += unit.start_line - 1;
                    sub.end_line += unit.start_line - 1;
                    sub.chunk_type = Self::kind_to_type(&unit.kind);
                    sub.level = unit.level;
                    // First sub-chunk inherits parent reference, others point to first
                    sub.parent_idx = if sub_idx == 0 {
                        parent_chunk_idx
                    } else {
                        Some(first_chunk_idx)
                    };
                    sub.metadata = Some(serde_json::json!({
                        "semantic_unit": unit.kind,
                        "name": unit.name,
                        "sub_chunk": sub_idx,
                    }));
                    chunks.push(sub);
                }
            } else {
                unit_to_chunk_idx.insert(unit_idx, chunks.len());

                chunks.push(Chunk {
                    text: unit.text.clone(),
                    start_line: unit.start_line,
                    end_line: unit.end_line,
                    start_byte: unit.start_byte,
                    end_byte: unit.end_byte,
                    chunk_type: Self::kind_to_type(&unit.kind),
                    level: unit.level,
                    parent_idx: parent_chunk_idx,
                    context_prefix: None,  // Will be set by IndexService with source context
                    metadata: Some(serde_json::json!({
                        "semantic_unit": unit.kind,
                        "name": unit.name,
                        "parent_context": parent_context,
                    })),
                });
            }
        }

        // Fallback if no semantic units found
        if chunks.is_empty() {
            return TokenChunker::default().chunk(content, language);
        }

        // Wave 4b: Collect gap text between semantic units.
        // Uncovered regions (e.g., GRANT/REVOKE/CREATE POLICY in SQL, or top-level
        // comments/constants in other languages) become additional chunks.
        let mut covered: Vec<(usize, usize)> = units.iter()
            .map(|u| (u.start_byte, u.end_byte))
            .collect();
        covered.sort_by_key(|&(s, _)| s);

        let content_bytes = content.as_bytes();
        let mut gap_start = 0usize;
        for &(unit_start, unit_end) in &covered {
            if unit_start > gap_start {
                let gap_text = String::from_utf8_lossy(&content_bytes[gap_start..unit_start]).to_string();
                let trimmed = gap_text.trim();
                // Only create a chunk if the gap has meaningful content (>20 chars, not just whitespace/comments)
                if trimmed.len() > 20 {
                    let start_line = content[..gap_start].matches('\n').count() + 1;
                    let end_line = content[..unit_start].matches('\n').count() + 1;

                    // If gap is too large, split with token chunker
                    if count_tokens(trimmed) > effective_max_tokens {
                        let sub_chunks = TokenChunker::new(ChunkerConfig {
                            max_tokens: Some(effective_max_tokens),
                            overlap_tokens: Some(effective_overlap),
                            ..Default::default()
                        }).chunk(trimmed, language);
                        for mut sub in sub_chunks {
                            sub.start_line += start_line - 1;
                            sub.end_line += start_line - 1;
                            sub.chunk_type = ChunkType::Code;
                            sub.level = 1;
                            chunks.push(sub);
                        }
                    } else {
                        chunks.push(Chunk {
                            text: trimmed.to_string(),
                            start_line,
                            end_line,
                            start_byte: gap_start,
                            end_byte: unit_start,
                            chunk_type: ChunkType::Code,
                            level: 1,
                            parent_idx: None,
                            context_prefix: None,
                            metadata: Some(serde_json::json!({"semantic_unit": "gap_text"})),
                        });
                    }
                }
            }
            gap_start = gap_start.max(unit_end);
        }
        // Trailing gap after last unit
        if gap_start < content_bytes.len() {
            let gap_text = String::from_utf8_lossy(&content_bytes[gap_start..]).to_string();
            let trimmed = gap_text.trim();
            if trimmed.len() > 20 {
                let start_line = content[..gap_start].matches('\n').count() + 1;
                let end_line = content.matches('\n').count() + 1;

                if count_tokens(trimmed) > effective_max_tokens {
                    let sub_chunks = TokenChunker::new(ChunkerConfig {
                        max_tokens: Some(effective_max_tokens),
                        overlap_tokens: Some(effective_overlap),
                        ..Default::default()
                    }).chunk(trimmed, language);
                    for mut sub in sub_chunks {
                        sub.start_line += start_line - 1;
                        sub.end_line += start_line - 1;
                        sub.chunk_type = ChunkType::Code;
                        sub.level = 1;
                        chunks.push(sub);
                    }
                } else {
                    chunks.push(Chunk {
                        text: trimmed.to_string(),
                        start_line,
                        end_line,
                        start_byte: gap_start,
                        end_byte: content_bytes.len(),
                        chunk_type: ChunkType::Code,
                        level: 1,
                        parent_idx: None,
                        context_prefix: None,
                        metadata: Some(serde_json::json!({"semantic_unit": "gap_text"})),
                    });
                }
            }
        }

        chunks
    }

    fn name(&self) -> &str {
        "semantic"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_semantic_chunking_rust() {
        let chunker = SemanticChunker::new(ChunkerConfig::default());
        let content = r#"
        fn main() {
            println!("Hello, world!");
        }

        fn helper() {
            // helper code
        }
        "#;

        let chunks = chunker.chunk(content, Some("rust"));
        // Should have at least some chunks
        assert!(!chunks.is_empty());
    }

    #[test]
    fn test_semantic_chunking_fallback() {
        let chunker = SemanticChunker::new(ChunkerConfig::default());
        let content = "Some random text without semantic structure";

        let chunks = chunker.chunk(content, Some("unknown_lang"));
        // Should fallback gracefully
        assert!(!chunks.is_empty());
    }

    #[test]
    fn test_semantic_chunking_c() {
        let chunker = SemanticChunker::new(ChunkerConfig::default());
        let content = r#"
struct Point {
    int x;
    int y;
};

enum Color {
    RED,
    GREEN,
    BLUE
};

void print_point(struct Point p) {
    printf("Point: (%d, %d)\n", p.x, p.y);
}

int main() {
    return 0;
}
        "#;

        let chunks = chunker.chunk(content, Some("c"));
        assert!(!chunks.is_empty(), "No chunks found for C code");

        // Verify we found struct (Type)
        assert!(
            chunks.iter().any(|c| c.chunk_type == ChunkType::Type),
            "No Type chunk found in C file (expected struct or enum)"
        );

        // Verify we found function
        assert!(
            chunks.iter().any(|c| c.chunk_type == ChunkType::Function),
            "No Function chunk found in C file"
        );
    }

    #[test]
    fn test_semantic_chunking_cpp() {
        let chunker = SemanticChunker::new(ChunkerConfig::default());
        // Simpler C++ content - classes at top level (not inside namespace)
        let content = r#"
class Shape {
public:
    virtual double area() const = 0;
};

class Rectangle : public Shape {
private:
    double width;
public:
    double area() const override {
        return width * 2;
    }
};

namespace geometry {
    int helper() { return 42; }
}

void print_hello() {
    int x = 1;
}
        "#;

        let chunks = chunker.chunk(content, Some("cpp"));
        assert!(!chunks.is_empty(), "No chunks found for C++ code");

        // Verify we found class (Class)
        assert!(
            chunks.iter().any(|c| c.chunk_type == ChunkType::Class),
            "No Class chunk found in C++ file"
        );

        // Verify we found namespace (Module)
        assert!(
            chunks.iter().any(|c| c.chunk_type == ChunkType::Module),
            "No Module chunk found in C++ file (expected namespace)"
        );
    }

    #[test]
    fn test_semantic_chunking_java() {
        let chunker = SemanticChunker::new(ChunkerConfig::default());
        let content = r#"
interface Drawable {
    void draw();
}

enum Status {
    PENDING,
    ACTIVE,
    COMPLETED
}

public class Sample implements Drawable {
    private String name;

    public Sample(String name) {
        this.name = name;
    }

    @Override
    public void draw() {
        System.out.println("Drawing: " + name);
    }

    public static void main(String[] args) {
        Sample sample = new Sample("Test");
        sample.draw();
    }
}
        "#;

        let chunks = chunker.chunk(content, Some("java"));
        assert!(!chunks.is_empty(), "No chunks found for Java code");

        // Verify we found class (Class)
        assert!(
            chunks.iter().any(|c| c.chunk_type == ChunkType::Class),
            "No Class chunk found in Java file"
        );

        // Verify we found interface (Class)
        let has_interface = chunks.iter().any(|c| {
            c.chunk_type == ChunkType::Class &&
            c.metadata.as_ref().map_or(false, |m| {
                m.get("semantic_unit").map_or(false, |v| v == "interface_declaration")
            })
        });
        assert!(has_interface, "No interface chunk found in Java file");

        // Verify we found enum (Type)
        assert!(
            chunks.iter().any(|c| c.chunk_type == ChunkType::Type),
            "No Type chunk found in Java file (expected enum)"
        );
    }

    #[test]
    fn test_c_header_extension() {
        // Test that .h files are recognized as C
        let chunker = SemanticChunker::new(ChunkerConfig::default());
        let content = r#"
struct Config {
    int value;
};
        "#;

        let chunks = chunker.chunk(content, Some("h"));
        assert!(!chunks.is_empty(), "No chunks found for .h file");
        assert!(
            chunks.iter().any(|c| c.chunk_type == ChunkType::Type),
            "No struct chunk found in .h file"
        );
    }

    #[test]
    fn test_cpp_extensions() {
        // Test that various C++ extensions work
        let chunker = SemanticChunker::new(ChunkerConfig::default());
        let content = r#"
class Test {
public:
    void method() {}
};
        "#;

        for ext in &["cpp", "cc", "cxx", "hpp"] {
            let chunks = chunker.chunk(content, Some(ext));
            assert!(
                !chunks.is_empty(),
                "No chunks found for .{} file", ext
            );
            assert!(
                chunks.iter().any(|c| c.chunk_type == ChunkType::Class),
                "No Class chunk found in .{} file", ext
            );
        }
    }

    #[test]
    fn test_hierarchical_chunking_rust() {
        let chunker = SemanticChunker::new(ChunkerConfig::default());
        let content = r#"
struct Database {
    url: String,
}

impl Database {
    fn new(url: &str) -> Self {
        Self { url: url.to_string() }
    }

    fn connect(&self) -> Result<(), Error> {
        Ok(())
    }
}
        "#;

        let chunks = chunker.chunk(content, Some("rust"));
        assert!(!chunks.is_empty(), "No chunks found for Rust code");

        // Find the impl block
        let impl_chunk = chunks.iter().find(|c| c.chunk_type == ChunkType::Module);
        assert!(impl_chunk.is_some(), "No impl (Module) chunk found");

        // Find functions that have a parent (nested in impl)
        let nested_funcs: Vec<_> = chunks.iter()
            .filter(|c| c.chunk_type == ChunkType::Function && c.parent_idx.is_some())
            .collect();

        // Should have at least one nested function (new or connect)
        assert!(!nested_funcs.is_empty(), "No nested functions found in impl block");
    }

    #[test]
    fn test_language_token_limits() {
        assert_eq!(language_token_limit("python", 256), 200);
        assert_eq!(language_token_limit("javascript", 256), 200);
        assert_eq!(language_token_limit("rust", 256), 300);
        assert_eq!(language_token_limit("c", 256), 300);
        assert_eq!(language_token_limit("java", 256), 300);
        assert_eq!(language_token_limit("markdown", 256), 400);
        assert_eq!(language_token_limit("yaml", 256), 300); // yaml is explicitly mapped
        assert_eq!(language_token_limit("unknown_lang", 256), 256); // actual default fallback
    }

    #[test]
    fn test_adaptive_overlap() {
        assert_eq!(adaptive_overlap(200), 20);  // 200/10 = 20
        assert_eq!(adaptive_overlap(300), 30);  // 300/10 = 30
        assert_eq!(adaptive_overlap(400), 40);  // 400/10 = 40
        assert_eq!(adaptive_overlap(100), 16);  // 100/10 = 10 < 16, clamped to 16
    }

    #[test]
    fn test_hierarchical_chunking_python() {
        let chunker = SemanticChunker::new(ChunkerConfig::default());
        let content = r#"
class Calculator:
    def __init__(self):
        self.result = 0

    def add(self, x: int) -> int:
        self.result += x
        return self.result

    def reset(self):
        self.result = 0
"#;

        let chunks = chunker.chunk(content, Some("python"));
        assert!(!chunks.is_empty(), "No chunks found for Python code");

        // Find the class
        let class_chunk = chunks.iter().find(|c| c.chunk_type == ChunkType::Class);
        assert!(class_chunk.is_some(), "No Class chunk found");

        // Find methods that have a parent (nested in class)
        let methods: Vec<_> = chunks.iter()
            .filter(|c| c.chunk_type == ChunkType::Function && c.parent_idx.is_some())
            .collect();

        // Should have methods (__init__, add, reset)
        assert!(methods.len() >= 2, "Expected at least 2 methods in class, found {}", methods.len());
    }

    /// Verify ALL 22 tree-sitter parsers can be initialized (ABI 15 compatibility check)
    #[test]
    fn test_all_parsers_initialize() {
        let expected_languages = [
            "rust", "python", "javascript", "typescript", "go", "c", "cpp", "java",
            "json", "toml", "yaml", "bash", "markdown",
            "csharp", "zig", "lua", "ruby", "php", "html", "css", "xml", "scheme", "sql",
        ];
        let mut failed = vec![];
        for lang in &expected_languages {
            if SemanticChunker::create_parser(lang).is_none() {
                failed.push(*lang);
            }
        }
        assert!(
            failed.is_empty(),
            "Tree-sitter parsers FAILED to initialize (ABI mismatch?): {:?}",
            failed
        );
    }
}
