-- Migration: Add indexing_outbox table for transactional Qdrant sync
-- Required for Outbox Pattern (Phase 2)
-- Date: 2026-01-07
--
-- This migration:
-- 1. Creates indexing_outbox table for async Qdrant synchronization
-- 2. Creates indexes for efficient polling
-- 3. Creates claim_outbox_batch() function with SKIP LOCKED pattern

BEGIN;

-- 1. Create table
CREATE TABLE IF NOT EXISTS indexing_outbox (
    id BIGSERIAL PRIMARY KEY,

    -- Action
    action VARCHAR(20) NOT NULL,        -- 'upsert', 'delete'

    -- References (Worker JOINt chunk_embeddings für vector)
    chunk_id BIGINT NOT NULL,
    file_id BIGINT REFERENCES files(id) ON DELETE SET NULL,
    source_id BIGINT REFERENCES sources(id) ON DELETE SET NULL,

    -- Minimal payload (nur was Qdrant-Index braucht, nicht der vector)
    payload JSONB NOT NULL DEFAULT '{}',

    -- Processing state
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, processing, done, failed
    processed_at TIMESTAMPTZ,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,

    -- Metadata
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Indexes for efficient polling
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON indexing_outbox(created_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_outbox_chunk ON indexing_outbox(chunk_id);

-- 3. Claim function for worker (SKIP LOCKED pattern)
-- HINWEIS: vector(768) ist für BGE-base-en-v1.5. Bei Modellwechsel anpassen!
CREATE OR REPLACE FUNCTION claim_outbox_batch(batch_size INT DEFAULT 100)
RETURNS TABLE (
    outbox_id BIGINT,
    action VARCHAR,
    chunk_id BIGINT,
    file_id BIGINT,
    source_id BIGINT,
    payload JSONB,
    vector vector(768)  -- pgvector type, Dimension = EMBEDDING_DIMENSION
) AS $$
BEGIN
    RETURN QUERY
    WITH claimed AS (
        UPDATE indexing_outbox o
        SET status = 'processing'
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

-- Add comment for documentation
COMMENT ON TABLE indexing_outbox IS 'Transactional queue for async Qdrant synchronization (outbox pattern)';
COMMENT ON COLUMN indexing_outbox.action IS 'upsert or delete';
COMMENT ON COLUMN indexing_outbox.chunk_id IS 'References chunks.id - worker uses this to fetch vector from chunk_embeddings';
COMMENT ON COLUMN indexing_outbox.status IS 'pending -> processing -> done/failed';
COMMENT ON FUNCTION claim_outbox_batch IS 'Claims batch of pending entries using SKIP LOCKED pattern';

COMMIT;
