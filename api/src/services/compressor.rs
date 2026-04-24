//! Contextual Compression Service
//!
//! Reduces token usage in search results by removing:
//! - Import statements (use, import, require, include)
//! - License headers and copyright notices
//! - Excessive whitespace and blank lines
//! - Redundant comments (optional)
//!
//! This is a regex-based approach (not LLM-based) for low latency.

use once_cell::sync::Lazy;
use regex::Regex;
use std::collections::HashMap;

/// Compression rules for a specific language
#[derive(Debug, Clone)]
pub struct LanguageRules {
    /// Patterns to remove entirely
    pub remove_patterns: Vec<Regex>,
    /// Patterns to replace (pattern, replacement)
    pub replace_patterns: Vec<(Regex, String)>,
}

/// Global compression rules by language
static COMPRESSION_RULES: Lazy<HashMap<&'static str, LanguageRules>> = Lazy::new(|| {
    let mut rules = HashMap::new();

    // Rust rules
    rules.insert(
        "rust",
        LanguageRules {
            remove_patterns: vec![
                // Import statements (use ...)
                Regex::new(r"(?m)^use\s+[^;]+;\s*\n").unwrap(),
                // Extern crate
                Regex::new(r"(?m)^extern\s+crate\s+[^;]+;\s*\n").unwrap(),
                // Module declarations (mod foo;)
                Regex::new(r"(?m)^mod\s+\w+;\s*\n").unwrap(),
                // License headers (// Copyright, // SPDX, etc.)
                Regex::new(r"(?m)^//[!/]?\s*(Copyright|SPDX|License|Author).*\n").unwrap(),
                // Empty doc comments
                Regex::new(r"(?m)^\s*///\s*\n").unwrap(),
            ],
            replace_patterns: vec![
                // Multiple blank lines -> single blank line
                (Regex::new(r"\n{3,}").unwrap(), "\n\n".to_string()),
                // Trailing whitespace
                (Regex::new(r"[ \t]+$").unwrap(), "".to_string()),
            ],
        },
    );

    // Python rules
    rules.insert(
        "python",
        LanguageRules {
            remove_patterns: vec![
                // Import statements
                Regex::new(r"(?m)^import\s+[^\n]+\n").unwrap(),
                Regex::new(r"(?m)^from\s+\S+\s+import\s+[^\n]+\n").unwrap(),
                // License headers
                Regex::new(r#"(?m)^#\s*(Copyright|License|Author|SPDX).*\n"#).unwrap(),
                // Shebang
                Regex::new(r"(?m)^#!.*\n").unwrap(),
                // Encoding declaration
                Regex::new(r"(?m)^#.*coding[:=].*\n").unwrap(),
            ],
            replace_patterns: vec![
                // Multiple blank lines -> single blank line
                (Regex::new(r"\n{3,}").unwrap(), "\n\n".to_string()),
            ],
        },
    );

    // JavaScript/TypeScript rules
    rules.insert(
        "javascript",
        LanguageRules {
            remove_patterns: vec![
                // ES6 imports (matches 'module' or "module")
                Regex::new(r#"(?m)^import\s+.*from\s+["'][^"']+["'];\s*\n"#).unwrap(),
                Regex::new(r#"(?m)^import\s+["'][^"']+["'];\s*\n"#).unwrap(),
                // CommonJS require
                Regex::new(r"(?m)^(const|let|var)\s+\w+\s*=\s*require\([^)]+\);\s*\n").unwrap(),
                // License headers
                Regex::new(r"(?m)^//\s*(Copyright|License|Author|SPDX).*\n").unwrap(),
                // Block license header
                Regex::new(r"(?s)/\*\*?\s*(Copyright|License|MIT|Apache).*?\*/\s*\n?").unwrap(),
            ],
            replace_patterns: vec![
                // Multiple blank lines -> single blank line
                (Regex::new(r"\n{3,}").unwrap(), "\n\n".to_string()),
            ],
        },
    );

    // TypeScript uses same rules as JavaScript
    rules.insert("typescript", rules.get("javascript").unwrap().clone());
    rules.insert("ts", rules.get("javascript").unwrap().clone());
    rules.insert("tsx", rules.get("javascript").unwrap().clone());
    rules.insert("jsx", rules.get("javascript").unwrap().clone());

    // Go rules
    rules.insert(
        "go",
        LanguageRules {
            remove_patterns: vec![
                // Import block
                Regex::new(r"(?s)import\s*\([^)]*\)\s*\n").unwrap(),
                // Single import
                Regex::new(r#"(?m)^import\s+"[^"]+"\s*\n"#).unwrap(),
                // License headers
                Regex::new(r"(?m)^//\s*(Copyright|License|Author|SPDX).*\n").unwrap(),
            ],
            replace_patterns: vec![
                // Multiple blank lines -> single blank line
                (Regex::new(r"\n{3,}").unwrap(), "\n\n".to_string()),
            ],
        },
    );

    // Java rules
    rules.insert(
        "java",
        LanguageRules {
            remove_patterns: vec![
                // Import statements
                Regex::new(r"(?m)^import\s+[^;]+;\s*\n").unwrap(),
                // Package declaration
                Regex::new(r"(?m)^package\s+[^;]+;\s*\n").unwrap(),
                // License headers (block comment)
                Regex::new(r"(?s)/\*\*?\s*(Copyright|License|Author).*?\*/\s*\n?").unwrap(),
            ],
            replace_patterns: vec![
                // Multiple blank lines -> single blank line
                (Regex::new(r"\n{3,}").unwrap(), "\n\n".to_string()),
            ],
        },
    );

    // C/C++ rules
    rules.insert(
        "c",
        LanguageRules {
            remove_patterns: vec![
                // Include statements
                Regex::new(r#"(?m)^#include\s*[<"][^>"]+[>"]\s*\n"#).unwrap(),
                // License headers
                Regex::new(r"(?m)^//\s*(Copyright|License|Author|SPDX).*\n").unwrap(),
                Regex::new(r"(?s)/\*\*?\s*(Copyright|License|Author).*?\*/\s*\n?").unwrap(),
            ],
            replace_patterns: vec![
                // Multiple blank lines -> single blank line
                (Regex::new(r"\n{3,}").unwrap(), "\n\n".to_string()),
            ],
        },
    );
    rules.insert("cpp", rules.get("c").unwrap().clone());
    rules.insert("h", rules.get("c").unwrap().clone());
    rules.insert("hpp", rules.get("c").unwrap().clone());

    rules
});

/// Default rules for unknown languages
static DEFAULT_RULES: Lazy<LanguageRules> = Lazy::new(|| {
    LanguageRules {
        remove_patterns: vec![
            // License headers (common patterns)
            Regex::new(r"(?m)^[#/]+\s*(Copyright|License|Author|SPDX).*\n").unwrap(),
        ],
        replace_patterns: vec![
            // Multiple blank lines -> single blank line
            (Regex::new(r"\n{3,}").unwrap(), "\n\n".to_string()),
            // Trailing whitespace
            (Regex::new(r"[ \t]+\n").unwrap(), "\n".to_string()),
        ],
    }
});

/// Contextual Compressor configuration
#[derive(Debug, Clone)]
pub struct CompressorConfig {
    /// Enable compression
    pub enabled: bool,
    /// Remove import statements
    pub remove_imports: bool,
    /// Remove license headers
    pub remove_licenses: bool,
    /// Normalize whitespace
    pub normalize_whitespace: bool,
    /// Minimum content length to compress (skip small chunks)
    pub min_length: usize,
}

impl Default for CompressorConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            remove_imports: true,
            remove_licenses: true,
            normalize_whitespace: true,
            min_length: 100,
        }
    }
}

