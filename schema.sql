-- ===================================================================
-- CodeRag PostgreSQL Schema - HISTORICAL / REFERENCE ONLY
-- ===================================================================
-- WARNING: This file is OUTDATED. The actual schema is defined by
-- the migrations in /work/mainrag/migrations/ (Source of Truth).
-- To get the current schema, run:
--   pg_dump --schema-only --no-owner --no-privileges mainrag
--
-- Original Version: 1.0 (pre-migration era)
-- PostgreSQL 18.1 + pgvector 0.8.1
-- Hardware: AMD Ryzen 9 5900HS, 16GB RAM, NVMe SSD
-- Workload: 10-20 parallel coding agents
-- ===================================================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- For fuzzy text matching

-- ===================================================================
-- Sources: Registry of data sources (git, fs, web, conversation)
-- ===================================================================
CREATE TABLE IF NOT EXISTS sources (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,  -- 'git', 'fs', 'web', 'conversation'
    path TEXT NOT NULL,
    config JSONB,  -- Flexible config (JSONB for indexing)
    last_synced TIMESTAMPTZ,
    file_count INTEGER DEFAULT 0,
    total_size BIGINT DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sources_type ON sources(type);
CREATE INDEX idx_sources_last_synced ON sources(last_synced);
CREATE INDEX idx_sources_config ON sources USING GIN (config);

-- ===================================================================
-- Files: All imported files with compressed content
-- ===================================================================
CREATE TABLE IF NOT EXISTS files (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    hash BYTEA NOT NULL,  -- SHA256 hash
    content BYTEA NOT NULL,  -- zstd compressed content
    content_text TEXT,  -- Decompressed for FTS (generated/updated by trigger)
    language TEXT,
    size_original INTEGER NOT NULL,
    size_compressed INTEGER NOT NULL,
    last_modified TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    intelligence_analyzed_at TIMESTAMPTZ,
    intelligence_symbols_count INTEGER NOT NULL DEFAULT 0,
    intelligence_calls_count INTEGER NOT NULL DEFAULT 0,

    -- Full-text search vector (auto-updated by trigger)
    fts_vector TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', COALESCE(path, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(language, '')), 'B')
    ) STORED,

    UNIQUE(source_id, path)
);

CREATE INDEX idx_files_hash ON files(hash);
CREATE INDEX idx_files_source ON files(source_id);
CREATE INDEX idx_files_language ON files(language);
CREATE INDEX idx_files_modified ON files(last_modified);
CREATE INDEX idx_files_source_path ON files(source_id, path);
CREATE INDEX idx_files_intelligence_pending ON files(source_id, updated_at, id)
    WHERE intelligence_analyzed_at IS NULL;

-- GIN index for full-text search (fastupdate=off for read performance)
CREATE INDEX idx_files_fts ON files USING GIN (fts_vector) WITH (fastupdate = off);

-- ===================================================================
-- Symbols: Tree-sitter extracted symbols (functions, classes, etc.)
-- ===================================================================
CREATE TABLE IF NOT EXISTS symbols (
    id BIGSERIAL PRIMARY KEY,
    file_id BIGINT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qualified_name TEXT,           -- Full path: module::Class::method
    type TEXT NOT NULL,            -- 'function', 'class', 'struct', 'method', etc.
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    context TEXT,                  -- Signature preview (API reads this)
    signature TEXT,                -- Full function signature
    doc_comment TEXT,              -- Docstring/comment
    visibility TEXT,               -- 'pub', 'private', 'protected', etc.
    language TEXT,                 -- 'rust', 'python', 'go', etc.

    -- For fast symbol name search
    name_trigram TEXT GENERATED ALWAYS AS (lower(name)) STORED,

    -- UNIQUE for ON CONFLICT in intelligence.rs store_symbol()
    UNIQUE (file_id, name, line_start)
);

CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_type ON symbols(type);
CREATE INDEX idx_symbols_file ON symbols(file_id);
CREATE INDEX idx_symbols_file_name ON symbols(file_id, name);

-- Trigram index for fuzzy symbol search
CREATE INDEX idx_symbols_name_trgm ON symbols USING GIN (name_trigram gin_trgm_ops);

