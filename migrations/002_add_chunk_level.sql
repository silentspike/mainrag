-- Migration 002: Add hierarchical chunk level
-- Phase 2: Parent-Child Chunking + CCH
-- Date: 2026-01-24

-- Add level column for hierarchical depth tracking
-- Level 0 = File/Document level
-- Level 1 = Class/Section level
-- Level 2 = Function/Subsection level
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS level SMALLINT DEFAULT 0;

-- Create index for level-based queries (e.g., "get all file-level chunks")
CREATE INDEX IF NOT EXISTS idx_chunks_level ON chunks(level);

-- Composite index for efficient parent-child queries
CREATE INDEX IF NOT EXISTS idx_chunks_parent_level ON chunks(parent_chunk_id, level);

-- Update schema version
UPDATE schema_metadata SET value = '3', updated_at = NOW() WHERE key = 'version';
INSERT INTO schema_metadata (key, value) VALUES ('version', '3')
    ON CONFLICT (key) DO UPDATE SET value = '3', updated_at = NOW();

-- Log migration
DO $$
BEGIN
    RAISE NOTICE 'Migration 002: Added level column and indexes for hierarchical chunking';
END $$;