/// Contextual Compressor service
pub struct ContextualCompressor {
    config: CompressorConfig,
}

impl ContextualCompressor {
    /// Create new compressor with config
    pub fn new(config: CompressorConfig) -> Self {
        Self { config }
    }

    /// Compress content based on language
    /// Returns (compressed_content, compression_ratio)
    pub fn compress(&self, content: &str, language: Option<&str>) -> (String, f32) {
        if !self.config.enabled {
            return (content.to_string(), 1.0);
        }

        // Skip small chunks
        if content.len() < self.config.min_length {
            return (content.to_string(), 1.0);
        }

        let original_len = content.len();
        let mut result = content.to_string();

        // Get language-specific rules
        let rules = language
            .and_then(|lang| COMPRESSION_RULES.get(lang))
            .unwrap_or(&DEFAULT_RULES);

        // Apply remove patterns
        if self.config.remove_imports || self.config.remove_licenses {
            for pattern in &rules.remove_patterns {
                result = pattern.replace_all(&result, "").to_string();
            }
        }

        // Apply replace patterns
        if self.config.normalize_whitespace {
            for (pattern, replacement) in &rules.replace_patterns {
                result = pattern
                    .replace_all(&result, replacement.as_str())
                    .to_string();
            }
        }

        // Trim leading/trailing whitespace
        result = result.trim().to_string();

        // Calculate compression ratio
        let compressed_len = result.len();
        let ratio = if original_len > 0 {
            compressed_len as f32 / original_len as f32
        } else {
            1.0
        };

        (result, ratio)
    }

