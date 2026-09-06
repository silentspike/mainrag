//! Independent post-chunking budget for non-conversation output.
//! This limits downstream writes, not the memory required to generate chunks.

use crate::services::chunker::{Chunk, ChunkType};

const DEFAULT_NON_CONVERSATION_LIMIT: usize = 500;

fn non_conversation_limit(value: Option<&str>) -> usize {
    value
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|limit| *limit > 0)
        .unwrap_or(DEFAULT_NON_CONVERSATION_LIMIT)
}

#[derive(Debug, PartialEq, Eq)]
struct Outcome {
    class: &'static str,
    generated: usize,
    retained: usize,
    limit: usize,
}

fn enforce(chunks: &mut Vec<Chunk>, global: usize, non_conversation: usize) -> Outcome {
    // Classify the complete list, before truncation. Mixed/unknown output does
    // not gain a higher budget just because a conversation chunk comes first.
    let conversation = !chunks.is_empty()
        && chunks
            .iter()
            .all(|chunk| chunk.chunk_type == ChunkType::Conversation);
    let limit = if conversation {
        global
    } else {
        global.min(non_conversation)
    };
    let generated = chunks.len();
    chunks.truncate(limit);
    Outcome {
        class: if conversation {
            "conversation"
        } else {
            "other"
        },
        generated,
        retained: chunks.len(),
        limit,
    }
}

pub(super) fn apply(chunks: &mut Vec<Chunk>, global: usize) {
    let setting = std::env::var("MAX_NON_CONVERSATION_CHUNKS_PER_FILE").ok();
    let outcome = enforce(chunks, global, non_conversation_limit(setting.as_deref()));
    metrics::histogram!("mainrag_file_chunks_before_limit", "class" => outcome.class)
        .record(outcome.generated as f64);
    metrics::histogram!("mainrag_file_chunks_after_limit", "class" => outcome.class)
        .record(outcome.retained as f64);
    if outcome.generated > outcome.retained {
        let discarded = outcome.generated - outcome.retained;
        // Preserve the original unlabeled counter for existing consumers.
        metrics::counter!("mainrag_file_chunks_truncated").increment(1);
        metrics::counter!("mainrag_file_chunks_discarded", "class" => outcome.class)
            .increment(discarded as u64);
        tracing::warn!(
            class = outcome.class,
            generated = outcome.generated,
            retained = outcome.retained,
            discarded,
            limit = outcome.limit,
            "Chunk budget truncated indexing output; adjust MAX_CHUNKS_PER_FILE and MAX_NON_CONVERSATION_CHUNKS_PER_FILE only after evaluating coverage and resource costs"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::chunker::{jsonl::JsonlChunker, Chunker, ChunkerConfig};
    use serde_json::json;

    fn fixture(count: usize, chunk_type: ChunkType) -> Vec<Chunk> {
        (0..count)
            .map(|i| Chunk {
                text: format!("public fixture {i} ä"),
                start_line: i + 1,
                end_line: i + 2,
                start_byte: i * 10,
                end_byte: i * 10 + 9,
                chunk_type,
                metadata: Some(json!({"fixture": i})),
                parent_idx: (i > 0).then_some(0),
                level: 2,
                context_prefix: Some(format!("context {i}")),
            })
            .collect()
    }

    fn complete(chunks: &[Chunk]) -> serde_json::Value {
        json!(chunks
            .iter()
            .map(|c| json!({
                "text": c.text, "start_line": c.start_line, "end_line": c.end_line,
                "start_byte": c.start_byte, "end_byte": c.end_byte,
                "type": c.chunk_type.to_string(), "metadata": c.metadata,
                "parent_idx": c.parent_idx, "level": c.level, "context": c.context_prefix
            }))
            .collect::<Vec<_>>())
    }

    #[test]
    fn high_global_limit_does_not_expand_non_conversation_budget() {
        for kind in [
            ChunkType::Text,
            ChunkType::Code,
            ChunkType::Config,
            ChunkType::Function,
        ] {
            let mut chunks = fixture(4065, kind);
            let expected = complete(&chunks[..500]);
            let vector = chunks.as_ptr();
            let text = chunks[0].text.as_ptr();
            let result = enforce(&mut chunks, 50_000, non_conversation_limit(None));
            assert_eq!(
                result,
                Outcome {
                    class: "other",
                    generated: 4065,
                    retained: 500,
                    limit: 500
                }
            );
            assert_eq!(complete(&chunks), expected);
            assert_eq!(chunks.as_ptr(), vector);
            assert_eq!(chunks[0].text.as_ptr(), text);
        }
        println!("issue50 public non-conversation fixture: generated=4065 retained=500 discarded=3565; global=50000 independent=500");
    }

    #[test]
    fn conversations_keep_global_budget_but_mixed_output_does_not() {
        let mut conversations = fixture(4065, ChunkType::Conversation);
        assert_eq!(enforce(&mut conversations, 50_000, 500).retained, 4065);
        // A non-conversation chunk beyond the normal budget must still count.
        conversations[4000].chunk_type = ChunkType::Text;
        assert_eq!(enforce(&mut conversations, 50_000, 500).retained, 500);
    }

    #[test]
    fn global_ceiling_and_explicit_override_are_respected() {
        for kind in [ChunkType::Text, ChunkType::Conversation] {
            let mut chunks = fixture(12, kind);
            assert_eq!(enforce(&mut chunks, 3, 5).retained, 3);
            assert_eq!(enforce(&mut chunks, 0, 5).retained, 0);
        }
        let mut chunks = fixture(12, ChunkType::Text);
        assert_eq!(
            enforce(&mut chunks, 50, non_conversation_limit(Some("7"))).retained,
            7
        );
    }

    #[test]
    fn invalid_or_zero_new_setting_uses_safe_default() {
        for setting in [
            None,
            Some(""),
            Some("0"),
            Some("-1"),
            Some("bad"),
            Some("999999999999999999999999999"),
        ] {
            assert_eq!(non_conversation_limit(setting), 500);
        }
        assert_eq!(non_conversation_limit(Some("1000")), 1000);
    }

    #[test]
    fn empty_and_at_boundary_lists_remain_unchanged() {
        for count in [0, 4, 5] {
            let mut chunks = fixture(count, ChunkType::Text);
            let expected = complete(&chunks);
            let result = enforce(&mut chunks, 10, 5);
            assert_eq!(result.generated, result.retained);
            assert_eq!(complete(&chunks), expected);
        }
    }

    #[test]
    fn actual_conversation_parser_output_receives_conversation_budget() {
        let content = (0..8).map(|i| json!({
            "type": "user", "message": {"role": "user", "content": format!("message {i} {}", "x".repeat(300))}
        }).to_string()).collect::<Vec<_>>().join("\n");
        let mut chunks = JsonlChunker::new(ChunkerConfig {
            max_tokens: Some(50),
            ..Default::default()
        })
        .chunk(&content, Some("jsonl"));
        assert!(chunks.len() > 2);
        let before = complete(&chunks);
        let result = enforce(&mut chunks, 100, 2);
        assert_eq!(result.class, "conversation");
        assert_eq!(complete(&chunks), before);
    }
}
