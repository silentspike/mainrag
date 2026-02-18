-- Migration: Add missing columns to symbols table
-- Required for Code Intelligence (intelligence.rs:60-71)
-- Date: 2026-01-07
--
-- This migration:
-- 1. Adds columns that intelligence.rs expects but schema was missing
-- 2. Creates UNIQUE constraint for ON CONFLICT clause
-- 3. Populates context from signature for API compatibility

BEGIN;

-- 1. Add new columns (IF NOT EXISTS handles re-runs safely)
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS qualified_name TEXT;
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS signature TEXT;
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS doc_comment TEXT;
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS visibility TEXT;
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS language TEXT;

-- 2. Create UNIQUE constraint for ON CONFLICT (file_id, name, line_start)
-- Use CREATE INDEX IF NOT EXISTS pattern
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE indexname = 'idx_symbols_unique'
    ) THEN
        CREATE UNIQUE INDEX idx_symbols_unique ON symbols(file_id, name, line_start);
    END IF;
END $$;

-- 3. Populate context from signature where context is NULL
-- This ensures API reads correct data
UPDATE symbols
SET context = signature
WHERE context IS NULL AND signature IS NOT NULL;

-- Add comment for documentation
COMMENT ON TABLE symbols IS 'Tree-sitter extracted symbols with full metadata (intelligence.rs)';
COMMENT ON COLUMN symbols.qualified_name IS 'Full path: module::Class::method';
COMMENT ON COLUMN symbols.signature IS 'Full function signature';
COMMENT ON COLUMN symbols.doc_comment IS 'Extracted docstring/comment';
COMMENT ON COLUMN symbols.visibility IS 'pub, private, protected, etc.';
COMMENT ON COLUMN symbols.language IS 'rust, python, go, etc.';
COMMENT ON COLUMN symbols.context IS 'Signature preview - populated from signature column';

COMMIT;
