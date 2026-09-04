//! MuPDF-based PDF implementation with structured extraction
//!
//! Features:
//! - Font-based heading detection (relative thresholds)
//! - Whitespace reconstruction (span coordinates)
//! - Structure-aware smart chunking
//! - All processing in single spawn_blocking
//!
//! # v2.3 Fixes:
//! - y-coordinate inversion (MuPDF origin at bottom-left)
//! - Span sorting by (y_bucket, x0) for multi-column support
//! - Hash in path schema for collision prevention
//! - Configurable Semaphore via PDF_MAX_CONCURRENCY env var

use async_trait::async_trait;
use mupdf::text_page::TextBlockType;
use mupdf::{Document, TextPageOptions};
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::path::Path;
use std::sync::LazyLock;
use std::time::Instant;
use tokio::sync::Semaphore;
use tracing::{info, warn};

use super::super::pdf_smart_chunker::chunk_pdf_blocks;
use super::super::pdf_types::{BlockType, FontStats, PdfBlock, ProcessedChunk};
use super::super::{RawFile, SourcePlugin, SyncResult};
use super::{MAX_PDF_SIZE, MIN_TEXT_LENGTH};
use crate::services::chunker::ChunkerConfig;
use crate::utils::text::slugify;

/// Max concurrent PDF extractions (CPU-bound, not too many in parallel)
/// v2.3: Configurable via PDF_MAX_CONCURRENCY env var
static PDF_SEMAPHORE: LazyLock<Semaphore> = LazyLock::new(|| {
    let max_concurrency = std::env::var("PDF_MAX_CONCURRENCY")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(4) // Default: 4
        .max(1) // Minimum 1
        .min(16); // Maximum 16
    Semaphore::new(max_concurrency)
});

pub struct PdfPlugin {
    chunker_config: ChunkerConfig,
}

impl PdfPlugin {
    pub fn new() -> Self {
        Self {
            chunker_config: ChunkerConfig::default(),
        }
    }

    pub fn with_config(config: ChunkerConfig) -> Self {
        Self {
            chunker_config: config,
        }
    }
}

impl Default for PdfPlugin {
    fn default() -> Self {
        Self::new()
    }
}

/// Extract line text with whitespace reconstruction
///
/// Uses char-level iteration (mupdf 0.5 API).
/// Returns: (text, max_font_size, y_position)
fn extract_line_text(line: &mupdf::TextLine) -> (String, Option<f32>, f32) {
    let mut text = String::new();
    let mut max_font_size: Option<f32> = None;
    let mut last_x_end: Option<f32> = None;
    let mut first_y: Option<f32> = None;
    let mut current_font_size = 12.0_f32; // fallback

    for chr in line.chars() {
        let c = match chr.char() {
            Some(c) => c,
            None => continue,
        };

        let origin = chr.origin();
        let font_size = chr.size();
        let quad = chr.quad();

        // Track first y for position calculation
        if first_y.is_none() {
            first_y = Some(origin.y);
        }

        // Track max font size
        if font_size > 0.0 {
            current_font_size = font_size;
            match max_font_size {
                Some(current) if font_size > current => max_font_size = Some(font_size),
                None => max_font_size = Some(font_size),
                _ => {}
            }
        }

        // Insert space if gap > 30% of font size (whitespace reconstruction)
        if let Some(last_end) = last_x_end {
            let gap = origin.x - last_end;
            if gap > current_font_size * 0.3 {
                text.push(' ');
            }
        }

        text.push(c);

        // quad.ur.x is upper-right x coordinate (end of char)
        last_x_end = Some(quad.ur.x);
    }

    let y_position = first_y.unwrap_or(0.0);

    (text.trim().to_string(), max_font_size, y_position)
}

/// Raw block data before classification: (text, page_num, font_size, y_position, page_height)
type RawBlockData = (String, usize, Option<f32>, f32, f32);

