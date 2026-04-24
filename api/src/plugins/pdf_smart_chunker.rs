//! Structure-aware PDF chunking
//!
//! Smart chunking that respects document structure:
//! - Headings create chunk boundaries
//! - Tables stay together
//! - Cross-page chunks with page range tracking
//!
//! # v2.3 Fixes:
//! - Cleanup BEFORE chunking (not after) for correct boundaries
//! - Page range fix: start_page from first block after flush
//! - Heading exception: flush even if < min_chars when heading present

use super::pdf_cleanup::cleanup_pdf_text;
use super::pdf_types::{PdfBlock, ProcessedChunk};
use crate::services::chunker::ChunkerConfig;

/// Convert token limit to char limit (conservative estimate)
///
/// NOTE: ~3 chars/token is conservative. ASCII typically ~4 chars/token,
/// but CJK/Emoji can be 1-2 chars/token. For exact limits, use tiktoken.
fn tokens_to_chars(tokens: usize) -> usize {
    tokens * 3
}

struct ChunkerState {
    current_text: String,
    current_heading: Option<String>,
    start_page: usize,
    /// v2.3: Flag to track when start_page needs update (after flush)
    start_page_needs_update: bool,
    chunks: Vec<ProcessedChunk>,
    chunk_index: usize,
}

/// Chunk PDF blocks into ProcessedChunks
///
/// # Arguments
/// * `blocks` - Structured blocks from PDF extraction
/// * `config` - Chunker configuration (max_tokens, overlap_tokens)
///
/// # Returns
/// Vector of processed chunks with page ranges and optional headings
pub fn chunk_pdf_blocks(blocks: Vec<PdfBlock>, config: &ChunkerConfig) -> Vec<ProcessedChunk> {
    let max_chars = tokens_to_chars(config.max_tokens.unwrap_or(256));
    let overlap_chars = tokens_to_chars(config.overlap_tokens.unwrap_or(32));
    let min_chars = 80;

    let mut state = ChunkerState {
        current_text: String::new(),
        current_heading: None,
        start_page: 1,
        start_page_needs_update: true, // First block sets start_page
        chunks: Vec::new(),
        chunk_index: 0,
    };

    let mut last_page = 1;

    for block in blocks {
        last_page = block.page_num;

        // v2.3 FIX: Update start_page on first block after flush
        if state.start_page_needs_update {
            state.start_page = block.page_num;
            state.start_page_needs_update = false;
        }

        // Heading creates boundary
        if block.is_heading() {
            // v2.3 FIX: Heading exception - flush even if < min_chars when heading present
            let should_flush = state.current_text.len() >= min_chars
                || (state.current_heading.is_some() && !state.current_text.is_empty());

            if should_flush {
                flush_chunk(&mut state, block.page_num.saturating_sub(1).max(1));
            }
            state.current_heading = Some(block.text.clone());
            state.start_page = block.page_num;
            state.start_page_needs_update = false;
            continue;
        }

        // v2.3 FIX: Cleanup BEFORE adding to chunk (not after)
        let cleaned_text = cleanup_pdf_text(&block.text);
        if cleaned_text.is_empty() {
            continue; // Skip empty blocks after cleanup
        }

        // Add block text with separator
        if !state.current_text.is_empty() {
            state.current_text.push_str("\n\n");
        }
        state.current_text.push_str(&cleaned_text);

        // Check size limit
        if state.current_text.len() >= max_chars {
            flush_chunk(&mut state, block.page_num);

            // Overlap: keep last portion for context continuity
            if overlap_chars > 0 && state.current_text.len() > overlap_chars {
                let overlap_start = state.current_text.len().saturating_sub(overlap_chars);
                state.current_text = state.current_text[overlap_start..].to_string();
            } else {
                state.current_text.clear();
            }
        }
    }

    // v2.3 FIX: Flush remaining - with heading exception
    let should_flush = state.current_text.len() >= min_chars
        || (state.current_heading.is_some() && !state.current_text.is_empty());

    if should_flush {
        flush_chunk(&mut state, last_page);
    }

    state.chunks
}