-- ===================================================================
-- Embeddings: File-level BGE vectors (768 dimensions)
-- ===================================================================
CREATE TABLE IF NOT EXISTS embeddings (
    file_id BIGINT PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    model TEXT NOT NULL,  -- e.g., 'BAAI/bge-base-en-v1.5'
    vector vector(768) NOT NULL,  -- pgvector native type (768 for BGE-base)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for fast ANN search (cosine similarity)
-- m=24: increased connections for 768-dim (better recall)
-- ef_construction=128: higher recall for larger vectors
CREATE INDEX idx_embeddings_vector ON embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 24, ef_construction = 128);

-- ===================================================================
-- Chunks: Content-aware chunks for long documents
-- ===================================================================
CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    file_id BIGINT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_type TEXT NOT NULL,  -- 'code', 'markdown', 'text', 'function', etc.
    content_hash BYTEA NOT NULL,
    content_compressed BYTEA NOT NULL,  -- zstd compressed
    content_text TEXT,  -- Decompressed for FTS
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    parent_chunk_id BIGINT REFERENCES chunks(id) ON DELETE CASCADE,
    level SMALLINT DEFAULT 0,  -- Hierarchy depth: 0=file, 1=class/section, 2=function
    metadata JSONB,  -- Flexible metadata
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Full-text search vector
    fts_vector TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', COALESCE(content_text, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(chunk_type, '')), 'B')
    ) STORED
);

CREATE INDEX idx_chunks_file ON chunks(file_id);
CREATE INDEX idx_chunks_type ON chunks(chunk_type);
CREATE INDEX idx_chunks_parent ON chunks(parent_chunk_id);
CREATE INDEX idx_chunks_level ON chunks(level);
CREATE INDEX idx_chunks_parent_level ON chunks(parent_chunk_id, level);
CREATE INDEX idx_chunks_file_type ON chunks(file_id, chunk_type);
CREATE INDEX idx_chunks_hash ON chunks(content_hash);

-- GIN index for chunk full-text search
CREATE INDEX idx_chunks_fts ON chunks USING GIN (fts_vector) WITH (fastupdate = off);

-- GIN index for metadata queries
CREATE INDEX idx_chunks_metadata ON chunks USING GIN (metadata);

-- ===================================================================
-- Chunk Embeddings: Chunk-level BGE vectors for semantic search
-- This is the PRIMARY table for RAG semantic search
-- ===================================================================
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id BIGINT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    model TEXT NOT NULL,  -- 'BAAI/bge-base-en-v1.5'
    vector vector(768) NOT NULL,  -- 768 dimensions for BGE-base
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for fast ANN search on chunks
-- This is the MAIN vector index for RAG queries
-- m=24, ef=128 optimized for 768-dim BGE embeddings
CREATE INDEX idx_chunk_embeddings_vector ON chunk_embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 24, ef_construction = 128);

