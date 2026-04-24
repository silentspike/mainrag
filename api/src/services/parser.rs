//! Tree-Sitter based code parser for symbol and call graph extraction.
//!
//! Supported languages: Rust, Python, JavaScript, TypeScript, Go, C, C++, Java,
//! JSON, TOML, YAML, Bash, Markdown.

use anyhow::{anyhow, Result};
use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;
use tree_sitter::{Language, Parser, Tree};

/// Supported programming languages
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Lang {
    Rust,
    Python,
    JavaScript,
    TypeScript,
    Go,
    C,
    Cpp,
    Java,
    Json,
    Jsonl, // JSONL conversations (Claude Code, Codex)
    Toml,
    Yaml,
    Bash,
    Markdown,
    // Additional languages for CodeRag feature parity (A.6)
    CSharp,
    Zig,
    Lua,
    Ruby,
    Php,
    Html,
    Css,
    Xml,
    Dockerfile,
    Scheme,
    Sql,
    Unknown,
}

impl Lang {
    /// Detect language from file extension
    pub fn from_extension(ext: &str) -> Self {
        match ext.to_lowercase().as_str() {
            // Rust
            "rs" => Lang::Rust,
            // Python (including Windows .pyw)
            "py" | "pyi" | "pyw" => Lang::Python,
            // JavaScript (including JSX for React)
            "js" | "jsx" | "mjs" | "cjs" => Lang::JavaScript,
            // TypeScript (including module variants)
            "ts" | "tsx" | "mts" | "cts" => Lang::TypeScript,
            // Go
            "go" => Lang::Go,
            // C (headers go to C by default)
            "c" => Lang::C,
            // C++ (including all common extensions and headers)
            "cpp" | "cc" | "cxx" | "c++" | "cp" | "h" | "hpp" | "hh" | "hxx" | "h++" => Lang::Cpp,
            // Java
            "java" => Lang::Java,
            // JSON (including JSONC)
            "json" | "jsonc" => Lang::Json,
            // JSONL (conversation logs: Claude Code, Codex)
            "jsonl" => Lang::Jsonl,
            // TOML
            "toml" => Lang::Toml,
            // YAML
            "yaml" | "yml" => Lang::Yaml,
            // Shell (including zsh)
            "sh" | "bash" | "zsh" => Lang::Bash,
            // Markdown
            "md" | "markdown" => Lang::Markdown,
            // Additional languages for CodeRag feature parity (A.6)
            "cs" => Lang::CSharp,
            "zig" => Lang::Zig,
            "lua" => Lang::Lua,
            "rb" | "rake" | "gemspec" => Lang::Ruby,
            "php" | "phtml" => Lang::Php,
            "html" | "htm" => Lang::Html,
            "css" | "scss" | "sass" => Lang::Css,
            "xml" | "xsl" | "xslt" | "svg" => Lang::Xml,
            "dockerfile" => Lang::Dockerfile,
            "scm" | "ss" | "rkt" => Lang::Scheme,
            "sql" => Lang::Sql,
            _ => Lang::Unknown,
        }
    }

    /// Detect language from file path
    pub fn from_path(path: &Path) -> Self {
        // Special case: Dockerfile has no extension
        if let Some(filename) = path.file_name().and_then(|n| n.to_str()) {
            let lower = filename.to_lowercase();
            if lower == "dockerfile" || lower.starts_with("dockerfile.") {
                return Lang::Dockerfile;
            }
        }

        path.extension()
            .and_then(|e| e.to_str())
            .map(Self::from_extension)
            .unwrap_or(Lang::Unknown)
    }

    /// Get tree-sitter Language (tree-sitter 0.23+ API)
    fn ts_language(&self) -> Option<Language> {
        match self {
            Lang::Rust => Some(tree_sitter_rust::LANGUAGE.into()),
            Lang::Python => Some(tree_sitter_python::LANGUAGE.into()),
            Lang::JavaScript => Some(tree_sitter_javascript::LANGUAGE.into()),
            Lang::TypeScript => {
                // tree-sitter-typescript v0.23 exports LANGUAGE_TYPESCRIPT
                // For TSX files, we'd need separate Lang::Tsx variant
                // For now, use TypeScript language for both .ts and .tsx
                Some(tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into())
            }
            Lang::Go => Some(tree_sitter_go::LANGUAGE.into()),
            Lang::C => Some(tree_sitter_c::LANGUAGE.into()),
            Lang::Cpp => Some(tree_sitter_cpp::LANGUAGE.into()),
            Lang::Java => Some(tree_sitter_java::LANGUAGE.into()),
            Lang::Json => Some(tree_sitter_json::LANGUAGE.into()),
            Lang::Jsonl => None, // JSONL uses line-by-line parsing, not tree-sitter
            Lang::Toml => {
                // tree-sitter-toml-ng 0.7 is compatible with tree-sitter 0.24
                Some(tree_sitter_toml_ng::LANGUAGE.into())
            }
            Lang::Yaml => Some(tree_sitter_yaml::LANGUAGE.into()),
            Lang::Bash => Some(tree_sitter_bash::LANGUAGE.into()),
            Lang::Markdown => {
                // tree-sitter-md 0.5 is compatible with tree-sitter 0.24
                Some(tree_sitter_md::LANGUAGE.into())
            }
            // Additional languages for CodeRag feature parity (A.6)
            Lang::CSharp => Some(tree_sitter_c_sharp::LANGUAGE.into()),
            Lang::Zig => Some(tree_sitter_zig::LANGUAGE.into()),
            Lang::Lua => Some(tree_sitter_lua::LANGUAGE.into()),
            Lang::Ruby => Some(tree_sitter_ruby::LANGUAGE.into()),
            Lang::Php => Some(tree_sitter_php::LANGUAGE_PHP.into()),
            Lang::Html => Some(tree_sitter_html::LANGUAGE.into()),
            Lang::Css => Some(tree_sitter_css::LANGUAGE.into()),
            Lang::Xml => Some(tree_sitter_xml::LANGUAGE_XML.into()),
            Lang::Scheme => Some(tree_sitter_scheme::LANGUAGE.into()),
            // TODO: tree-sitter-dockerfile stuck at ABI 14, waiting for 0.26 compatible crate
            Lang::Dockerfile => None,
            // tree-sitter-sequel ~0.25 (testing 0.26 compatibility)
            Lang::Sql => Some(tree_sitter_sequel::LANGUAGE.into()),
            Lang::Unknown => None,
        }
    }
}

