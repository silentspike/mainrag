-- Sprint 8.3: Versioned Chunk-Level Incremental Indexing
-- Adds version tracking columns to chunks for skip-on-unchanged logic
-- and model/chunker-aware re-embedding on config changes.

-- Content hash for individual chunk content (SHA256, stored as hex for readability)
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS chunk_content_hash VARCHAR(64);

-- Chunker version string (e.g., "semantic-v1")
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS chunker_version VARCHAR(32);

-- Embedding model identifier (e.g., "BAAI/bge-base-en-v1.5")
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_model_id VARCHAR(128);

-- Tokenizer version string (e.g., "tiktoken-cl100k")
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tokenizer_version VARCHAR(32);

-- Index for efficient lookups during incremental indexing
CREATE INDEX IF NOT EXISTS idx_chunks_content_hash ON chunks (chunk_content_hash) WHERE chunk_content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_chunks_versions ON chunks (chunker_version, embedding_model_id) WHERE chunker_version IS NOT NULL;
