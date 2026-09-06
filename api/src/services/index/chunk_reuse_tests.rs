use super::*;
use crate::services::chunker::{semantic::SemanticChunker, ChunkType, ChunkerConfig};
use serde_json::{json, Value};
use std::sync::atomic::{AtomicUsize, Ordering};

struct CountingChunker {
    calls: AtomicUsize,
    empty: bool,
}

impl Chunker for CountingChunker {
    fn chunk(&self, content: &str, language: Option<&str>) -> Vec<Chunk> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if self.empty {
            return vec![];
        }
        vec![Chunk {
            text: content.to_string(),
            start_line: 2,
            end_line: 4,
            start_byte: 3,
            end_byte: content.len(),
            chunk_type: ChunkType::Function,
            metadata: Some(json!({"language": language})),
            parent_idx: Some(0),
            level: 2,
            context_prefix: Some("fixture context".to_string()),
        }]
    }

    fn name(&self) -> &str {
        "counting-fixture"
    }
}

fn complete_chunks(chunks: &[Chunk]) -> Value {
    Value::Array(
        chunks
            .iter()
            .map(|c| {
                json!({
                    "text": c.text, "start_line": c.start_line, "end_line": c.end_line,
                    "start_byte": c.start_byte, "end_byte": c.end_byte,
                    "chunk_type": c.chunk_type.to_string(), "metadata": c.metadata,
                    "parent_idx": c.parent_idx, "level": c.level, "context_prefix": c.context_prefix
                })
            })
            .collect(),
    )
}

#[test]
fn probe_is_moved_without_rechunking_or_cloning() {
    let chunker = CountingChunker {
        calls: AtomicUsize::new(0),
        empty: false,
    };
    let probe = chunker.chunk("public fixture ä🦀", Some("rust"));
    let expected = complete_chunks(&probe);
    let vector_address = probe.as_ptr();
    let text_address = probe[0].text.as_ptr();
    let chunks = chunks_for_write(Some(probe), &chunker, "must not be chunked", None);
    assert_eq!(chunker.calls.load(Ordering::SeqCst), 1);
    assert_eq!(chunks.as_ptr(), vector_address);
    assert_eq!(chunks[0].text.as_ptr(), text_address);
    assert_eq!(complete_chunks(&chunks), expected);
}

#[test]
fn empty_probe_is_a_result_not_a_cache_miss() {
    let chunker = CountingChunker {
        calls: AtomicUsize::new(0),
        empty: true,
    };
    let probe = chunker.chunk("empty-result fixture", None);
    assert!(chunks_for_write(Some(probe), &chunker, "unused", None).is_empty());
    assert_eq!(chunker.calls.load(Ordering::SeqCst), 1);
}

#[test]
fn no_probe_chunks_only_the_supplied_new_file_or_delta() {
    for (content, language) in [
        ("new file ä🦀", Some("rust")),
        ("appended delta\n", Some("json")),
        ("plain", None),
    ] {
        let chunker = CountingChunker {
            calls: AtomicUsize::new(0),
            empty: false,
        };
        let chunks = chunks_for_write(None, &chunker, content, language);
        assert_eq!(chunker.calls.load(Ordering::SeqCst), 1);
        assert_eq!(chunks[0].text, content);
        assert_eq!(chunks[0].metadata, Some(json!({"language": language})));
    }
}

fn fixtures() -> Vec<(&'static str, String, Option<&'static str>)> {
    vec![
        ("rust", (0..180).map(|n| format!("pub fn fixture_{n}(value: usize) -> usize {{ value + {n} }}\n")).collect(), Some("rust")),
        ("prose", "Public fixture with Unicode ä🦀 and repeated searchable words.\n".repeat(1200), None),
        ("conversation", (0..240).map(|n| format!("{{\"type\":\"user\",\"message\":{{\"role\":\"user\",\"content\":\"Public fixture question {n} with Unicode ä🦀\"}}}}\n{{\"type\":\"assistant\",\"message\":{{\"role\":\"assistant\",\"content\":\"Public fixture answer {n}\"}}}}\n")).collect(), Some("json")),
    ]
}

#[test]
fn real_chunker_probe_reuse_preserves_every_ordered_field() {
    let chunker = SemanticChunker::new(ChunkerConfig::default());
    for (fixture, content, language) in fixtures() {
        let probe = chunker.chunk(&content, language);
        assert!(
            !probe.is_empty(),
            "fixture {fixture} must exercise real output"
        );
        let reference = chunker.chunk(&content, language);
        let actual = chunks_for_write(Some(probe), &chunker, &content, language);
        assert_eq!(
            complete_chunks(&actual),
            complete_chunks(&reference),
            "{fixture}"
        );
    }
}

/// Opt-in chunk-preparation benchmark, not a database or end-to-end ingest test.
/// One warmup per fixture; alternate pair order across five repetitions in each
/// of three groups. JSON lines retain every observation and complete identity.
#[test]
#[ignore = "opt-in repeated chunk-preparation measurement"]
fn benchmark_chunk_probe_reuse() {
    use std::hint::black_box;
    use std::time::Instant;
    let chunker = SemanticChunker::new(ChunkerConfig::default());
    for (fixture, content, language) in fixtures() {
        let expected = complete_chunks(&chunker.chunk(&content, language));
        let expected_sha = chunk_content_sha256(&expected.to_string());
        for group in 1..=3 {
            for repetition in 1..=5 {
                for reuse in if (group + repetition) % 2 == 0 {
                    [false, true]
                } else {
                    [true, false]
                } {
                    let start = Instant::now();
                    let probe = chunker.chunk(black_box(&content), language);
                    // Both variants pay the same version-comparison content hash
                    // cost. Database row lookup is intentionally not simulated.
                    for chunk in &probe {
                        black_box(chunk_content_sha256(&chunk.text));
                    }
                    let chunks = if reuse {
                        chunks_for_write(Some(probe), &chunker, &content, language)
                    } else {
                        drop(probe);
                        chunks_for_write(None, &chunker, &content, language)
                    };
                    black_box(&chunks);
                    let elapsed_ms = start.elapsed().as_secs_f64() * 1000.0;
                    let actual = complete_chunks(&chunks);
                    assert_eq!(
                        actual, expected,
                        "{fixture} group {group} repetition {repetition}"
                    );
                    println!(
                        "CHUNK_REUSE_SAMPLE {}",
                        json!({
                            "fixture": fixture, "fixture_sha256": chunk_content_sha256(&content),
                            "group": group, "repetition": repetition,
                            "variant": if reuse { "reuse" } else { "double" },
                            "elapsed_ms": elapsed_ms, "chunker_invocations": if reuse { 1 } else { 2 },
                            "input_bytes": content.len(), "chunks": chunks.len(),
                            "result_sha256": expected_sha, "complete_identity_pass": true,
                            "scope": "synthetic_chunk_preparation_no_database"
                        })
                    );
                }
            }
        }
    }
}