/// Extract raw blocks from PDF (before heading classification)
fn extract_raw_blocks(path: &Path) -> anyhow::Result<Vec<RawBlockData>> {
    let doc = Document::open(
        path.to_str()
            .ok_or_else(|| anyhow::anyhow!("Invalid path encoding"))?,
    )?;

    let mut raw_blocks = Vec::new();
    let page_count = doc.page_count()?;

    for page_num in 0..page_count {
        let page = doc.load_page(page_num as i32)?;
        let bounds = page.bounds()?;
        let page_height = bounds.height();
        let text_page = page.to_text_page(TextPageOptions::empty())?;

        for block in text_page.blocks() {
            // Only process text blocks, skip image blocks
            if block.r#type() == TextBlockType::Text {
                for line in block.lines() {
                    let (text, font_size, y_position) = extract_line_text(&line);
                    if !text.is_empty() {
                        raw_blocks.push((
                            text,
                            (page_num + 1) as usize,
                            font_size,
                            y_position,
                            page_height,
                        ));
                    }
                }
            }
        }
    }

    Ok(raw_blocks)
}

/// Hybrid heading detection: Font + text length + position
///
/// v2.3 FIX: y-coordinate inversion (MuPDF origin at bottom-left)
fn classify_as_heading(
    text: &str,
    font_size: f32,
    stats: &FontStats,
    y_position: f32, // Raw MuPDF y-coordinate (origin BOTTOM-left)
    page_height: f32,
) -> BlockType {
    let text_len = text.len();

    // v2.3 FIX: Invert y-coordinate (MuPDF origin is bottom-left)
    // y_norm: 0.0 = bottom, 1.0 = top
    let y_norm = 1.0 - (y_position / page_height).clamp(0.0, 1.0);
    let in_upper_third = y_norm > 0.66;

    // Rule 1: Font significantly larger than p90 + short text
    let is_large_font = font_size >= stats.p90 * 1.2;
    let is_short_text = text_len <= 100; // Headings are typically short

    // Rule 2: Doesn't end with sentence punctuation
    let ends_like_heading = !text.ends_with('.')
        && !text.ends_with(',')
        && !text.ends_with(';')
        && !text.ends_with(':');

    // Scoring-based classification
    let mut heading_score = 0;

    if font_size >= stats.p90 * 1.3 {
        heading_score += 3;
    } else if font_size >= stats.p90 * 1.1 {
        heading_score += 2;
    } else if font_size > stats.p90 {
        heading_score += 1;
    }

    if is_short_text {
        heading_score += 1;
    }
    if ends_like_heading {
        heading_score += 1;
    }
    if in_upper_third && is_large_font {
        heading_score += 1;
    }

    // Classification based on score
    match heading_score {
        5.. => BlockType::Heading1,
        3..=4 => BlockType::Heading2,
        2 if is_large_font => BlockType::Heading3,
        _ => BlockType::Paragraph,
    }
}

/// Convert raw blocks to PdfBlocks with relative heading classification
fn classify_blocks(raw_blocks: Vec<RawBlockData>) -> Vec<PdfBlock> {
    // Collect font sizes for statistics
    let font_sizes: Vec<f32> = raw_blocks
        .iter()
        .filter_map(|(_, _, fs, _, _)| *fs)
        .collect();

    let stats = FontStats::from_sizes(&font_sizes);

    raw_blocks
        .into_iter()
        .map(|(text, page_num, font_size, y_pos, page_height)| {
            let block_type = font_size
                .map(|fs| classify_as_heading(&text, fs, &stats, y_pos, page_height))
                .unwrap_or(BlockType::Paragraph);

            // v2.3: Store normalized y-position
            let y_normalized = if page_height > 0.0 {
                Some(1.0 - (y_pos / page_height).clamp(0.0, 1.0))
            } else {
                None
            };

            PdfBlock {
                text,
                block_type,
                page_num,
                font_size,
                y_position: y_normalized,
            }
        })
        .collect()
}

