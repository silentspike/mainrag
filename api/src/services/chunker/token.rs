//! Token-based chunker with dual tokenizer support
//!
//! Supports two tokenizer backends controlled by `TOKENIZER_VERSION` env var:
//! - `hf_bge_wordpiece` (default): HuggingFace BERT WordPiece aligned with BGE-base-en-v1.5
//! - `tiktoken_cl100k`: GPT-3.5 BPE tokenization (legacy, causes token count mismatch with BGE embeddings)
//!
//! The tokenizer is loaded once as a global singleton to avoid repeated memory allocation.
//!
//! # Byte Offset Accuracy
//!
//! **NOTE:** Byte offsets (`start_byte`, `end_byte`) are **APPROXIMATE** (best-effort).
//!
//! Reason: Token decode does not always produce bytes identical to the original input
//! due to BPE/WordPiece token merging and whitespace normalization.
//!
//! For **exact** byte positions, use [`SemanticChunker`] which uses tree-sitter
//! and provides precise AST-based byte offsets.

use super::{Chunk, ChunkType, Chunker, ChunkerConfig};
use std::sync::LazyLock;

/// Dual tokenizer backend — selected once at startup via TOKENIZER_VERSION env var.
#[allow(clippy::large_enum_variant)]
pub enum TokenizerBackend {
    Tiktoken(tiktoken_rs::CoreBPE),
    HuggingFace(tokenizers::Tokenizer),
}

// Make TokenizerBackend Send+Sync safe (tokenizers::Tokenizer is Send+Sync)
unsafe impl Send for TokenizerBackend {}
unsafe impl Sync for TokenizerBackend {}

/// Known SHA256 hash of the shipped BGE tokenizer.json (BAAI/bge-base-en-v1.5).
/// Override with TOKENIZER_ASSET_SHA256 env var if you use a different model version.
const BGE_TOKENIZER_SHA256: &str =
    "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66";

/// Global singleton tokenizer — loaded once, reused everywhere.
/// Backend selected by `TOKENIZER_VERSION` env var (default: hf_bge_wordpiece).
pub static TOKENIZER: LazyLock<TokenizerBackend> = LazyLock::new(|| {
    let version =
        std::env::var("TOKENIZER_VERSION").unwrap_or_else(|_| "hf_bge_wordpiece".to_string());

    match version.as_str() {
        "hf_bge_wordpiece" => {
            let start = std::time::Instant::now();

            // Local asset path — deterministic, no network at startup
            let asset_path = std::env::var("TOKENIZER_ASSET_PATH")
                .unwrap_or_else(|_| "/data/models/bge-base-en-v1.5/tokenizer.json".to_string());

            // SHA256 integrity check — uses embedded hash as default, env var for override
            let expected_hash = std::env::var("TOKENIZER_ASSET_SHA256")
                .unwrap_or_else(|_| BGE_TOKENIZER_SHA256.to_string());

            let file_bytes = std::fs::read(&asset_path).unwrap_or_else(|e| panic!(
                "Failed to read tokenizer asset {}: {}. \
                 Download with: huggingface-cli download BAAI/bge-base-en-v1.5 tokenizer.json --local-dir {}",
                asset_path, e, std::path::Path::new(&asset_path).parent().unwrap().display()
            ));

            let actual_hash = {
                use sha2::Digest;
                format!("{:x}", sha2::Sha256::digest(&file_bytes))
            };
            if actual_hash != expected_hash {
                panic!(
                    "Tokenizer asset hash mismatch!\n  Expected: {}\n  Actual:   {}\n  \
                     File may be corrupted or tampered with. Re-download and update TOKENIZER_ASSET_SHA256.",
                    expected_hash, actual_hash
                );
            }

            let tok = tokenizers::Tokenizer::from_bytes(&file_bytes).unwrap_or_else(|e| {
                panic!("Failed to parse BGE tokenizer from {}: {}", asset_path, e)
            });

            tracing::info!(
                "HF BGE WordPiece tokenizer loaded from {} (SHA256 verified) in {:?}",
                asset_path,
                start.elapsed()
            );
            TokenizerBackend::HuggingFace(tok)
        }
        "hf_gte_modernbert" => {
            let start = std::time::Instant::now();

            let asset_path = std::env::var("TOKENIZER_ASSET_PATH")
                .unwrap_or_else(|_| "/data/models/gte-modernbert-base/tokenizer.json".to_string());

            let expected_hash = std::env::var("TOKENIZER_ASSET_SHA256").unwrap_or_else(|_| {
                "6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30".to_string()
            });

            let file_bytes = std::fs::read(&asset_path).unwrap_or_else(|e| panic!(
                "Failed to read GTE tokenizer asset {}: {}. \
                 Download with: huggingface-cli download Alibaba-NLP/gte-modernbert-base tokenizer.json --local-dir {}",
                asset_path, e, std::path::Path::new(&asset_path).parent().unwrap().display()
            ));

            let actual_hash = {
                use sha2::Digest;
                format!("{:x}", sha2::Sha256::digest(&file_bytes))
            };
            if actual_hash != expected_hash {
                panic!(
                    "GTE tokenizer asset hash mismatch!\n  Expected: {}\n  Actual:   {}\n  \
                     Re-download and update TOKENIZER_ASSET_SHA256.",
                    expected_hash, actual_hash
                );
            }

            let tok = tokenizers::Tokenizer::from_bytes(&file_bytes).unwrap_or_else(|e| {
                panic!("Failed to parse GTE tokenizer from {}: {}", asset_path, e)
            });

            tracing::info!(
                "HF GTE-ModernBERT BPE tokenizer loaded from {} (SHA256 verified) in {:?}",
                asset_path,
                start.elapsed()
            );
            TokenizerBackend::HuggingFace(tok)
        }
        _ => {
            tracing::warn!(
                "Loading tiktoken cl100k tokenizer (LEGACY — mismatches embedding model)"
            );
            let start = std::time::Instant::now();
            let tok = tiktoken_rs::get_bpe_from_model("gpt-3.5-turbo")
                .expect("Failed to load tiktoken tokenizer");
            tracing::info!("Tiktoken tokenizer loaded in {:?}", start.elapsed());
            TokenizerBackend::Tiktoken(tok)
        }
    }
});

