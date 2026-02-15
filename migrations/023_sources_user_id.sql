-- Migration 023: Add user_id to sources for Qdrant tenant isolation (K4)
--
-- Sources need an owner (user_id) so that:
-- 1. Qdrant points include user_id in payload for tenant filtering
-- 2. Each agent's search results are scoped to their own data
--
-- Existing sources are assigned to the default admin user.
-- NOTE: Must set RLS context before modifying RLS-protected tables.

-- Set RLS context for this migration session
SET app.user_id = 'db8e73cc-f562-40c5-b3ca-70e6a042ef89';
SET app.is_admin = 'true';

-- Add user_id column (nullable first for backfill, then NOT NULL)
ALTER TABLE sources ADD COLUMN IF NOT EXISTS user_id UUID;

-- Backfill: All existing sources belong to the default admin user
UPDATE sources SET user_id = 'db8e73cc-f562-40c5-b3ca-70e6a042ef89' WHERE user_id IS NULL;

-- Now make it NOT NULL with default
ALTER TABLE sources ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE sources ALTER COLUMN user_id SET DEFAULT 'db8e73cc-f562-40c5-b3ca-70e6a042ef89';

-- Foreign key to users
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'sources_user_id_fkey' AND table_name = 'sources'
    ) THEN
        ALTER TABLE sources ADD CONSTRAINT sources_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(id);
    END IF;
END $$;

-- Index for efficient lookups by user_id
CREATE INDEX IF NOT EXISTS idx_sources_user_id ON sources (user_id);

-- Drop old function first (return type changed — cannot use CREATE OR REPLACE)
DROP FUNCTION IF EXISTS claim_outbox_batch(integer);

-- Recreate claim_outbox_batch with user_id output column
CREATE FUNCTION claim_outbox_batch(batch_size INTEGER DEFAULT 100)
RETURNS TABLE(
    outbox_id BIGINT,
    action VARCHAR,
    chunk_id BIGINT,
    file_id BIGINT,
    source_id BIGINT,
    payload JSONB,
    vector vector,
    user_id UUID  -- NEW: for Qdrant tenant isolation
)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    WITH claimed AS (
        UPDATE indexing_outbox o
        SET status = 'processing',
            processing_started_at = NOW()
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
        ce.vector,
        s.user_id  -- JOIN sources to get owner
    FROM claimed c
    LEFT JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
    LEFT JOIN sources s ON c.source_id = s.id;
END;
$$;