-- ===================================================================
-- Call Graph: Function call relationships
-- ===================================================================
CREATE TABLE IF NOT EXISTS call_graph (
    id BIGSERIAL PRIMARY KEY,
    caller_symbol_id BIGINT NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    callee_symbol_id BIGINT REFERENCES symbols(id) ON DELETE SET NULL,
    callee_name TEXT NOT NULL,
    call_line INTEGER NOT NULL,
    call_type TEXT NOT NULL,  -- 'direct', 'method', 'constructor', etc.
    is_external BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_call_graph_caller ON call_graph(caller_symbol_id);
CREATE INDEX idx_call_graph_callee ON call_graph(callee_symbol_id);
CREATE INDEX idx_call_graph_callee_name ON call_graph(callee_name);
CREATE INDEX idx_call_graph_callee_name_trgm ON call_graph USING GIN (callee_name gin_trgm_ops);

-- ===================================================================
-- Schema Metadata: Version tracking
-- ===================================================================
CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO schema_metadata (key, value) VALUES
    ('version', '2'),
    ('created_at', NOW()::TEXT),
    ('db_type', 'postgresql'),
    ('vector_dimensions', '768'),
    ('embedding_model', 'BAAI/bge-base-en-v1.5'),
    ('hnsw_m', '24'),
    ('hnsw_ef_construction', '128')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();

-- ===================================================================
-- Helper Functions
-- ===================================================================

-- Function to search chunks by semantic similarity
CREATE OR REPLACE FUNCTION search_chunks_semantic(
    query_vector vector(768),
    limit_count INTEGER DEFAULT 10,
    source_id_filter BIGINT DEFAULT NULL,
    ef_search INTEGER DEFAULT 100
)
RETURNS TABLE (
    chunk_id BIGINT,
    file_id BIGINT,
    file_path TEXT,
    source_name TEXT,
    chunk_type TEXT,
    content_text TEXT,
    start_line INTEGER,
    end_line INTEGER,
    similarity FLOAT
) AS $$
BEGIN
    -- Set ef_search for this query (higher = better recall)
    PERFORM set_config('hnsw.ef_search', ef_search::TEXT, TRUE);

    RETURN QUERY
    SELECT
        c.id AS chunk_id,
        c.file_id,
        f.path AS file_path,
        s.name AS source_name,
        c.chunk_type,
        c.content_text,
        c.start_line,
        c.end_line,
        1 - (ce.vector <=> query_vector) AS similarity
    FROM chunk_embeddings ce
    JOIN chunks c ON c.id = ce.chunk_id
    JOIN files f ON f.id = c.file_id
    JOIN sources s ON s.id = f.source_id
    WHERE (source_id_filter IS NULL OR f.source_id = source_id_filter)
    ORDER BY ce.vector <=> query_vector
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql;

-- Function to search chunks by full-text (keyword)
CREATE OR REPLACE FUNCTION search_chunks_fts(
    query_text TEXT,
    limit_count INTEGER DEFAULT 10,
    source_id_filter BIGINT DEFAULT NULL
)
RETURNS TABLE (
    chunk_id BIGINT,
    file_id BIGINT,
    file_path TEXT,
    source_name TEXT,
    chunk_type TEXT,
    content_text TEXT,
    start_line INTEGER,
    end_line INTEGER,
    rank FLOAT
) AS $$
DECLARE
    tsquery_val TSQUERY;
BEGIN
    -- Convert text to tsquery (websearch style for better UX)
    tsquery_val := websearch_to_tsquery('simple', query_text);

    RETURN QUERY
    SELECT
        c.id AS chunk_id,
        c.file_id,
        f.path AS file_path,
        s.name AS source_name,
        c.chunk_type,
        c.content_text,
        c.start_line,
        c.end_line,
        ts_rank_cd(c.fts_vector, tsquery_val) AS rank
    FROM chunks c
    JOIN files f ON f.id = c.file_id
    JOIN sources s ON s.id = f.source_id
    WHERE c.fts_vector @@ tsquery_val
      AND (source_id_filter IS NULL OR f.source_id = source_id_filter)
    ORDER BY rank DESC
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql;

-- Function for hybrid search (RRF fusion of semantic + FTS)
CREATE OR REPLACE FUNCTION search_chunks_hybrid(
    query_text TEXT,
    query_vector vector(768),
    limit_count INTEGER DEFAULT 10,
    source_id_filter BIGINT DEFAULT NULL,
    k_rrf INTEGER DEFAULT 60  -- RRF constant
)
RETURNS TABLE (
    chunk_id BIGINT,
    file_id BIGINT,
    file_path TEXT,
    source_name TEXT,
    chunk_type TEXT,
    content_text TEXT,
    start_line INTEGER,
    end_line INTEGER,
    rrf_score FLOAT,
    semantic_rank INTEGER,
    fts_rank INTEGER
) AS $$
WITH semantic_results AS (
    SELECT
        c.id AS chunk_id,
        ROW_NUMBER() OVER (ORDER BY ce.vector <=> query_vector) AS rank
    FROM chunk_embeddings ce
    JOIN chunks c ON c.id = ce.chunk_id
    JOIN files f ON f.id = c.file_id
    WHERE (source_id_filter IS NULL OR f.source_id = source_id_filter)
    ORDER BY ce.vector <=> query_vector
    LIMIT limit_count * 3  -- Get more candidates for RRF
),
fts_results AS (
    SELECT
        c.id AS chunk_id,
        ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.fts_vector, websearch_to_tsquery('simple', query_text)) DESC) AS rank
    FROM chunks c
    JOIN files f ON f.id = c.file_id
    WHERE c.fts_vector @@ websearch_to_tsquery('simple', query_text)
      AND (source_id_filter IS NULL OR f.source_id = source_id_filter)
    LIMIT limit_count * 3
),
rrf_combined AS (
    SELECT
        COALESCE(sr.chunk_id, fr.chunk_id) AS chunk_id,
        COALESCE(1.0 / (k_rrf + sr.rank), 0) + COALESCE(1.0 / (k_rrf + fr.rank), 0) AS rrf_score,
        sr.rank AS semantic_rank,
        fr.rank AS fts_rank
    FROM semantic_results sr
    FULL OUTER JOIN fts_results fr ON sr.chunk_id = fr.chunk_id
)
SELECT
    rc.chunk_id,
    c.file_id,
    f.path AS file_path,
    s.name AS source_name,
    c.chunk_type,
    c.content_text,
    c.start_line,
    c.end_line,
    rc.rrf_score,
    COALESCE(rc.semantic_rank, 0)::INTEGER AS semantic_rank,
    COALESCE(rc.fts_rank, 0)::INTEGER AS fts_rank