/// Count tokens in text using the active tokenizer backend.
/// This is the primary API — all callers should use this instead of accessing TOKENIZER directly.
pub fn count_tokens(text: &str) -> usize {
    match &*TOKENIZER {
        TokenizerBackend::Tiktoken(t) => t.encode_with_special_tokens(text).len(),
        TokenizerBackend::HuggingFace(t) => t
            .encode(text, false)
            .map(|enc| enc.get_ids().len())
            .unwrap_or(0),
    }
}

/// Encode result with token IDs and original byte offsets.
struct EncodeResult {
    ids: Vec<u32>,
    /// (start_byte, end_byte) per token — from original text, NOT from decode roundtrip.
    /// For tiktoken: approximated via cumulative decode lengths (legacy, lossy).
    /// For HuggingFace: exact offsets from tokenizer Encoding.
    offsets: Vec<(usize, usize)>,
}

/// Encode text and return both token IDs and original byte offsets.
/// Wave 3 Fix: Use original text slices instead of lossy decode roundtrip.
fn encode_with_offsets(text: &str) -> EncodeResult {
    match &*TOKENIZER {
        TokenizerBackend::Tiktoken(t) => {
            let ids = t.encode_with_special_tokens(text);
            // Tiktoken: no offset API, approximate via cumulative decode (legacy behavior)
            let mut offsets = Vec::with_capacity(ids.len());
            let mut cursor = 0usize;
            for &id in &ids {
                let decoded = t.decode(vec![id]).unwrap_or_default();
                let len = decoded.len();
                offsets.push((cursor, cursor + len));
                cursor += len;
            }
            EncodeResult { ids, offsets }
        }
        TokenizerBackend::HuggingFace(t) => match t.encode(text, false) {
            Ok(enc) => {
                let ids = enc.get_ids().to_vec();
                let offsets: Vec<(usize, usize)> = enc.get_offsets().to_vec();
                EncodeResult { ids, offsets }
            }
            Err(_) => EncodeResult {
                ids: vec![],
                offsets: vec![],
            },
        },
    }
}