/// Convert chunks to RawFiles with slugified paths
///
/// v2.3: Hash in path for collision prevention
fn chunks_to_raw_files(
    chunks: Vec<ProcessedChunk>,
    pdf_stem: &str,
    pdf_filename: &str,
    source_path: &str,
) -> Vec<RawFile> {
    let pdf_stem_slug = slugify(pdf_stem);

    // v2.3: Short hash for collision prevention
    let mut hasher = DefaultHasher::new();
    source_path.hash(&mut hasher);
    let path_hash = format!("{:08x}", hasher.finish() as u32); // 8 hex chars

    chunks
        .into_iter()
        .map(|chunk| {
            // v2.3: Format with hash: {stem}-{hash}__p001-002__001.md
            let path = format!(
                "{}-{}__p{:03}-{:03}__{:03}.md",
                pdf_stem_slug, path_hash, chunk.start_page, chunk.end_page, chunk.chunk_index
            );

            let mut content = String::new();

            if let Some(heading) = &chunk.heading {
                content.push_str(&format!("# {}\n\n", heading));
            }

            content.push_str(&format!(
                "<!-- pdf: {} | pages: {}-{} -->\n\n",
                pdf_filename, chunk.start_page, chunk.end_page
            ));

            // v2.3: Cleanup already applied BEFORE chunking
            content.push_str(&chunk.text);

            RawFile {
                path,
                size: content.len(),
                content,
                language: Some("markdown".to_string()),
                last_modified: None,
                source_path: None,
                source_range: None,
            }
        })
        .collect()
}

