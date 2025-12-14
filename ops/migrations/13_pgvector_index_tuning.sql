-- Phase 13: pgvector Index Tuning for pgvector-only Architecture
-- Optimizes HNSW index parameters for single-source vector search
-- Applied BEFORE migration to pgvector-only mode

BEGIN;

-- Step 1: Drop existing index if it exists
DROP INDEX IF EXISTS idx_chunk_embeddings_vector;
DROP INDEX IF EXISTS idx_chunk_embeddings_vector_hnsw;

-- Step 2: Create optimized HNSW index with higher parameters
-- m=24 (vs default 16) for better connectivity
-- ef_construction=256 (vs default 200) for better build quality
CREATE INDEX idx_chunk_embeddings_vector_hnsw
ON chunk_embeddings
USING hnsw (vector vector_cosine_ops)
WITH (m = 24, ef_construction = 256);

-- Step 3: Optimize runtime parameters
-- These can also be set in postgresql.conf or via SET command
SET hnsw.ef_search = 100;              -- Increased from default 64 for better recall
SET ivfflat.probes = 20;               -- Fallback for IVFFlat searches if needed

-- Step 4: Analyze statistics for query planner
ANALYZE chunk_embeddings;

-- Step 5: Verify index creation
-- This query should show the new HNSW index
SELECT
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename = 'chunk_embeddings'
ORDER BY indexname;

COMMIT;

-- Log entry for audit trail
-- INSERT INTO maintenance_log (operation, description, status, completed_at)
-- VALUES ('phase_13_index_tuning', 'pgvector HNSW index optimization', 'completed', NOW());
