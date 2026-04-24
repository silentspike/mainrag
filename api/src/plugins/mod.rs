//! Plugin system for source discovery and file retrieval
//!
//! Supports:
//! - Git repositories (clone, pull, file listing)
//! - Web crawling (BFS with configurable depth)
//! - Filesystem sources

use async_trait::async_trait;

pub mod export;
pub mod fs;
pub mod git;
pub mod pdf;
#[allow(dead_code)]
pub mod pdf_cleanup;
#[allow(dead_code)]
pub mod pdf_smart_chunker;
#[allow(dead_code)]
pub mod pdf_types;
#[allow(dead_code)]
pub mod web;

/// Result of a sync operation
#[derive(Debug, Clone)]
pub struct SyncResult {
    pub files: Vec<RawFile>,
    pub errors: Vec<String>,
}

/// Threshold above which conversation files are streamed from disk instead of loaded into memory.
pub const LARGE_FILE_THRESHOLD: usize = 5 * 1024 * 1024; // 5 MB

/// Raw file from plugin
#[derive(Debug, Clone)]
pub struct RawFile {
    pub path: String,           // Relative path from source root
    pub content: String,        // File content (empty for large files — use source_path)
    pub size: usize,            // File size in bytes
    pub language: Option<String>, // Programming language if detected
    #[allow(dead_code)]
    pub last_modified: Option<String>, // ISO 8601 timestamp
    /// Absolute path on disk. For large files (>LARGE_FILE_THRESHOLD), content is empty
    /// and the index service must stream from this path.
    pub source_path: Option<std::path::PathBuf>,
}

/// Plugin trait for source handling
#[async_trait]
pub trait SourcePlugin: Send + Sync {
    /// Sync source and return files
    async fn sync(&self, source_path: &str) -> anyhow::Result<SyncResult>;

    /// Get source type name (e.g., "git", "web", "fs")
    #[allow(dead_code)]
    fn source_type(&self) -> &'static str;
}

/// Source type detector
pub fn detect_source_type(path: &str) -> String {
    if path.ends_with(".git") || path.contains("github.com") || path.contains("gitlab.com") || path.starts_with("git@") {
        "git".to_string()
    } else if path.starts_with("http://") || path.starts_with("https://") {
        "web".to_string()
    } else if path.ends_with(".pdf") {
        "pdf".to_string()
    } else if path.ends_with("conversations.json") || path.contains("chatgpt") || path.contains("claude-export") {
        "export".to_string()
    } else {
        "fs".to_string()
    }
}

/// Get appropriate plugin for source type
pub fn get_plugin(source_type: &str) -> Option<Box<dyn SourcePlugin>> {
    match source_type {
        "fs" => Some(Box::new(fs::FilesystemPlugin::new())),
        "git" => Some(Box::new(git::GitPlugin::new())),
        "web" => Some(Box::new(web::WebPlugin::new())),
        "pdf" => Some(Box::new(pdf::PdfPlugin::new())),
        "export" => Some(Box::new(export::ExportPlugin::new())),
        _ => None,
    }
}