/// Decode token IDs back to text (legacy, kept for reference).
#[allow(dead_code)]
fn decode(tokens: &[u32]) -> String {
    match &*TOKENIZER {
        TokenizerBackend::Tiktoken(t) => {
            // tiktoken-rs 0.6: Rank = u32
            t.decode(tokens.to_vec()).unwrap_or_default()
        }
        TokenizerBackend::HuggingFace(t) => t.decode(tokens, true).unwrap_or_default(),
    }
}

/// Decode a single token to approximate its byte length (legacy, kept for reference).
#[allow(dead_code)]
fn decode_single(token: u32) -> String {
    match &*TOKENIZER {
        TokenizerBackend::Tiktoken(t) => {
            // tiktoken-rs 0.6: Rank = u32
            t.decode(vec![token]).unwrap_or_default()
        }
        TokenizerBackend::HuggingFace(t) => t.decode(&[token], false).unwrap_or_default(),
    }
}

/// Return active tokenizer backend name (for logging/metrics).
#[allow(dead_code)]
pub fn tokenizer_name() -> &'static str {
    match &*TOKENIZER {
        TokenizerBackend::Tiktoken(_) => "tiktoken_cl100k",
        TokenizerBackend::HuggingFace(_) => "hf_bge_wordpiece",
    }
}

pub struct TokenChunker {
    max_tokens: usize,     // Default: 256
    overlap_tokens: usize, // Default: 32
}

impl TokenChunker {
    pub fn new(config: ChunkerConfig) -> Self {
        // Force tokenizer initialization on first use (lazy)
        let _ = &*TOKENIZER;

        Self {
            max_tokens: config.max_tokens.unwrap_or(256),
            overlap_tokens: config.overlap_tokens.unwrap_or(32),
        }
    }

    /// Find start/end lines for a chunk in the original content (legacy, kept for reference).
    #[allow(dead_code)]
    fn find_line_range(&self, content: &str, chunk_text: &str) -> (usize, usize) {
        let start_byte = content.find(chunk_text).unwrap_or(0).min(content.len());
        let end_byte = (start_byte + chunk_text.len()).min(content.len());

        // Safety: clamp to valid UTF-8 char boundaries to prevent panics.
        // Walk backwards from the byte offset until we hit a valid boundary.
        let safe_start = {
            let mut b = start_byte;
            while b > 0 && !content.is_char_boundary(b) {
                b -= 1;
            }
            b
        };
        let safe_end = {
            let mut b = end_byte;
            while b > 0 && !content.is_char_boundary(b) {
                b -= 1;
            }
            b
        };

        let start_line = content[..safe_start].matches('\n').count() + 1;
        let end_line = content[..safe_end].matches('\n').count() + 1;

        (start_line, end_line)
    }
}

impl Default for TokenChunker {
    fn default() -> Self {
        Self::new(ChunkerConfig::default())
    }
}

impl Chunker for TokenChunker {
    fn chunk(&self, content: &str, _language: Option<&str>) -> Vec<Chunk> {
        let enc = encode_with_offsets(content);
        if enc.ids.is_empty() {
            return vec![];
        }

        let mut chunks = vec![];
        let mut token_start = 0;

        while token_start < enc.ids.len() {
            let token_end = (token_start + self.max_tokens).min(enc.ids.len());

            // Wave 3 Fix: Use original text byte offsets instead of lossy decode roundtrip.
            // This preserves identifiers like foo_bar exactly (no space insertion).
            let start_byte = enc.offsets[token_start].0;
            let end_byte = if token_end < enc.offsets.len() {
                enc.offsets[token_end - 1].1
            } else {
                enc.offsets.last().map(|o| o.1).unwrap_or(content.len())
            };

            // Clamp to content length and valid UTF-8 boundaries
            let start_byte = start_byte.min(content.len());
            let end_byte = end_byte.min(content.len());
            let safe_start = {
                let mut b = start_byte;
                while b > 0 && !content.is_char_boundary(b) {
                    b -= 1;
                }
                b
            };
            let safe_end = {
                let mut b = end_byte;
                while b < content.len() && !content.is_char_boundary(b) {
                    b += 1;
                }
                b.min(content.len())
            };

            let text = content[safe_start..safe_end].to_string();

            if !text.is_empty() {
                let start_line = content[..safe_start].matches('\n').count() + 1;
                let end_line = content[..safe_end].matches('\n').count() + 1;

                chunks.push(Chunk {
                    text,
                    start_line,
                    end_line,
                    start_byte: safe_start,
                    end_byte: safe_end,
                    chunk_type: ChunkType::Code,
                    metadata: None,
                    parent_idx: None,
                    level: 2,
                    context_prefix: None,
                });
            }

            if token_end >= enc.ids.len() {
                break;
            }

            token_start = token_end.saturating_sub(self.overlap_tokens);
        }

        chunks
    }