impl std::fmt::Display for Lang {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Lang::Rust => write!(f, "rust"),
            Lang::Python => write!(f, "python"),
            Lang::JavaScript => write!(f, "javascript"),
            Lang::TypeScript => write!(f, "typescript"),
            Lang::Go => write!(f, "go"),
            Lang::C => write!(f, "c"),
            Lang::Cpp => write!(f, "cpp"),
            Lang::Java => write!(f, "java"),
            Lang::Json => write!(f, "json"),
            Lang::Jsonl => write!(f, "jsonl"),
            Lang::Toml => write!(f, "toml"),
            Lang::Yaml => write!(f, "yaml"),
            Lang::Bash => write!(f, "bash"),
            Lang::Markdown => write!(f, "markdown"),
            // Additional languages for CodeRag feature parity (A.6)
            Lang::CSharp => write!(f, "c_sharp"),
            Lang::Zig => write!(f, "zig"),
            Lang::Lua => write!(f, "lua"),
            Lang::Ruby => write!(f, "ruby"),
            Lang::Php => write!(f, "php"),
            Lang::Html => write!(f, "html"),
            Lang::Css => write!(f, "css"),
            Lang::Xml => write!(f, "xml"),
            Lang::Dockerfile => write!(f, "dockerfile"),
            Lang::Scheme => write!(f, "scheme"),
            Lang::Sql => write!(f, "sql"),
            Lang::Unknown => write!(f, "unknown"),
        }
    }
}

/// Symbol types extracted from code
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SymbolType {
    Function,
    Method,
    Class,
    Struct,
    Enum,
    Interface,
    Trait,
    Module,
    Constant,
    Variable,
    Type,
    Import,
    // Conversation types (JSONL)
    Message,       // User/Assistant message from conversations
    ThinkingBlock, // Claude's <thinking> blocks
}

impl std::fmt::Display for SymbolType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SymbolType::Function => write!(f, "function"),
            SymbolType::Method => write!(f, "method"),
            SymbolType::Class => write!(f, "class"),
            SymbolType::Struct => write!(f, "struct"),
            SymbolType::Enum => write!(f, "enum"),
            SymbolType::Interface => write!(f, "interface"),
            SymbolType::Trait => write!(f, "trait"),
            SymbolType::Module => write!(f, "module"),
            SymbolType::Constant => write!(f, "constant"),
            SymbolType::Variable => write!(f, "variable"),
            SymbolType::Type => write!(f, "type"),
            SymbolType::Import => write!(f, "import"),
            SymbolType::Message => write!(f, "message"),
            SymbolType::ThinkingBlock => write!(f, "thinking_block"),
        }
    }
}

/// Extracted symbol from source code
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ExtractedSymbol {
    pub name: String,
    pub qualified_name: Option<String>,
    pub symbol_type: SymbolType,
    pub line_start: u32,
    pub line_end: u32,
    pub column_start: u32,
    pub column_end: u32,
    pub signature: Option<String>,
    pub doc_comment: Option<String>,
    pub visibility: Option<String>,
    pub language: String,
}

/// Call graph entry - who calls whom
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ExtractedCall {
    pub caller_name: String,
    pub callee_name: String,
    pub call_type: CallType,
    pub call_line: u32,
    pub call_column: u32,
}

/// Type of function call
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallType {
    Direct,      // foo()
    Method,      // obj.foo()
    Static,      // Class::foo()
    Constructor, // new Foo()
}

impl std::fmt::Display for CallType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CallType::Direct => write!(f, "direct"),
            CallType::Method => write!(f, "method"),
            CallType::Static => write!(f, "static"),
            CallType::Constructor => write!(f, "constructor"),
        }
    }
}

/// Parsing result for a single file
#[derive(Debug, Default)]
pub struct ParseResult {
    pub symbols: Vec<ExtractedSymbol>,
    pub calls: Vec<ExtractedCall>,
    #[allow(dead_code)]
    pub language: String,
}

/// Tree-sitter based code parser with per-language locking.
/// Each language has its own Mutex<Parser>, allowing concurrent parsing of different languages.
pub struct CodeParser {
    parsers: HashMap<Lang, Mutex<Parser>>,
}

impl CodeParser {
    /// Create a new CodeParser with all supported languages
    pub fn new() -> Result<Self> {
        let mut parsers = HashMap::new();

        for lang in [
            Lang::Rust,
            Lang::Python,
            Lang::JavaScript,
            Lang::TypeScript,
            Lang::Go,
            Lang::C,
            Lang::Cpp,
            Lang::Java,
            Lang::Json,
            Lang::Toml,
            Lang::Yaml,
            Lang::Bash,
            Lang::Markdown,
            Lang::CSharp,
            Lang::Zig,
            Lang::Lua,
            Lang::Ruby,
            Lang::Php,
            Lang::Html,
            Lang::Css,
            Lang::Xml,
            Lang::Scheme,
            Lang::Sql,
        ] {
            if let Some(ts_lang) = lang.ts_language() {
                let mut parser = Parser::new();
                parser.set_language(&ts_lang)?;
                parsers.insert(lang, Mutex::new(parser));
            }
        }

        Ok(Self { parsers })
    }