    /// Compress multiple search results
    /// Returns compressed results with average compression ratio
    pub fn compress_results(
        &self,
        results: Vec<crate::db::models::SearchResult>,
    ) -> (Vec<crate::db::models::SearchResult>, f32) {
        if !self.config.enabled {
            return (results, 1.0);
        }

        let mut total_ratio = 0.0;
        let count = results.len();

        let compressed: Vec<_> = results
            .into_iter()
            .map(|mut result| {
                let (compressed, ratio) =
                    self.compress(&result.content, result.language.as_deref());
                total_ratio += ratio;
                result.content = compressed;
                result
            })
            .collect();

        let avg_ratio = if count > 0 {
            total_ratio / count as f32
        } else {
            1.0
        };

        (compressed, avg_ratio)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_rust_compression() {
        let compressor = ContextualCompressor::new(CompressorConfig::default());

        let content = r#"
use std::collections::HashMap;
use tokio::sync::RwLock;
use serde::{Serialize, Deserialize};

// Copyright 2024 Example Corp

/// Main struct
pub struct Database {
    pool: Pool,
}

impl Database {
    pub fn new() -> Self {
        Self { pool: Pool::new() }
    }
}
"#;

        let (compressed, ratio) = compressor.compress(content, Some("rust"));

        // Should remove use statements and copyright
        assert!(!compressed.contains("use std::collections"));
        assert!(!compressed.contains("Copyright"));

        // Should keep the actual code
        assert!(compressed.contains("pub struct Database"));
        assert!(compressed.contains("impl Database"));

        // Ratio should be < 1.0 (content was reduced)
        assert!(ratio < 1.0, "Expected compression, got ratio: {}", ratio);
    }

    #[test]
    fn test_python_compression() {
        let compressor = ContextualCompressor::new(CompressorConfig::default());

        let content = r#"#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2024 Example Corp

import os
import sys
from typing import Optional, List

def main():
    print("Hello")

if __name__ == "__main__":
    main()
"#;

        let (compressed, ratio) = compressor.compress(content, Some("python"));

        // Should remove imports and header
        assert!(!compressed.contains("import os"));
        assert!(!compressed.contains("#!/usr/bin/env"));
        assert!(!compressed.contains("Copyright"));

        // Should keep the actual code
        assert!(compressed.contains("def main()"));
        assert!(compressed.contains(r#"print("Hello")"#));

        assert!(ratio < 1.0, "Expected compression, got ratio: {}", ratio);
    }

    #[test]
    fn test_javascript_compression() {
        let compressor = ContextualCompressor::new(CompressorConfig::default());

        let content = r#"
import React from 'react';
import { useState } from 'react';

const App = () => {
    const [count, setCount] = useState(0);
    return <div>{count}</div>;
};

export default App;
"#;

        let (compressed, ratio) = compressor.compress(content, Some("javascript"));

        // Should remove imports
        assert!(!compressed.contains("import React"));
        assert!(!compressed.contains("import { useState }"));

        // Should keep the actual code
        assert!(compressed.contains("const App"));
        assert!(compressed.contains("export default"));

        assert!(ratio < 1.0, "Expected compression, got ratio: {}", ratio);
    }

    #[test]
    fn test_go_compression() {
        // Use min_length: 0 to ensure compression is applied
        let compressor = ContextualCompressor::new(CompressorConfig {
            min_length: 0,
            ..Default::default()
        });

        let content = r#"
package main

import (
    "fmt"
    "os"
)

func main() {
    fmt.Println("Hello")
}
"#;

        let (compressed, ratio) = compressor.compress(content, Some("go"));

        // Should remove import block
        assert!(
            !compressed.contains(r#""fmt""#),
            "Expected import fmt to be removed, got: {}",
            compressed
        );
        assert!(
            !compressed.contains(r#""os""#),
            "Expected import os to be removed, got: {}",
            compressed
        );

        // Should keep the actual code
        assert!(compressed.contains("func main()"));

        assert!(ratio < 1.0, "Expected compression, got ratio: {}", ratio);
    }

    #[test]
    fn test_skip_small_content() {
        let compressor = ContextualCompressor::new(CompressorConfig {
            min_length: 100,
            ..Default::default()
        });

        let content = "fn main() {}";
        let (compressed, ratio) = compressor.compress(content, Some("rust"));

        // Small content should not be compressed
        assert_eq!(compressed, content);
        assert_eq!(ratio, 1.0);
    }

    #[test]
    fn test_whitespace_normalization() {
        // Use min_length: 0 to ensure compression is applied
        let compressor = ContextualCompressor::new(CompressorConfig {
            min_length: 0,
            ..Default::default()
        });

        let content = "fn main() {\n\n\n\n    let x = 1;\n\n\n\n    let y = 2;\n}";
        let (compressed, _) = compressor.compress(content, Some("rust"));

        // Multiple blank lines should become single blank line
        assert!(
            !compressed.contains("\n\n\n"),
            "Expected multiple newlines to be normalized, got: {:?}",
            compressed
        );
    }

    #[test]
    fn test_disabled_compression() {
        let compressor = ContextualCompressor::new(CompressorConfig {
            enabled: false,
            ..Default::default()
        });

        let content = "use std::io;\n\nfn main() {}";
        let (compressed, ratio) = compressor.compress(content, Some("rust"));

        // Should return unchanged
        assert_eq!(compressed, content);
        assert_eq!(ratio, 1.0);
    }

    #[test]
    fn test_unknown_language_uses_default_rules() {
        let compressor = ContextualCompressor::new(CompressorConfig {
            min_length: 0,
            ..Default::default()
        });

        // Unknown language should use default rules (license header removal)
        let content = "# Copyright 2024 Example Corp\n# SPDX-License-Identifier: MIT\n\ndef main():\n    pass";
        let (compressed, ratio) = compressor.compress(content, Some("unknown_lang"));

        // Default rules should remove license headers
        assert!(
            !compressed.contains("Copyright"),
            "Expected license to be removed: {}",
            compressed
        );
        assert!(compressed.contains("def main()"));
        assert!(ratio < 1.0);
    }

    #[test]
    fn test_empty_content() {
        let compressor = ContextualCompressor::new(CompressorConfig {
            min_length: 0,
            ..Default::default()
        });

        let (compressed, ratio) = compressor.compress("", Some("rust"));
        assert_eq!(compressed, "");
        // Empty content has ratio 1.0 to avoid division by zero
        assert_eq!(ratio, 1.0);
    }

    #[test]
    fn test_none_language() {
        let compressor = ContextualCompressor::new(CompressorConfig {
            min_length: 0,
            ..Default::default()
        });

        let content = "# Copyright 2024\nsome code here";
        let (compressed, _) = compressor.compress(content, None);

        // None language should use default rules
        assert!(!compressed.contains("Copyright"));
    }

    #[test]
    fn test_java_compression() {
        let compressor = ContextualCompressor::new(CompressorConfig {
            min_length: 0,
            ..Default::default()
        });

        let content = r#"
package com.example;

import java.util.List;
import java.util.Map;

public class Main {
    public static void main(String[] args) {
        System.out.println("Hello");
    }
}
"#;

        let (compressed, ratio) = compressor.compress(content, Some("java"));

        // Should remove package and imports
        assert!(
            !compressed.contains("import java.util"),
            "Expected import to be removed"
        );
        assert!(
            !compressed.contains("package com.example"),
            "Expected package to be removed"
        );

        // Should keep the actual code
        assert!(compressed.contains("public class Main"));
        assert!(ratio < 1.0);
    }

    #[test]
    fn test_c_compression() {
        let compressor = ContextualCompressor::new(CompressorConfig {
            min_length: 0,
            ..Default::default()
        });

        let content = r#"
#include <stdio.h>
#include <stdlib.h>

// Copyright 2024

int main() {
    printf("Hello\n");
    return 0;
}
"#;

        let (compressed, ratio) = compressor.compress(content, Some("c"));

        // Should remove includes and copyright
        assert!(
            !compressed.contains("#include"),
            "Expected includes to be removed"
        );
        assert!(
            !compressed.contains("Copyright"),
            "Expected copyright to be removed"
        );

        // Should keep the actual code
        assert!(compressed.contains("int main()"));
        assert!(ratio < 1.0);
    }
}
