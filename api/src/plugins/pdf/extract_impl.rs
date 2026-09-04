//! Fallback PDF implementation using pdf-extract
//!
//! Used when feature `pdf-mupdf` is not enabled.
//! Pure Rust implementation with no native dependencies.
//!
//! Features:
//! - Basic text extraction from PDF files
//! - File size limit (50MB) to prevent DoS
//! - Graceful failure for encrypted/corrupted PDFs
//!
//! Limitations (compared to MuPDF):
//! - No heading detection (font sizes not exposed)
//! - No structured chunking
//! - No ligature normalization

use async_trait::async_trait;
use std::path::Path;
use tracing::{info, warn};

use super::super::{RawFile, SourcePlugin, SyncResult};
use super::{MAX_PDF_SIZE, MIN_TEXT_LENGTH};

pub struct PdfPlugin;

impl PdfPlugin {
    pub fn new() -> Self {
        Self
    }

    /// Clean extracted text:
    /// - Normalize whitespace
    /// - Remove excessive blank lines
    /// - Trim leading/trailing whitespace
    fn clean_text(text: &str) -> String {
        // Replace multiple spaces/tabs with single space
        let cleaned: String = text
            .lines()
            .map(|line| line.split_whitespace().collect::<Vec<_>>().join(" "))
            .collect::<Vec<_>>()
            .join("\n");

        // Collapse multiple blank lines into at most 2
        let mut result = String::with_capacity(cleaned.len());
        let mut blank_count = 0;

        for line in cleaned.lines() {
            if line.trim().is_empty() {
                blank_count += 1;
                if blank_count <= 2 {
                    result.push('\n');
                }
            } else {
                blank_count = 0;
                result.push_str(line);
                result.push('\n');
            }
        }

        result.trim().to_string()
    }
}

impl Default for PdfPlugin {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl SourcePlugin for PdfPlugin {
    async fn sync(&self, source_path: &str) -> anyhow::Result<SyncResult> {
        info!("PDF plugin syncing (pdf-extract fallback): {}", source_path);

        let path = Path::new(source_path);

        // Validate file exists
        if !path.exists() {
            anyhow::bail!("PDF file not found: {}", source_path);
        }

        // Check file extension
        let extension = path.extension().and_then(|e| e.to_str()).unwrap_or("");
        if extension.to_lowercase() != "pdf" {
            anyhow::bail!("Not a PDF file (extension: {})", extension);
        }

        // Check file size
        let metadata = std::fs::metadata(path)
            .map_err(|e| anyhow::anyhow!("Failed to read file metadata: {}", e))?;

        if metadata.len() > MAX_PDF_SIZE {
            anyhow::bail!(
                "PDF too large: {} bytes (max: {} bytes / {}MB)",
                metadata.len(),
                MAX_PDF_SIZE,
                MAX_PDF_SIZE / 1024 / 1024
            );
        }

        // Extract text in blocking thread pool (pdf-extract is synchronous)
        let path_buf = path.to_path_buf();
        let text_result = tokio::task::spawn_blocking(move || pdf_extract::extract_text(&path_buf))
            .await
            .map_err(|e| anyhow::anyhow!("PDF extraction task panicked: {}", e))?;

        let raw_text = match text_result {
            Ok(text) => text,
            Err(e) => {
                let error_str = format!("{}", e);

                // Check for common error patterns
                if error_str.contains("encrypted") || error_str.contains("password") {
                    anyhow::bail!("PDF is encrypted and cannot be extracted without password");
                }

                if error_str.contains("invalid") || error_str.contains("corrupt") {
                    anyhow::bail!("PDF appears to be corrupted: {}", e);
                }

                // Generic extraction error
                anyhow::bail!("Failed to extract text from PDF: {}", e);
            }
        };

        // Clean the extracted text
        let text = Self::clean_text(&raw_text);

        // Check if we got meaningful content
        if text.len() < MIN_TEXT_LENGTH {
            warn!(
                "PDF has very little text ({} chars). May be scanned/image-based. Path: {}",
                text.len(),
                source_path
            );

            // Return empty result with warning, not error
            // This allows the sync to succeed but logs the issue
            if text.is_empty() {
                return Ok(SyncResult {
                    files: vec![],
                    errors: vec![format!(
                        "PDF contains no extractable text (may be scanned/image-based): {}",
                        source_path
                    )],
                });
            }
        }

        // Get filename for the RawFile path
        let filename = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("document.pdf");

        // Create output path (same name but with .txt extension for clarity)
        let output_path = format!(
            "{}.txt",
            filename.trim_end_matches(".pdf").trim_end_matches(".PDF")
        );

        info!(
            "PDF plugin: extracted {} chars from {}",
            text.len(),
            source_path
        );

        Ok(SyncResult {
            files: vec![RawFile {
                path: output_path,
                size: text.len(),
                content: text,
                language: Some("text".to_string()), // Plain text, not markdown
                last_modified: None,
                source_path: None,
                source_range: None,
            }],
            errors: vec![],
        })
    }

    fn source_type(&self) -> &'static str {
        "pdf"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_clean_text() {
        let input = "  Hello   world  \n\n\n\nMultiple    spaces\n\n\n\n\nToo many blanks";
        let cleaned = PdfPlugin::clean_text(input);

        assert!(cleaned.contains("Hello world"));
        assert!(cleaned.contains("Multiple spaces"));
        // Should not have more than 2 consecutive blank lines
        assert!(!cleaned.contains("\n\n\n\n"));
    }

    #[test]
    fn test_clean_text_preserves_structure() {
        let input = "Line 1\n\nLine 2\n\nLine 3";
        let cleaned = PdfPlugin::clean_text(input);

        // Should preserve reasonable spacing
        assert!(cleaned.contains("Line 1"));
        assert!(cleaned.contains("Line 2"));
        assert!(cleaned.contains("Line 3"));
    }

    #[tokio::test]
    async fn test_pdf_plugin_nonexistent() {
        let plugin = PdfPlugin::new();
        let result = plugin.sync("/nonexistent/file.pdf").await;

        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("not found"));
    }

    #[tokio::test]
    async fn test_pdf_plugin_wrong_extension() {
        // Create a temp file with wrong extension
        let temp_dir = std::env::temp_dir();
        let temp_file = temp_dir.join("test_wrong_ext_extract.txt");
        std::fs::write(&temp_file, "test content").unwrap();

        let plugin = PdfPlugin::new();
        let result = plugin.sync(temp_file.to_str().unwrap()).await;

        // Cleanup
        let _ = std::fs::remove_file(&temp_file);

        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("Not a PDF file"));
    }

    #[test]
    fn test_source_type() {
        let plugin = PdfPlugin::new();
        assert_eq!(plugin.source_type(), "pdf");
    }

    #[test]
    fn test_default_impl() {
        let plugin = PdfPlugin;
        assert_eq!(plugin.source_type(), "pdf");
    }
}
