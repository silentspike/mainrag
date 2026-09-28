//! Legacy character-based chunker (1000 chars, 100 overlap)
//! Kept for backward compatibility - prefer Token or Semantic chunking!

use super::{Chunk, ChunkType, Chunker, ChunkerConfig};

pub struct CharacterChunker {
    max_chars: usize,     // Default: 1000
    overlap_chars: usize, // Default: 100
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
        // Each character boundary records its byte offset and preceding newline
        // count once. Rebuilding/counting both prefixes per chunk was quadratic.
        let mut boundaries = Vec::new();
        let mut lines = 0;
        for (offset, character) in content.char_indices() {
            boundaries.push((offset, lines));
            lines += usize::from(character == '\n');
        }
        let char_count = boundaries.len();
        boundaries.push((content.len(), lines));

        // Empty content: return empty chunks
        if char_count == 0 {
            return chunks;
        }

        let mut start = 0;
        let mut prev_start: Option<usize> = None;

        while start < char_count {
            let end = start.saturating_add(self.max_chars).min(char_count);
            let text = content[boundaries[start].0..boundaries[end].0].to_string();
            let start_line = boundaries[start].1 + 1;
            let end_line = boundaries[end].1 + 1;

            chunks.push(Chunk {
                text,
                start_line,
                end_line,
                start_byte: boundaries[start].0,
                end_byte: boundaries[end].0,
                chunk_type: ChunkType::Text,
                metadata: None,
                parent_idx: None, // Character chunker: flat structure
                level: 2,         // Default to leaf level
                context_prefix: None,
            });

            // If we reached the end of content, stop
            if end >= char_count {
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

    #[test]
    fn character_boundaries_preserve_unicode_overlap_and_absolute_lines() {
        let content = "é\n水🙂abc\né\n水🙂abc\n";
        let chunks = CharacterChunker::new(ChunkerConfig {
            max_chars: Some(5),
            overlap_chars: Some(2),
            ..Default::default()
        })
        .chunk(content, None);
        assert!(chunks.len() > 2);
        for chunk in &chunks {
            assert_eq!(&content[chunk.start_byte..chunk.end_byte], chunk.text);
            assert_eq!(
                chunk.start_line,
                content[..chunk.start_byte].matches('\n').count() + 1
            );
            assert_eq!(
                chunk.end_line,
                content[..chunk.end_byte].matches('\n').count() + 1
            );
        }
        let characters: Vec<char> = content.chars().collect();
        for (index, chunk) in chunks.iter().enumerate() {
            let start = index * 3;
            assert_eq!(
                chunk.text,
                characters[start..(start + 5).min(characters.len())]
                    .iter()
                    .collect::<String>()
            );
        }
    }
}
