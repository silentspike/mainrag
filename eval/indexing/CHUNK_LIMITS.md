# Independent non-conversation chunk budget

`MAX_CHUNKS_PER_FILE` retains its existing parsing/default behavior (code default
500). `MAX_NON_CONVERSATION_CHUNKS_PER_FILE` adds a positive budget, default
500; absent, invalid, overflowing, negative, and zero values use that default.

At the two existing capped paths, a nonempty list containing only
`ChunkType::Conversation` uses the global maximum. Every other list, including
mixed output, uses the smaller of the two maxima. Classification inspects the
whole generated list before truncation and does not depend on a path suffix.
The helper truncates in place, preserving the complete ordered retained prefix.

For example, setting the global limit to 50,000 leaves normal code/text output
capped at 500 unless the independent budget is also explicitly increased.
The deterministic 4,065-chunk public fixture retains 500 and discards 3,565;
a conversation-only fixture of the same size retains all 4,065. These are policy
counts, not measured database, latency, RSS, or retrieval-quality improvements.
The default 500 is a guard, not a demonstrated optimal setting.

## Boundaries and recovery

The shared helper runs after full version comparison/probe reuse in the
non-streaming path and before batch flushing in the deferred JSON path.
Deferred JSONL has a separate uncapped batch loop and is unchanged. Raising
this budget is not a guarantee of full conversation coverage: parser behavior,
timeouts, other limits, and previously stored state remain separate concerns.

This is post-chunking truncation, so it bounds downstream writes/embedding work,
not the memory or CPU required to create the full chunk list. It is deliberately
lossy for search indexing and does not implement storage-v2 lossless retention.

Changing settings does not reprocess hash-skipped files. Existing stored files
need a content change or an explicitly scoped rebuild to apply a new budget.
Reverting this code/config does not restore omitted chunks by itself. No
automatic production reindex, cleanup, or deployment is part of issue #50.

## Telemetry and validation

- `mainrag_file_chunks_before_limit` and `mainrag_file_chunks_after_limit` are
  chunk-count histograms per helper invocation, labeled only `class=conversation`
  or `class=other`. They are not per-source totals or latency distributions.
- `mainrag_file_chunks_discarded` counts removed chunks with the same bounded
  class labels. The existing unlabeled `mainrag_file_chunks_truncated` counter
  continues to count affected invocations; the deferred JSON path now also
  reports its previously silent truncation.
- Hash/version skips and the deferred JSONL loop do not emit these new samples.
  Logs include generated, retained, discarded and effective-limit counts, not
  private file names or content.

Six policy regressions run explicitly in hosted CI with a nonzero-count check,
including actual conversation-parser output, invalid settings, mixed lists,
global-ceiling precedence, and complete retained-field/pointer equality.
Production telemetry comparison and setting optimization remain not run.
