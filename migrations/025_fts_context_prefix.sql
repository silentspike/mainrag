-- Migration 025: Add context_prefix to chunk FTS vector
--
-- Problem: chunks.fts_vector only indexes content_text + chunk_type.
-- File paths and function scope are only in context_prefix column but
-- not searchable via FTS. Query "search handler" can't find chunks
-- in api/src/api/handlers/search.rs.
--
-- Fix: Include context_prefix (format: [source] path > fn parent)
-- as Weight B in the FTS vector. This makes file paths and parent
-- scope names searchable alongside content.
--
-- Impact: ~545K rows will have fts_vector recomputed. GIN index rebuild.
-- Expected duration: 2-5 minutes on 545K rows.

BEGIN;

-- Drop old generated column and GIN index
DROP INDEX IF EXISTS idx_chunks_fts;
ALTER TABLE chunks DROP COLUMN IF EXISTS fts_vector;

-- Recreate with context_prefix as Weight B
ALTER TABLE chunks ADD COLUMN fts_vector TSVECTOR GENERATED ALWAYS AS (
    setweight(to_tsvector('simple', COALESCE(content_text, '')), 'A') ||
    setweight(to_tsvector('simple', COALESCE(context_prefix, '')), 'B') ||
    setweight(to_tsvector('simple', COALESCE(chunk_type, '')), 'C')
) STORED;

-- Rebuild GIN index (fastupdate=off for read performance)
CREATE INDEX idx_chunks_fts ON chunks USING GIN (fts_vector) WITH (fastupdate = off);

COMMIT;