    fn name(&self) -> &str {
        "token"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_token_chunking() {
        let chunker = TokenChunker::new(ChunkerConfig {
            max_tokens: Some(50),
            overlap_tokens: Some(5),
            ..Default::default()
        });

        let content = "fn main() { println!(\"Hello world!\"); }";
        let chunks = chunker.chunk(content, Some("rust"));

        assert!(!chunks.is_empty());
        assert!(chunks.iter().all(|c| !c.text.is_empty()));
    }

    #[test]
    fn test_token_chunking_overlap() {
        let chunker = TokenChunker::default();
        let content = "function test() {\n  return 42;\n}";
        let chunks = chunker.chunk(content, Some("javascript"));

        assert!(!chunks.is_empty());
        assert!(chunks.iter().all(|c| c.start_line <= c.end_line));
    }

    #[test]
    fn test_byte_offsets_basic() {
        let chunker = TokenChunker::default();
        let content = "First line.\nSecond line.\nThird line.";
        let chunks = chunker.chunk(content, None);

        assert!(!chunks.is_empty());
        assert_eq!(chunks[0].start_byte, 0);
        assert!(chunks[0].end_byte > 0);
    }

    #[test]
    fn test_byte_offsets_monotonic() {
        let chunker = TokenChunker::new(ChunkerConfig {
            max_tokens: Some(10),
            overlap_tokens: Some(2),
            ..Default::default()
        });
        let content = "foo bar foo bar foo bar foo bar foo bar foo bar foo bar";
        let chunks = chunker.chunk(content, None);

        for i in 1..chunks.len() {
            assert!(
                chunks[i].start_byte > chunks[i - 1].start_byte,
                "Chunk {} start_byte ({}) should be > chunk {} start_byte ({})",
                i,
                chunks[i].start_byte,
                i - 1,
                chunks[i - 1].start_byte
            );
        }
    }

    #[test]
    fn test_empty_content() {
        let chunker = TokenChunker::default();
        let chunks = chunker.chunk("", None);
        assert!(chunks.is_empty());
    }

    #[test]
    fn test_count_tokens_nonzero() {
        let n = count_tokens("fn main() { println!(\"hello\"); }");
        assert!(n > 0, "Token count should be > 0, got {}", n);
    }

    #[test]
    fn test_tokenizer_name_valid() {
        let name = tokenizer_name();
        assert!(
            name == "tiktoken_cl100k" || name == "hf_bge_wordpiece",
            "Unexpected tokenizer name: {}",
            name
        );
    }

    #[test]
    fn test_underscored_identifiers_preserved() {
        // Wave 3: Verify that identifiers with underscores are preserved exactly
        // (the old lossy decode turned foo_bar into "foo _ bar")
        let chunker = TokenChunker::new(ChunkerConfig {
            max_tokens: Some(256),
            overlap_tokens: Some(0),
            ..Default::default()
        });

        let content =
            "const RRF_K: f32 = 60.0;\nconst OVERLAP_MULTIPLIER: f32 = 1.5;\nfn foo_bar_baz() {}";
        let chunks = chunker.chunk(content, Some("rust"));

        assert!(!chunks.is_empty(), "Should produce at least one chunk");
        let all_text: String = chunks.iter().map(|c| c.text.clone()).collect();
        assert!(
            all_text.contains("RRF_K"),
            "RRF_K should be preserved exactly, got: {}",
            &all_text[..all_text.len().min(200)]
        );
        assert!(
            all_text.contains("OVERLAP_MULTIPLIER"),
            "OVERLAP_MULTIPLIER should be preserved exactly"
        );
        assert!(
            all_text.contains("foo_bar_baz"),
            "foo_bar_baz should be preserved exactly"
        );
    }
}