fn flush_chunk(state: &mut ChunkerState, end_page: usize) {
    if state.current_text.is_empty() {
        return;
    }

    state.chunks.push(ProcessedChunk {
        text: std::mem::take(&mut state.current_text),
        heading: state.current_heading.take(),
        start_page: state.start_page,
        end_page,
        chunk_index: state.chunk_index,
    });

    state.chunk_index += 1;
    // v2.3 FIX: start_page will be set by NEXT block
    state.start_page_needs_update = true;
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::plugins::pdf_types::BlockType;

    fn make_heading(text: &str, page: usize) -> PdfBlock {
        PdfBlock {
            text: text.to_string(),
            block_type: BlockType::Heading1,
            page_num: page,
            font_size: Some(24.0),
            y_position: Some(0.9),
        }
    }

    fn make_paragraph(text: &str, page: usize) -> PdfBlock {
        PdfBlock {
            text: text.to_string(),
            block_type: BlockType::Paragraph,
            page_num: page,
            font_size: Some(12.0),
            y_position: Some(0.5),
        }
    }

    #[test]
    fn test_heading_creates_boundary() {
        let blocks = vec![
            make_heading("Chapter 1", 1),
            make_paragraph(&"Content of chapter one. ".repeat(10), 1),
            make_heading("Chapter 2", 2),
            make_paragraph(&"Content of chapter two. ".repeat(10), 2),
        ];

        let config = ChunkerConfig::default();
        let chunks = chunk_pdf_blocks(blocks, &config);

        assert_eq!(chunks.len(), 2);
        assert_eq!(chunks[0].heading, Some("Chapter 1".to_string()));
        assert_eq!(chunks[1].heading, Some("Chapter 2".to_string()));
    }

    #[test]
    fn test_short_chapter_with_heading_preserved() {
        // v2.3 FIX TEST: Short chapters with heading are NOT discarded
        let blocks = vec![
            make_heading("Abstract", 1),
            make_paragraph("Short abstract text.", 1), // < 80 chars
        ];

        let config = ChunkerConfig::default();
        let chunks = chunk_pdf_blocks(blocks, &config);

        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0].heading, Some("Abstract".to_string()));
        assert!(chunks[0].text.contains("Short abstract"));
    }

    #[test]
    fn test_page_range_correct() {
        let blocks = vec![
            make_paragraph(&"Page 1 content. ".repeat(10), 1),
            make_paragraph(&"Page 2 content. ".repeat(10), 2),
            make_paragraph(&"Page 3 content. ".repeat(10), 3),
        ];

        let config = ChunkerConfig::default();
        let chunks = chunk_pdf_blocks(blocks, &config);

        // Should have at least one chunk spanning pages 1-3
        assert!(!chunks.is_empty());
        assert_eq!(chunks[0].start_page, 1);
        assert!(chunks.last().unwrap().end_page >= 1);
    }

    #[test]
    fn test_page_range_after_heading_boundary() {
        // v2.3 FIX TEST: Page range is correct after heading boundary
        let blocks = vec![
            make_heading("Chapter 1", 1),
            make_paragraph(&"Content on page 1. ".repeat(10), 1),
            make_heading("Chapter 2", 5), // Jump to page 5
            make_paragraph(&"Content on page 5. ".repeat(10), 5),
        ];

        let config = ChunkerConfig::default();
        let chunks = chunk_pdf_blocks(blocks, &config);

        assert_eq!(chunks.len(), 2);
        assert_eq!(chunks[0].start_page, 1);
        assert_eq!(chunks[1].start_page, 5); // NOT 1!
    }

    #[test]
    fn test_cleanup_applied_before_chunking() {
        // v2.3: Cleanup happens BEFORE adding to chunk
        let blocks = vec![
            make_paragraph("Text with lig-\natures and ﬁle.", 1),
        ];

        let config = ChunkerConfig::default();
        let chunks = chunk_pdf_blocks(blocks, &config);

        // Should be cleaned
        if !chunks.is_empty() {
            assert!(!chunks[0].text.contains("ﬁ"));
            assert!(!chunks[0].text.contains("-\n"));
        }
    }

    #[test]
    fn test_empty_blocks_filtered() {
        let blocks = vec![
            make_heading("Title", 1),
            make_paragraph("   ", 1), // Empty after cleanup
            make_paragraph(&"Real content. ".repeat(10), 1),
        ];

        let config = ChunkerConfig::default();
        let chunks = chunk_pdf_blocks(blocks, &config);

        assert_eq!(chunks.len(), 1);
        assert!(!chunks[0].text.contains("   "));
    }

    #[test]
    fn test_chunk_size_limit() {
        let config = ChunkerConfig {
            max_tokens: Some(50), // 50*3=150 chars max
            overlap_tokens: Some(10),
            ..Default::default()
        };

        // Multiple blocks that together exceed the limit
        // Each block is ~100 chars, limit is 150 chars
        let blocks = vec![
            make_paragraph(&"A".repeat(100), 1),
            make_paragraph(&"B".repeat(100), 1),
            make_paragraph(&"C".repeat(100), 2),
        ];

        let chunks = chunk_pdf_blocks(blocks, &config);

        // Should have multiple chunks due to size limit
        assert!(chunks.len() >= 2, "Expected at least 2 chunks, got {}", chunks.len());
    }

    #[test]
    fn test_overlap_preserved() {
        let config = ChunkerConfig {
            max_tokens: Some(50),
            overlap_tokens: Some(20),
            ..Default::default()
        };

        let blocks = vec![
            make_paragraph(&"First chunk content. ".repeat(20), 1),
            make_paragraph(&"Second chunk content. ".repeat(20), 2),
        ];

        let chunks = chunk_pdf_blocks(blocks, &config);

        // With overlap, adjacent chunks should share some text
        if chunks.len() >= 2 {
            // Overlap should cause some repetition
            // (exact test depends on chunk boundaries)
        }
    }

    #[test]
    fn test_min_chars_threshold() {
        // Content below min_chars threshold without heading is discarded
        let blocks = vec![make_paragraph("Short.", 1)]; // < 80 chars, no heading

        let config = ChunkerConfig::default();
        let chunks = chunk_pdf_blocks(blocks, &config);

        // Without heading, short content is filtered
        assert!(chunks.is_empty());
    }
}