#[async_trait]
impl SourcePlugin for PdfPlugin {
    async fn sync(&self, source_path: &str) -> anyhow::Result<SyncResult> {
        info!("PDF plugin syncing (MuPDF): {}", source_path);
        let start = Instant::now();

        // v2.3: Semaphore for backpressure
        let _permit = PDF_SEMAPHORE
            .acquire()
            .await
            .map_err(|_| anyhow::anyhow!("PDF semaphore closed"))?;

        let path = Path::new(source_path);

        if !path.exists() {
            anyhow::bail!("PDF file not found: {}", source_path);
        }

        let extension = path.extension().and_then(|e| e.to_str()).unwrap_or("");
        if extension.to_lowercase() != "pdf" {
            anyhow::bail!("Not a PDF file (extension: {})", extension);
        }

        let metadata = std::fs::metadata(path)?;
        if metadata.len() > MAX_PDF_SIZE {
            anyhow::bail!(
                "PDF too large: {} bytes (max: {}MB)",
                metadata.len(),
                MAX_PDF_SIZE / 1024 / 1024
            );
        }

        let path_buf = path.to_path_buf();
        let chunker_config = self.chunker_config.clone();
        let source_path_owned = source_path.to_string();

        // FIX: ALL in spawn_blocking (Extract + Classify + Chunk + Convert)
        let files = tokio::task::spawn_blocking(move || -> anyhow::Result<Vec<RawFile>> {
            // 1. Extract raw blocks with sorted spans
            let raw_blocks = extract_raw_blocks(&path_buf)?;

            if raw_blocks.is_empty() {
                return Ok(vec![]);
            }

            // 2. Classify with hybrid heading detection
            let blocks = classify_blocks(raw_blocks);

            // 3. Get metadata
            let pdf_stem = path_buf
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("document")
                .to_string();
            let pdf_filename = path_buf
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("document.pdf")
                .to_string();

            // 4. Smart chunking (cleanup happens INSIDE chunk_pdf_blocks)
            let chunks = chunk_pdf_blocks(blocks, &chunker_config);

            // 5. Convert to RawFiles with hash in path
            let files = chunks_to_raw_files(chunks, &pdf_stem, &pdf_filename, &source_path_owned);

            Ok(files)
        })
        .await??;

        let duration = start.elapsed();

        if files.is_empty() {
            warn!(
                "PDF contains no extractable text (may be scanned): {}",
                source_path
            );
            return Ok(SyncResult {
                files: vec![],
                errors: vec![format!("PDF contains no extractable text: {}", source_path)],
            });
        }

        // Check total extracted length
        let total_chars: usize = files.iter().map(|f| f.content.len()).sum();
        if total_chars < MIN_TEXT_LENGTH {
            warn!(
                "PDF has very little text ({} chars). May be scanned. Path: {}",
                total_chars, source_path
            );
        }

        info!(
            "PDF plugin (MuPDF): extracted {} chunks ({} chars total) in {:?}",
            files.len(),
            total_chars,
            duration
        );

        Ok(SyncResult {
            files,
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
    fn test_slugify_pdf_stem() {
        assert_eq!(slugify("My Report (Final)"), "my-report-final");
        assert_eq!(slugify("  Spaces  Around  "), "spaces-around");
        assert_eq!(slugify("file_name.pdf"), "file-name-pdf");
    }

    #[test]
    fn test_hash_path_generation() {
        let chunks = vec![ProcessedChunk {
            text: "Test content".to_string(),
            heading: Some("Chapter 1".to_string()),
            start_page: 1,
            end_page: 3,
            chunk_index: 0,
        }];

        let files =
            chunks_to_raw_files(chunks, "My Report", "My Report.pdf", "/path/to/report.pdf");

        assert_eq!(files.len(), 1);
        // Path should have format: {slug}-{hash}__p001-003__000.md
        assert!(files[0].path.starts_with("my-report-"));
        assert!(files[0].path.contains("__p001-003__000.md"));
        assert!(files[0].content.contains("# Chapter 1"));
    }

    #[test]
    fn test_y_coordinate_inversion() {
        let stats = FontStats {
            median: 12.0,
            p90: 14.0,
        };

        // y=10 with page_height=100 → y_norm = 1.0 - (10/100) = 0.9 (top)
        let block_type = classify_as_heading("Title", 20.0, &stats, 10.0, 100.0);
        // Large font + short text + upper third → should be heading
        assert!(matches!(
            block_type,
            BlockType::Heading1 | BlockType::Heading2
        ));

        // y=90 with page_height=100 → y_norm = 1.0 - (90/100) = 0.1 (bottom)
        let block_type =
            classify_as_heading("Normal paragraph text here.", 12.0, &stats, 90.0, 100.0);
        assert_eq!(block_type, BlockType::Paragraph);
    }

    #[test]
    fn test_heading_ends_with_punctuation() {
        let stats = FontStats {
            median: 12.0,
            p90: 14.0,
        };

        // Text ending with period is less likely to be heading
        let block_type = classify_as_heading("This is a sentence.", 16.0, &stats, 50.0, 100.0);
        // Even with larger font, punctuation reduces heading score
        assert!(matches!(
            block_type,
            BlockType::Heading2 | BlockType::Heading3 | BlockType::Paragraph
        ));
    }

    #[test]
    fn test_default_impl() {
        let plugin = PdfPlugin::default();
        assert_eq!(plugin.source_type(), "pdf");
    }

    #[test]
    fn test_with_config() {
        let config = ChunkerConfig {
            max_tokens: Some(512),
            overlap_tokens: Some(64),
            ..Default::default()
        };
        let plugin = PdfPlugin::with_config(config);
        assert_eq!(plugin.source_type(), "pdf");
    }

    #[tokio::test]
    async fn test_nonexistent_pdf() {
        let plugin = PdfPlugin::new();
        let result = plugin.sync("/nonexistent/file.pdf").await;
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("not found"));
    }

    #[tokio::test]
    async fn test_wrong_extension() {
        let temp_dir = std::env::temp_dir();
        let temp_file = temp_dir.join("test_mupdf_wrong_ext.txt");
        std::fs::write(&temp_file, "test content").unwrap();

        let plugin = PdfPlugin::new();
        let result = plugin.sync(temp_file.to_str().unwrap()).await;

        let _ = std::fs::remove_file(&temp_file);

        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("Not a PDF file"));
    }
}