FROM rrf_combined rc
JOIN chunks c ON c.id = rc.chunk_id
JOIN files f ON f.id = c.file_id
JOIN sources s ON s.id = f.source_id
ORDER BY rc.rrf_score DESC
LIMIT limit_count;
$$ LANGUAGE sql;

-- ===================================================================
-- Triggers for auto-updating timestamps
-- ===================================================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_sources_updated_at
    BEFORE UPDATE ON sources
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trigger_files_updated_at
    BEFORE UPDATE ON files
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ===================================================================
-- Performance Settings Hints (apply via SET or postgresql.conf)
-- ===================================================================
COMMENT ON TABLE chunk_embeddings IS
'Primary table for RAG semantic search.
For best performance, set session-level: SET hnsw.ef_search = 100;
Higher ef_search = better recall, slightly slower queries.';

COMMENT ON INDEX idx_chunk_embeddings_vector IS
'HNSW index for fast approximate nearest neighbor search.
m=24, ef_construction=128 optimized for 768-dim BGE embeddings.
~200K vectors, estimated memory: ~400MB for index.';

-- ===================================================================
-- Optional contextual prefix on chunks
-- Stores the "context-prefix" (source name + relative path + parent
-- scope) that the indexer prepends to chunk text before embedding,
-- so the embedding encodes the document context. The asymmetry —
-- documents get the prefix, queries do not — is intentional. Toggle
-- with the EMBEDDING_WITH_CCH env var; see api/src/services/index.rs.
-- ===================================================================
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS context_prefix TEXT;

COMMENT ON COLUMN chunks.context_prefix IS
'Document-context prefix prepended to chunk text at embed time
(source name + relative path + parent scope). Indexer-side only;
query embeddings stay natural.';