    /// Parse a file and extract symbols + call graph.
    /// Thread-safe: only locks the parser for the specific language being parsed.
    pub fn parse_file(&self, path: &Path, content: &str) -> Result<ParseResult> {
        let lang = Lang::from_path(path);
        if lang == Lang::Unknown {
            return Ok(ParseResult::default());
        }

        // JSONL files use line-by-line parsing, not tree-sitter
        if lang == Lang::Jsonl {
            return Ok(self.extract_jsonl(content, path));
        }

        // JSON files that are Gemini conversations get conversation parsing
        if lang == Lang::Json
            && content.contains(r#""messages""#)
            && (content.contains(r#""type":"gemini""#) || content.contains(r#""type": "gemini""#))
        {
            return Ok(self.extract_gemini_json(content, path));
        }

        let parser_mutex = self
            .parsers
            .get(&lang)
            .ok_or_else(|| anyhow!("No parser for language: {}", lang))?;

        let tree = {
            let mut parser = parser_mutex
                .lock()
                .map_err(|e| anyhow!("Parser lock poisoned for {:?}: {}", lang, e))?;
            parser
                .parse(content, None)
                .ok_or_else(|| anyhow!("Failed to parse file: {}", path.display()))?
        };

        let mut result = ParseResult {
            language: lang.to_string(),
            ..Default::default()
        };

        // Extract symbols
        self.extract_symbols(&tree, content, lang, &mut result)?;

        // Extract call graph
        self.extract_calls(&tree, content, lang, &mut result)?;

        Ok(result)
    }

    /// Extract symbols (functions, classes, etc.) from AST
    pub fn extract_symbols(
        &self,
        tree: &Tree,
        source: &str,
        lang: Lang,
        result: &mut ParseResult,
    ) -> Result<()> {
        let root = tree.root_node();
        let mut cursor = root.walk();
        self.walk_for_symbols(&mut cursor, source, lang, result, None);
        Ok(())
    }

    /// Recursively walk AST for symbols
    fn walk_for_symbols(
        &self,
        cursor: &mut tree_sitter::TreeCursor,
        source: &str,
        lang: Lang,
        result: &mut ParseResult,
        parent_name: Option<&str>,
    ) {
        loop {
            let node = cursor.node();

            if let Some(symbol) = self.node_to_symbol(&node, source, lang, parent_name) {
                let new_parent = if matches!(
                    symbol.symbol_type,
                    SymbolType::Class | SymbolType::Struct | SymbolType::Trait | SymbolType::Module
                ) {
                    Some(symbol.name.clone())
                } else {
                    None
                };

                result.symbols.push(symbol);

                if cursor.goto_first_child() {
                    self.walk_for_symbols(
                        cursor,
                        source,
                        lang,
                        result,
                        new_parent.as_deref().or(parent_name),
                    );
                    cursor.goto_parent();
                }
            } else if cursor.goto_first_child() {
                self.walk_for_symbols(cursor, source, lang, result, parent_name);
                cursor.goto_parent();
            }

            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }

    /// Convert AST node to Symbol if it's a definition
    fn node_to_symbol(
        &self,
        node: &tree_sitter::Node,
        source: &str,
        lang: Lang,
        parent_name: Option<&str>,
    ) -> Option<ExtractedSymbol> {
        let kind = node.kind();

        let (symbol_type, name_child_kind) = match lang {
            Lang::Rust => match kind {
                "function_item" => (SymbolType::Function, Some("name")),
                "struct_item" => (SymbolType::Struct, Some("name")),
                "enum_item" => (SymbolType::Enum, Some("name")),
                "trait_item" => (SymbolType::Trait, Some("name")),
                "impl_item" => (SymbolType::Type, None),
                "mod_item" => (SymbolType::Module, Some("name")),
                "const_item" => (SymbolType::Constant, Some("name")),
                "static_item" => (SymbolType::Variable, Some("name")),
                "type_item" => (SymbolType::Type, Some("name")),
                _ => return None,
            },
            Lang::Python => match kind {
                "function_definition" => (SymbolType::Function, Some("name")),
                "class_definition" => (SymbolType::Class, Some("name")),
                _ => return None,
            },
            Lang::JavaScript | Lang::TypeScript => match kind {
                "function_declaration" => (SymbolType::Function, Some("name")),
                "class_declaration" => (SymbolType::Class, Some("name")),
                "method_definition" => (SymbolType::Method, Some("name")),
                "arrow_function" => (SymbolType::Function, None),
                "variable_declarator" => (SymbolType::Variable, Some("name")),
                _ => return None,
            },
            Lang::Go => match kind {
                "function_declaration" => (SymbolType::Function, Some("name")),
                "method_declaration" => (SymbolType::Method, Some("name")),
                "type_declaration" => (SymbolType::Type, None),
                "type_spec" => (SymbolType::Struct, Some("name")),
                _ => return None,
            },
            Lang::Java => match kind {
                "method_declaration" => (SymbolType::Method, Some("name")),
                "class_declaration" => (SymbolType::Class, Some("name")),
                "interface_declaration" => (SymbolType::Interface, Some("name")),
                "enum_declaration" => (SymbolType::Enum, Some("name")),
                _ => return None,
            },
            Lang::C | Lang::Cpp => match kind {
                "function_definition" => (SymbolType::Function, Some("declarator")),
                "struct_specifier" => (SymbolType::Struct, Some("name")),
                "enum_specifier" => (SymbolType::Enum, Some("name")),
                "class_specifier" => (SymbolType::Class, Some("name")),
                _ => return None,
            },
            // Additional languages for CodeRag feature parity (A.6)
            Lang::CSharp => match kind {
                "method_declaration" => (SymbolType::Method, Some("name")),
                "class_declaration" => (SymbolType::Class, Some("name")),
                "struct_declaration" => (SymbolType::Struct, Some("name")),
                "interface_declaration" => (SymbolType::Interface, Some("name")),
                "enum_declaration" => (SymbolType::Enum, Some("name")),
                "property_declaration" => (SymbolType::Variable, Some("name")),
                "namespace_declaration" => (SymbolType::Module, Some("name")),
                _ => return None,
            },
            Lang::Ruby => match kind {
                "method" => (SymbolType::Method, Some("name")),
                "singleton_method" => (SymbolType::Method, Some("name")),
                "class" => (SymbolType::Class, Some("name")),
                "module" => (SymbolType::Module, Some("name")),
                _ => return None,
            },
            Lang::Php => match kind {
                "function_definition" => (SymbolType::Function, Some("name")),
                "method_declaration" => (SymbolType::Method, Some("name")),
                "class_declaration" => (SymbolType::Class, Some("name")),
                "interface_declaration" => (SymbolType::Interface, Some("name")),
                "trait_declaration" => (SymbolType::Trait, Some("name")),
                _ => return None,
            },
            Lang::Lua => match kind {
                "function_declaration" => (SymbolType::Function, Some("name")),
                "local_function_declaration" => (SymbolType::Function, Some("name")),
                "function_definition" => (SymbolType::Function, None),
                _ => return None,
            },
            Lang::Zig => match kind {
                "fn_decl" => (SymbolType::Function, Some("name")),
                "TopLevelDecl" => (SymbolType::Function, Some("name")),
                "struct_decl" => (SymbolType::Struct, Some("name")),
                "enum_decl" => (SymbolType::Enum, Some("name")),
                _ => return None,
            },
            Lang::Scheme => match kind {
                "define" => (SymbolType::Function, None), // (define name ...)
                "lambda" => (SymbolType::Function, None),
                _ => return None,
            },
            Lang::Sql => match kind {
                "create_function_statement" => (SymbolType::Function, Some("name")),
                "create_table_statement" => (SymbolType::Type, Some("name")),
                "create_view_statement" => (SymbolType::Type, Some("name")),
                "create_index_statement" => (SymbolType::Type, Some("name")),
                _ => return None,
            },
            // Languages without meaningful symbol extraction (markup/config)
            Lang::Html | Lang::Css | Lang::Xml | Lang::Dockerfile => return None,
            _ => return None,
        };

        let name = if let Some(name_kind) = name_child_kind {
            node.child_by_field_name(name_kind)
                .and_then(|n| n.utf8_text(source.as_bytes()).ok())
                .map(|s| s.to_string())
        } else {
            None
        };

        let name = name?;
        let qualified_name = parent_name.map(|p| format!("{}::{}", p, name));

        let visibility = if lang == Lang::Rust {
            node.child_by_field_name("visibility")
                .and_then(|n| n.utf8_text(source.as_bytes()).ok())
                .map(|s| s.to_string())
        } else if lang == Lang::Java {
            // Java: modifiers are child nodes (public, private, protected, static, etc.)
            extract_java_visibility(node, source)
        } else {
            None
        };

        let start_line = node.start_position().row;
        let signature = source.lines().nth(start_line).map(|l| l.trim().to_string());

        Some(ExtractedSymbol {
            name,
            qualified_name,
            symbol_type,
            line_start: node.start_position().row as u32 + 1,
            line_end: node.end_position().row as u32 + 1,
            column_start: node.start_position().column as u32,
            column_end: node.end_position().column as u32,
            signature,
            doc_comment: None,
            visibility,
            language: lang.to_string(),
        })
    }

    /// Extract function calls from AST
    pub fn extract_calls(
        &self,
        tree: &Tree,
        source: &str,
        lang: Lang,
        result: &mut ParseResult,
    ) -> Result<()> {
        let root = tree.root_node();
        let mut cursor = root.walk();
        let mut current_function: Option<String> = None;
        self.walk_for_calls(&mut cursor, source, lang, result, &mut current_function);
        Ok(())
    }

    /// Recursively walk AST for function calls
    fn walk_for_calls(
        &self,
        cursor: &mut tree_sitter::TreeCursor,
        source: &str,
        lang: Lang,
        result: &mut ParseResult,
        current_function: &mut Option<String>,
    ) {
        loop {
            let node = cursor.node();
            let kind = node.kind();

            let is_function_def = matches!(
                (lang, kind),
                (Lang::Rust, "function_item")
                    | (Lang::Python, "function_definition")
                    | (Lang::JavaScript | Lang::TypeScript, "function_declaration")
                    | (Lang::Go, "function_declaration" | "method_declaration")
                    | (Lang::Java, "method_declaration")
                    | (Lang::C | Lang::Cpp, "function_definition")
            );

            let prev_function = current_function.clone();
            if is_function_def {
                if let Some(name) = node
                    .child_by_field_name("name")
                    .and_then(|n| n.utf8_text(source.as_bytes()).ok())
                {
                    *current_function = Some(name.to_string());
                }
            }

            if let Some(call) = self.node_to_call(&node, source, lang, current_function) {
                result.calls.push(call);
            }

            if cursor.goto_first_child() {
                self.walk_for_calls(cursor, source, lang, result, current_function);
                cursor.goto_parent();
            }

            if is_function_def {
                *current_function = prev_function;
            }

            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }

    /// Convert call expression node to ExtractedCall
    fn node_to_call(
        &self,
        node: &tree_sitter::Node,
        source: &str,
        lang: Lang,
        current_function: &Option<String>,
    ) -> Option<ExtractedCall> {
        let kind = node.kind();
        let caller_name = current_function
            .clone()
            .unwrap_or_else(|| "<global>".to_string());

        match lang {
            Lang::Rust => {
                if kind == "call_expression" {
                    let func = node.child_by_field_name("function")?;
                    let callee_name = func.utf8_text(source.as_bytes()).ok()?.to_string();
                    let call_type = if callee_name.contains("::") {
                        CallType::Static
                    } else {
                        CallType::Direct
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
                if kind == "method_call_expression" {
                    let method = node.child_by_field_name("name")?;
                    let callee_name = method.utf8_text(source.as_bytes()).ok()?.to_string();
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type: CallType::Method,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            Lang::Python => {
                if kind == "call" {
                    let func = node.child_by_field_name("function")?;
                    let callee_name = func.utf8_text(source.as_bytes()).ok()?.to_string();
                    let call_type = if callee_name.contains(".") {
                        CallType::Method
                    } else {
                        CallType::Direct
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            Lang::JavaScript | Lang::TypeScript => {
                if kind == "call_expression" {
                    let func = node.child_by_field_name("function")?;
                    let callee_name = func.utf8_text(source.as_bytes()).ok()?.to_string();
                    let call_type = if callee_name.starts_with("new ") {
                        CallType::Constructor
                    } else if callee_name.contains(".") {
                        CallType::Method
                    } else {
                        CallType::Direct
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
                if kind == "new_expression" {
                    let constructor = node.child_by_field_name("constructor")?;
                    let callee_name = constructor.utf8_text(source.as_bytes()).ok()?.to_string();
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type: CallType::Constructor,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            Lang::Go => {
                if kind == "call_expression" {
                    let func = node.child_by_field_name("function")?;
                    let callee_name = func.utf8_text(source.as_bytes()).ok()?.to_string();
                    let call_type = if callee_name.contains(".") {
                        CallType::Method
                    } else {
                        CallType::Direct
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            Lang::C => {
                // C: call_expression with function field
                // Examples: foo(), ptr->method(), (*fn_ptr)(args)
                if kind == "call_expression" {
                    let func = node.child_by_field_name("function")?;
                    let callee_name = func.utf8_text(source.as_bytes()).ok()?.to_string();
                    // Detect pointer-to-member calls: ptr->method
                    let call_type = if callee_name.contains("->") {
                        CallType::Method
                    } else {
                        CallType::Direct
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            Lang::Cpp => {
                // C++: call_expression, new_expression
                // Examples: foo(), obj.method(), obj->method(), new ClassName()
                if kind == "call_expression" {
                    let func = node.child_by_field_name("function")?;
                    let callee_name = func.utf8_text(source.as_bytes()).ok()?.to_string();
                    // Detect method calls: obj.method, obj->method, ns::func
                    let call_type = if callee_name.contains("->") || callee_name.contains(".") {
                        CallType::Method
                    } else if callee_name.contains("::") {
                        CallType::Static
                    } else {
                        CallType::Direct
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
                // C++ new expression: new ClassName(args)
                if kind == "new_expression" {
                    let type_node = node.child_by_field_name("type")?;
                    let callee_name = type_node.utf8_text(source.as_bytes()).ok()?.to_string();
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type: CallType::Constructor,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            Lang::Java => {
                // Java: method_invocation, object_creation_expression
                // Examples: foo(), obj.method(), new ClassName()
                if kind == "method_invocation" {
                    let name_node = node.child_by_field_name("name")?;
                    let method_name = name_node.utf8_text(source.as_bytes()).ok()?.to_string();
                    // Check if there's an object (obj.method vs just method)
                    let call_type = if node.child_by_field_name("object").is_some() {
                        CallType::Method
                    } else {
                        CallType::Direct
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name: method_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
                // Java: new ClassName()
                if kind == "object_creation_expression" {
                    let type_node = node.child_by_field_name("type")?;
                    let callee_name = type_node.utf8_text(source.as_bytes()).ok()?.to_string();
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type: CallType::Constructor,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            // Additional languages for CodeRag feature parity (A.6)
            Lang::CSharp => {
                // C#: invocation_expression, object_creation_expression
                if kind == "invocation_expression" {
                    let func = node.child(0)?; // First child is the function/method
                    let callee_name = func.utf8_text(source.as_bytes()).ok()?.to_string();
                    let call_type = if callee_name.contains(".") {
                        CallType::Method
                    } else {
                        CallType::Direct
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
                if kind == "object_creation_expression" {
                    let type_node = node.child_by_field_name("type")?;
                    let callee_name = type_node.utf8_text(source.as_bytes()).ok()?.to_string();
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type: CallType::Constructor,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            Lang::Ruby => {
                // Ruby: call, method_call
                if kind == "call" || kind == "method_call" {
                    let method = node.child_by_field_name("method")?;
                    let callee_name = method.utf8_text(source.as_bytes()).ok()?.to_string();
                    let call_type = if node.child_by_field_name("receiver").is_some() {
                        CallType::Method
                    } else {
                        CallType::Direct
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            Lang::Php => {
                // PHP: function_call_expression, method_call_expression, scoped_call_expression
                if kind == "function_call_expression" {
                    let func = node.child_by_field_name("function")?;
                    let callee_name = func.utf8_text(source.as_bytes()).ok()?.to_string();
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type: CallType::Direct,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
                if kind == "method_call_expression" || kind == "scoped_call_expression" {
                    let name = node.child_by_field_name("name")?;
                    let callee_name = name.utf8_text(source.as_bytes()).ok()?.to_string();
                    let call_type = if kind == "scoped_call_expression" {
                        CallType::Static
                    } else {
                        CallType::Method
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
                if kind == "object_creation_expression" {
                    let class = node.child_by_field_name("class")?;
                    let callee_name = class.utf8_text(source.as_bytes()).ok()?.to_string();
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type: CallType::Constructor,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            Lang::Lua => {
                // Lua: function_call
                if kind == "function_call" {
                    let name = node.child_by_field_name("name")?;
                    let callee_name = name.utf8_text(source.as_bytes()).ok()?.to_string();
                    let call_type = if callee_name.contains(":") || callee_name.contains(".") {
                        CallType::Method
                    } else {
                        CallType::Direct
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            Lang::Zig => {
                // Zig: call_expression
                if kind == "call_expression" || kind == "call" {
                    let func = node
                        .child_by_field_name("function")
                        .or_else(|| node.child(0))?;
                    let callee_name = func.utf8_text(source.as_bytes()).ok()?.to_string();
                    let call_type = if callee_name.contains(".") {
                        CallType::Method
                    } else {
                        CallType::Direct
                    };
                    return Some(ExtractedCall {
                        caller_name,
                        callee_name,
                        call_type,
                        call_line: node.start_position().row as u32 + 1,
                        call_column: node.start_position().column as u32,
                    });
                }
            }
            // Languages without meaningful call extraction (markup/config/declarative)
            Lang::Html | Lang::Css | Lang::Xml | Lang::Dockerfile | Lang::Scheme | Lang::Sql => {}
            _ => {}
        }

        None
    }

    /// Extract symbols from JSONL conversation files (Claude Code, Codex)
    ///
    /// Supports two formats:
    /// - Claude Code: `{"type": "user|assistant", "message": {"content": ...}}`
    /// - Codex: `{"session_id": "...", "ts": ..., "text": "..."}`
    ///
    /// Filters out: tool_use, tool_result, file-history-snapshot, system
    pub fn extract_jsonl(&self, content: &str, path: &Path) -> ParseResult {
        let mut result = ParseResult {
            language: "jsonl".to_string(),
            ..Default::default()
        };

        let mut user_count = 0u32;
        let mut assistant_count = 0u32;
        let mut codex_count = 0u32;
        let mut thinking_count = 0u32;

        for (line_num, line) in content.lines().enumerate() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }

            // Try to parse as JSON
            let json: serde_json::Value = match serde_json::from_str(line) {
                Ok(v) => v,
                Err(_) => continue, // Skip invalid JSON lines
            };

            // Detect format and extract
            if let Some(symbols) = self.parse_claude_code_format(
                &json,
                line_num,
                &mut user_count,
                &mut assistant_count,
                &mut thinking_count,
                path,
            ) {
                result.symbols.extend(symbols);
            } else if let Some(symbol) =
                self.parse_codex_format(&json, line_num, &mut codex_count, path)
            {
                result.symbols.push(symbol);
            }
        }

        result
    }

    /// Extract symbols from Gemini CLI JSON conversation format.
    /// Format: single JSON with `messages` array, each having `type`, `content`, `thoughts`, `toolCalls`.
    pub fn extract_gemini_json(&self, content: &str, path: &Path) -> ParseResult {
        let mut result = ParseResult {
            language: "json".to_string(),
            ..Default::default()
        };

        let root: serde_json::Value = match serde_json::from_str(content) {
            Ok(v) => v,
            Err(_) => return result,
        };

        let messages = match root.get("messages").and_then(|m| m.as_array()) {
            Some(msgs) => msgs,
            None => return result,
        };

        let mut user_count = 0u32;
        let mut assistant_count = 0u32;
        let total_lines = content.lines().count();
        let lines_per_msg = if messages.len() > 0 {
            total_lines / messages.len()
        } else {
            1
        };

        for (idx, msg) in messages.iter().enumerate() {
            let msg_type = match msg.get("type").and_then(|t| t.as_str()) {
                Some(t) => t,
                None => continue,
            };

            let approx_line = (idx * lines_per_msg + 1) as u32;

            // Extract text content
            let text = match msg_type {
                "user" => {
                    // content: [{text: "..."}]
                    msg.get("content")
                        .and_then(|c| c.as_array())
                        .and_then(|arr| {
                            let texts: Vec<&str> = arr
                                .iter()
                                .filter_map(|item| item.get("text").and_then(|t| t.as_str()))
                                .collect();
                            if texts.is_empty() {
                                None
                            } else {
                                Some(texts.join("\n"))
                            }
                        })
                }
                "gemini" => {
                    // content: "string"
                    msg.get("content")
                        .and_then(|c| c.as_str())
                        .map(|s| s.to_string())
                }
                _ => continue,
            };

            let text = match text {
                Some(t) if !t.trim().is_empty() => t,
                _ => continue,
            };

            let (name, symbol_type) = if msg_type == "user" {
                user_count += 1;
                (format!("user_{}", user_count), SymbolType::Message)
            } else {
                assistant_count += 1;
                (
                    format!("assistant_{}", assistant_count),
                    SymbolType::Message,
                )
            };

            result.symbols.push(ExtractedSymbol {
                name: name.clone(),
                qualified_name: Some(format!("{}:{}", path.display(), name)),
                symbol_type,
                line_start: approx_line,
                line_end: approx_line,
                column_start: 0,
                column_end: text.len().min(u32::MAX as usize) as u32,
                signature: Some(text.chars().take(100).collect::<String>()),
                doc_comment: Some(text),
                visibility: None,
                language: "json".to_string(),
            });
        }

        result
    }

    /// Parse Claude Code JSONL format
    /// Format: `{"type": "user|assistant", "message": {"content": ...}}`
    fn parse_claude_code_format(
        &self,
        json: &serde_json::Value,
        line_num: usize,
        user_count: &mut u32,
        assistant_count: &mut u32,
        thinking_count: &mut u32,
        path: &Path,
    ) -> Option<Vec<ExtractedSymbol>> {
        let msg_type = json.get("type")?.as_str()?;

        // Filter out non-content types
        match msg_type {
            "tool_use" | "tool_result" | "file-history-snapshot" | "system" => return None,
            "user" | "assistant" => {}
            _ => return None,
        }

        let message = json.get("message")?;
        let content = Self::extract_message_content(message)?;

        if content.trim().is_empty() {
            return None;
        }

        let mut symbols = Vec::new();
        let line_u32 = line_num as u32 + 1;

        // Create main message symbol
        let (name, symbol_type) = if msg_type == "user" {
            *user_count += 1;
            (format!("user_{}", user_count), SymbolType::Message)
        } else {
            *assistant_count += 1;
            (
                format!("assistant_{}", assistant_count),
                SymbolType::Message,
            )
        };

        symbols.push(ExtractedSymbol {
            name: name.clone(),
            qualified_name: Some(format!("{}:{}", path.display(), name)),
            symbol_type,
            line_start: line_u32,
            line_end: line_u32,
            column_start: 0,
            column_end: content.len() as u32,
            signature: Some(content.chars().take(100).collect::<String>()),
            doc_comment: Some(content.clone()),
            visibility: None,
            language: "jsonl".to_string(),
        });

        // Extract thinking blocks from assistant messages
        if msg_type == "assistant" {
            for thinking in Self::extract_thinking_blocks(&content) {
                *thinking_count += 1;
                symbols.push(ExtractedSymbol {
                    name: format!("thinking_{}", thinking_count),
                    qualified_name: Some(format!("{}:thinking_{}", path.display(), thinking_count)),
                    symbol_type: SymbolType::ThinkingBlock,
                    line_start: line_u32,
                    line_end: line_u32,
                    column_start: 0,
                    column_end: thinking.len() as u32,
                    signature: Some(thinking.chars().take(100).collect::<String>()),
                    doc_comment: Some(thinking),
                    visibility: None,
                    language: "jsonl".to_string(),
                });
            }
        }

        Some(symbols)
    }

    /// Parse Codex JSONL format (legacy + new 2025+ format)
    /// Legacy: `{"session_id": "...", "ts": ..., "text": "..."}`
    /// New:    `{"timestamp":"...","type":"response_item","payload":{"role":"user","content":[{"type":"input_text","text":"..."}]}}`
    fn parse_codex_format(
        &self,
        json: &serde_json::Value,
        line_num: usize,
        codex_count: &mut u32,
        path: &Path,
    ) -> Option<ExtractedSymbol> {
        // Legacy format
        if let Some(_session_id) = json.get("session_id").and_then(|s| s.as_str()) {
            if let Some(text) = json.get("text").and_then(|t| t.as_str()) {
                if !text.trim().is_empty() {
                    *codex_count += 1;
                    return Some(ExtractedSymbol {
                        name: format!("codex_msg_{}", codex_count),
                        qualified_name: Some(format!(
                            "{}:codex_msg_{}",
                            path.display(),
                            codex_count
                        )),
                        symbol_type: SymbolType::Message,
                        line_start: line_num as u32 + 1,
                        line_end: line_num as u32 + 1,
                        column_start: 0,
                        column_end: text.len() as u32,
                        signature: Some(text.chars().take(100).collect::<String>()),
                        doc_comment: Some(text.to_string()),
                        visibility: None,
                        language: "jsonl".to_string(),
                    });
                }
            }
        }

        // New Codex CLI format (2025+): response_item with payload
        let msg_type = json.get("type").and_then(|t| t.as_str())?;
        if msg_type != "response_item" {
            return None;
        }

        let payload = json.get("payload")?;
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
        }

        let text = text_parts.join("\n");
        if text.trim().is_empty() {
            return None;
        }

        *codex_count += 1;
        Some(ExtractedSymbol {
            name: format!("codex_msg_{}", codex_count),
            qualified_name: Some(format!("{}:codex_msg_{}", path.display(), codex_count)),
            symbol_type: SymbolType::Message,
            line_start: line_num as u32 + 1,
            line_end: line_num as u32 + 1,
            column_start: 0,
            column_end: text.len().min(u32::MAX as usize) as u32,
            signature: Some(text.chars().take(100).collect::<String>()),
            doc_comment: Some(text),
            visibility: None,
            language: "jsonl".to_string(),
        })
    }

    /// Extract text content from Claude Code message structure
    /// Handles both string content and array of content blocks
    fn extract_message_content(message: &serde_json::Value) -> Option<String> {
        let content = message.get("content")?;

        // String content
        if let Some(s) = content.as_str() {
            return Some(s.to_string());
        }

        // Array of content blocks (Claude API format)
        if let Some(arr) = content.as_array() {
            let mut texts = Vec::new();
            for block in arr {
                // Text block
                if block.get("type").and_then(|t| t.as_str()) == Some("text") {
                    if let Some(text) = block.get("text").and_then(|t| t.as_str()) {
                        texts.push(text.to_string());
                    }
                }
            }
            if !texts.is_empty() {
                return Some(texts.join("\n"));
            }
        }

        None
    }

    /// Extract <thinking>...</thinking> blocks from content
    fn extract_thinking_blocks(content: &str) -> Vec<String> {
        let mut blocks = Vec::new();
        let mut remaining = content;

        while let Some(start) = remaining.find("<thinking>") {
            let after_tag = &remaining[start + 10..]; // len("<thinking>") = 10
            if let Some(end) = after_tag.find("</thinking>") {
                let thinking = after_tag[..end].trim().to_string();
                if !thinking.is_empty() {
                    blocks.push(thinking);
                }
                remaining = &after_tag[end + 11..]; // len("</thinking>") = 11
            } else {
                break;
            }
        }

        blocks
    }
}

impl Default for CodeParser {
    fn default() -> Self {
        Self::new().expect("Failed to create CodeParser")
    }
}

/// Extract Java visibility from AST modifiers node.
/// Java modifiers appear as child nodes of type "modifiers" containing
/// "public", "private", "protected", "static", "final", "abstract", etc.
fn extract_java_visibility(node: &tree_sitter::Node, source: &str) -> Option<String> {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "modifiers" {
            let mut mod_cursor = child.walk();
            for modifier in child.children(&mut mod_cursor) {
                let text = modifier.utf8_text(source.as_bytes()).ok()?;
                match text {
                    "public" | "private" | "protected" => return Some(text.to_string()),
                    _ => continue,
                }
            }
            // Modifiers block exists but no visibility keyword → package-private
            return Some("package_private".to_string());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_lang_detection() {
        assert_eq!(Lang::from_extension("rs"), Lang::Rust);
        assert_eq!(Lang::from_extension("py"), Lang::Python);
        assert_eq!(Lang::from_extension("ts"), Lang::TypeScript);
        assert_eq!(Lang::from_extension("unknown"), Lang::Unknown);
    }

    #[test]
    fn test_parse_rust_function() {
        let parser = CodeParser::new().unwrap();
        let source = r#"
pub fn hello_world() {
    println!("Hello, World!");
}
"#;
        let result = parser.parse_file(Path::new("test.rs"), source).unwrap();
        assert_eq!(result.symbols.len(), 1);
        assert_eq!(result.symbols[0].name, "hello_world");
        assert_eq!(result.symbols[0].symbol_type, SymbolType::Function);
    }

    #[test]
    fn test_parse_java_visibility() {
        let parser = CodeParser::new().unwrap();
        let source = r#"
public class Foo {
    public void doSomething() {}
    private void helperMethod() {}
    protected void onEvent() {}
    void packagePrivate() {}
}
"#;
        let result = parser.parse_file(Path::new("Foo.java"), source).unwrap();
        assert!(!result.symbols.is_empty(), "Should parse Java symbols");

        // Find class
        let class_sym = result.symbols.iter().find(|s| s.name == "Foo");
        assert!(class_sym.is_some(), "Should find class Foo");
        assert_eq!(class_sym.unwrap().visibility.as_deref(), Some("public"));

        // Find public method
        let public_method = result.symbols.iter().find(|s| s.name == "doSomething");
        if let Some(m) = public_method {
            assert_eq!(m.visibility.as_deref(), Some("public"));
        }

        // Find private method
        let private_method = result.symbols.iter().find(|s| s.name == "helperMethod");
        if let Some(m) = private_method {
            assert_eq!(m.visibility.as_deref(), Some("private"));
        }
    }

    #[test]
    fn test_jsonl_lang_detection() {
        assert_eq!(Lang::from_extension("jsonl"), Lang::Jsonl);
        assert_eq!(Lang::from_extension("json"), Lang::Json);
    }

    #[test]
    fn test_parse_claude_code_jsonl() {
        let parser = CodeParser::new().unwrap();
        let source = r#"{"type": "user", "message": {"content": "Hello, how are you?"}}
{"type": "assistant", "message": {"content": "I'm doing well, thank you!"}}
{"type": "tool_use", "message": {"content": "ignored"}}
{"type": "user", "message": {"content": "Can you help me?"}}"#;

        let result = parser
            .parse_file(Path::new("conversation.jsonl"), source)
            .unwrap();
        assert_eq!(result.language, "jsonl");
        assert_eq!(result.symbols.len(), 3); // 2 user + 1 assistant (tool_use filtered)

        // Check user messages
        let user_msgs: Vec<_> = result
            .symbols
            .iter()
            .filter(|s| s.name.starts_with("user_"))
            .collect();
        assert_eq!(user_msgs.len(), 2);
        assert_eq!(user_msgs[0].name, "user_1");
        assert_eq!(user_msgs[1].name, "user_2");

        // Check assistant message
        let assistant_msgs: Vec<_> = result
            .symbols
            .iter()
            .filter(|s| s.name.starts_with("assistant_"))
            .collect();
        assert_eq!(assistant_msgs.len(), 1);
        assert_eq!(assistant_msgs[0].name, "assistant_1");
    }

    #[test]
    fn test_parse_codex_jsonl() {
        let parser = CodeParser::new().unwrap();
        let source = r#"{"session_id": "abc123", "ts": 1234567890, "text": "First message"}
{"session_id": "abc123", "ts": 1234567891, "text": "Second message"}"#;

        let result = parser.parse_file(Path::new("codex.jsonl"), source).unwrap();
        assert_eq!(result.language, "jsonl");
        assert_eq!(result.symbols.len(), 2);
        assert_eq!(result.symbols[0].name, "codex_msg_1");
        assert_eq!(result.symbols[1].name, "codex_msg_2");
        assert_eq!(result.symbols[0].symbol_type, SymbolType::Message);
    }

    #[test]
    fn test_extract_thinking_blocks() {
        let content = "Some text <thinking>This is thinking</thinking> more text <thinking>Another thought</thinking> end";
        let blocks = CodeParser::extract_thinking_blocks(content);
        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[0], "This is thinking");
        assert_eq!(blocks[1], "Another thought");
    }

    #[test]
    fn test_parse_claude_with_thinking() {
        let parser = CodeParser::new().unwrap();
        let source = r#"{"type": "assistant", "message": {"content": "Let me think... <thinking>I need to analyze this</thinking> Here's my answer."}}"#;

        let result = parser.parse_file(Path::new("conv.jsonl"), source).unwrap();
        assert_eq!(result.symbols.len(), 2); // 1 message + 1 thinking block

        let thinking: Vec<_> = result
            .symbols
            .iter()
            .filter(|s| s.symbol_type == SymbolType::ThinkingBlock)
            .collect();
        assert_eq!(thinking.len(), 1);
        assert_eq!(thinking[0].name, "thinking_1");
    }

    #[test]
    fn test_parse_claude_array_content() {
        let parser = CodeParser::new().unwrap();
        // Claude API format with array of content blocks
        let source = r#"{"type": "user", "message": {"content": [{"type": "text", "text": "Hello from array"}]}}"#;

        let result = parser.parse_file(Path::new("conv.jsonl"), source).unwrap();
        assert_eq!(result.symbols.len(), 1);
        assert!(result.symbols[0]
            .doc_comment
            .as_ref()
            .unwrap()
            .contains("Hello from array"));
    }
}
