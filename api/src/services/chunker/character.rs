//! Legacy character-based chunker (1000 chars, 100 overlap)
//! Kept for backward compatibility - prefer Token or Semantic chunking!

use super::{Chunk, ChunkType, ChunkerConfig, Chunker};

pub struct CharacterChunker {
    max_chars: usize,       // Default: 1000
    overlap_chars: usize,   // Default: 100
}

impl CharacterChunker {
    pub fn new(config: ChunkerConfig) -> Self {
        Self {
            max_chars: config.max_chars.unwrap_or(1000),
            overlap_chars: config.overlap_chars.unwrap_or(100),
        }
    }
}

impl Default for CharacterChunker {
    fn default() -> Self {
        Self {
            max_chars: 1000,
            overlap_chars: 100,
        }
    }
}

impl Chunker for CharacterChunker {
    fn chunk(&self, content: &str, _language: Option<&str>) -> Vec<Chunk> {
        let mut chunks = vec![];
        let chars: Vec<char> = content.chars().collect();

        // Empty content: return empty chunks
        if chars.is_empty() {
            return chunks;
        }

        let mut start = 0;
        let mut prev_start: Option<usize> = None;

        while start < chars.len() {
            let end = (start + self.max_chars).min(chars.len());
            let text: String = chars[start..end].iter().collect();

            // Line calculation
            let start_line = content[..chars[..start].iter().collect::<String>().len()]
                .matches('\n').count() + 1;
            let end_line = content[..chars[..end].iter().collect::<String>().len()]
                .matches('\n').count() + 1;

            chunks.push(Chunk {
                text,
                start_line,
                end_line,
                start_byte: start,
                end_byte: end,
                chunk_type: ChunkType::Text,
                metadata: None,
                parent_idx: None,  // Character chunker: flat structure
                level: 2,          // Default to leaf level
                context_prefix: None,
            });

            // If we reached the end of content, stop
            if end >= chars.len() {
                break;
            }

            // Calculate next start with overlap
            let next_start = end.saturating_sub(self.overlap_chars);

            // Prevent infinite loop: if start didn't advance, force progress
            if Some(next_start) == prev_start || next_start <= start {
                // Force at least 1 character progress
                start += 1;
            } else {
                start = next_start;
            }
            prev_start = Some(start);
        }

        chunks
    }

    fn name(&self) -> &str {
        "character"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_character_chunking() {
        let chunker = CharacterChunker::new(ChunkerConfig {
            max_chars: Some(50),
            overlap_chars: Some(10),
            ..Default::default()
        });

        let content = "This is a test string with multiple words.";
        let chunks = chunker.chunk(content, None);

        assert!(!chunks.is_empty());
        assert!(chunks.iter().all(|c| c.text.len() <= 60)); // Max 50 + some tolerance
    }

    #[test]
    fn test_character_chunking_multiline() {
        let chunker = CharacterChunker::default();
        let content = "Line 1\nLine 2\nLine 3";
        let chunks = chunker.chunk(content, None);

        assert!(!chunks.is_empty());
        assert!(chunks[0].start_line >= 1);
        assert!(chunks[0].end_line >= chunks[0].start_line);
    }
}
