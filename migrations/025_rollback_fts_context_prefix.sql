-- Rollback Migration 025: Revert to original FTS vector (content_text + chunk_type only)

BEGIN;

DROP INDEX IF EXISTS idx_chunks_fts;
ALTER TABLE chunks DROP COLUMN IF EXISTS fts_vector;

ALTER TABLE chunks ADD COLUMN fts_vector TSVECTOR GENERATED ALWAYS AS (
    setweight(to_tsvector('simple', COALESCE(content_text, '')), 'A') ||
    setweight(to_tsvector('simple', COALESCE(chunk_type, '')), 'B')
) STORED;

CREATE INDEX idx_chunks_fts ON chunks USING GIN (fts_vector) WITH (fastupdate = off);

COMMIT;
