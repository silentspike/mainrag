-- Phase 14: Embedding Dimension Update for BGE-m3 (1024-dim)
-- Applied when upgrading from 768-dim to 1024-dim models
-- This is a HIGH-IMPACT migration - coordinate with team before running!

BEGIN;

-- Step 1: Backup current embeddings (just in case)
-- This creates a table to restore from if needed
CREATE TABLE IF NOT EXISTS chunk_embeddings_backup_768dim AS
SELECT * FROM chunk_embeddings;

-- Step 2: Drop dependent indexes
DROP INDEX IF EXISTS idx_chunk_embeddings_vector;
DROP INDEX IF EXISTS idx_chunk_embeddings_vector_hnsw;

-- Step 3: Convert vector column to new dimension
-- PostgreSQL pgvector allows dimension change via casting
ALTER TABLE chunk_embeddings
ALTER COLUMN vector TYPE vector(1024);

-- Step 4: Create new optimized HNSW index for 1024-dim vectors
-- Slightly different parameters for higher-dimensional space
CREATE INDEX idx_chunk_embeddings_vector_hnsw
ON chunk_embeddings
USING hnsw (vector vector_cosine_ops)
WITH (m = 16, ef_construction = 200);

-- Step 5: Update model tracking (if using this pattern)
UPDATE chunk_embeddings
SET model = 'BAAI/bge-m3'
WHERE model = 'BAAI/bge-base-en-v1.5' OR model IS NULL;

-- Step 6: Analyze statistics for query planner
ANALYZE chunk_embeddings;

-- Step 7: Log migration
-- INSERT INTO maintenance_log (operation, description, status, completed_at)
-- VALUES ('phase_14_dimension_upgrade', 'Upgraded embedding dimension from 768 to 1024', 'completed', NOW());

-- Verify migration
SELECT
    count(*) as total_embeddings,
    sum(case when vector IS NOT NULL then 1 else 0 end) as non_null_embeddings,
    sum(case when vector::text = '[]' then 1 else 0 end) as empty_embeddings
FROM chunk_embeddings;

COMMIT;

-- After running this migration:
-- 1. Verify all embeddings are still present
-- 2. Run: REINDEX INDEX idx_chunk_embeddings_vector_hnsw;
-- 3. Re-index all sources with new model: mainrag source sync --force <source_id>