-- ===================================================================
-- Entities: Named Entity Recognition results
-- Used by the ownership lookup in api/src/services/intelligence.rs.
-- ===================================================================
CREATE TABLE IF NOT EXISTS entities (
    id BIGSERIAL PRIMARY KEY,
    chunk_id BIGINT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,  -- 'function', 'class', 'person', 'concept', etc.
    normalized_name TEXT,  -- Normalized form for deduplication
    confidence FLOAT,  -- NER confidence score
    start_offset INTEGER,
    end_offset INTEGER,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_entities_chunk ON entities(chunk_id);
CREATE INDEX idx_entities_name ON entities(name);
CREATE INDEX idx_entities_type ON entities(entity_type);
CREATE INDEX idx_entities_normalized ON entities(normalized_name);
CREATE INDEX idx_entities_name_trgm ON entities USING GIN (name gin_trgm_ops);

-- ===================================================================
-- Entity Relations: directed edges over the entities table
-- Used by the bidirectional ownership query in
-- api/src/services/intelligence.rs (get_ownership).
-- ===================================================================
CREATE TABLE IF NOT EXISTS entity_relations (
    id BIGSERIAL PRIMARY KEY,
    source_entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,  -- 'calls', 'inherits', 'uses', 'defines', etc.
    confidence FLOAT,
    chunk_id BIGINT REFERENCES chunks(id) ON DELETE SET NULL,  -- Where this relation was found
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(source_entity_id, target_entity_id, relation_type)
);

CREATE INDEX idx_entity_rel_source ON entity_relations(source_entity_id);
CREATE INDEX idx_entity_rel_target ON entity_relations(target_entity_id);
CREATE INDEX idx_entity_rel_type ON entity_relations(relation_type);

COMMENT ON TABLE entity_relations IS
'Directed entity-to-entity edges (source -> target, typed).
Backs the ownership/relationship lookup in the intelligence layer.';

-- ===================================================================
-- Indexing Outbox: Transactional queue for Qdrant synchronization
-- DESIGN: Schlanke Referenz-Queue, Worker zieht vector aus chunk_embeddings
-- ===================================================================
CREATE TABLE IF NOT EXISTS indexing_outbox (
    id BIGSERIAL PRIMARY KEY,

    -- Action
    action VARCHAR(20) NOT NULL,        -- 'upsert', 'delete'

    -- References (Worker JOINs chunk_embeddings for vector)
    -- NOTE: No FK on chunk_id! FK would CASCADE delete outbox before Qdrant sync
    chunk_id BIGINT NOT NULL,
    file_id BIGINT REFERENCES files(id) ON DELETE SET NULL,
    source_id BIGINT REFERENCES sources(id) ON DELETE SET NULL,

    -- Minimal payload (just IDs, not the vector itself)
    payload JSONB NOT NULL DEFAULT '{}',

    -- Processing state
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, processing, done, failed
    processing_started_at TIMESTAMPTZ,  -- For accurate Reaper timeout detection
    processed_at TIMESTAMPTZ,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,

    -- Metadata
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for efficient polling
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON indexing_outbox(created_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_outbox_chunk ON indexing_outbox(chunk_id);
-- Index for Reaper queries (stale processing entries)
CREATE INDEX IF NOT EXISTS idx_outbox_processing_stale ON indexing_outbox(processing_started_at)
    WHERE status = 'processing';

-- Claim function for worker (SKIP LOCKED pattern)
-- Sets processing_started_at for accurate Reaper timeout detection
CREATE OR REPLACE FUNCTION claim_outbox_batch(batch_size INT DEFAULT 100)
RETURNS TABLE (
    outbox_id BIGINT,
    action VARCHAR,
    chunk_id BIGINT,
    file_id BIGINT,
    source_id BIGINT,
    payload JSONB,
    vector vector  -- No dimension - inferred from chunk_embeddings
) AS $$
BEGIN
    RETURN QUERY
    WITH claimed AS (
        UPDATE indexing_outbox o
        SET status = 'processing',
            processing_started_at = NOW()  -- Track when processing started
        WHERE o.id IN (
            SELECT id FROM indexing_outbox
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT batch_size
            FOR UPDATE SKIP LOCKED
        )
        RETURNING o.*
    )
    SELECT
        c.id as outbox_id,
        c.action,
        c.chunk_id,
        c.file_id,
        c.source_id,
        c.payload,
        ce.vector
    FROM claimed c
    LEFT JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id;
END;
$$ LANGUAGE plpgsql;

-- Grant permissions to mainrag user
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mainrag;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO mainrag;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO mainrag;

-- Additive storage-v2 generation schema. Numbered migrations are authoritative;
-- this psql-relative include keeps the historical bootstrap in sync.
\ir migrations/029_storage_v2_generations.sql
\ir migrations/030_storage_v2_content_bodies.sql
\ir migrations/031_storage_v2_content_graph.sql
\ir migrations/032_storage_v2_shadow_ingest.sql
\ir migrations/033_storage_v2_intelligence.sql
\ir migrations/034_storage_v2_retrieval.sql
\ir migrations/035_storage_v2_shadow_slice.sql
\ir migrations/036_storage_v2_release_candidates.sql
\ir migrations/037_storage_v2_release_candidate_replacement.sql
\ir migrations/038_storage_v2_commit_aware_reuse.sql
\ir migrations/039_storage_v2_unbounded_search_terms.sql
\ir migrations/040_storage_v2_sparse_search_documents.sql
\ir migrations/041_storage_v2_structural_card_bundle.sql
\ir migrations/042_storage_v2_oversized_search_documents.sql
\ir migrations/043_storage_v2_indexed_search_document_reuse.sql
\ir migrations/044_storage_v2_late_search_materialization.sql
