-- Migration 17: Add processing_started_at for accurate Reaper logic
--
-- Problem: Reaper using created_at would incorrectly reset old entries
-- that were just claimed from a backlog
-- Solution: Track when processing actually started, not when entry was created
--
-- Run: PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag -f 17_outbox_processing_timestamp.sql

-- Add column for tracking when processing started
ALTER TABLE indexing_outbox
ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ;

-- Index for efficient Reaper queries (partial index on processing entries)
CREATE INDEX IF NOT EXISTS idx_outbox_processing_stale
ON indexing_outbox(processing_started_at)
WHERE status = 'processing';

-- Update claim_outbox_batch to set processing_started_at
CREATE OR REPLACE FUNCTION claim_outbox_batch(batch_size INT DEFAULT 100)
RETURNS TABLE (
    outbox_id BIGINT,
    action VARCHAR,
    chunk_id BIGINT,
    file_id BIGINT,
    source_id BIGINT,
    payload JSONB,
    vector vector  -- No dimension - PostgreSQL infers from chunk_embeddings
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

COMMENT ON COLUMN indexing_outbox.processing_started_at IS 'Timestamp when processing started (for Reaper timeout logic)';
