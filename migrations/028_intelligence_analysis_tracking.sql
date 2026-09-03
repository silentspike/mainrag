-- Migration 028: Track per-file intelligence analysis completion
--
-- A file can be successfully analyzed and still produce zero symbols/calls
-- (unsupported syntax, generated stubs, tiny library files, etc.).  The
-- intelligence backfill must distinguish that state from "never analyzed";
-- otherwise zero-symbol files are selected forever.

SET app.user_id = 'db8e73cc-f562-40c5-b3ca-70e6a042ef89';
SET app.is_admin = 'true';

ALTER TABLE files
    ADD COLUMN IF NOT EXISTS intelligence_analyzed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS intelligence_symbols_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS intelligence_calls_count INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_files_intelligence_pending
    ON files (source_id, updated_at, id)
    WHERE intelligence_analyzed_at IS NULL;

-- Preserve current behavior for files that already have symbol rows: they are
-- known to have been analyzed by the intelligence layer before this ledger
-- existed. Files with zero symbols remain NULL and are intentionally eligible
-- for one explicit backfill pass.
WITH symbol_counts AS (
    SELECT file_id, COUNT(*)::INTEGER AS symbols_count
    FROM symbols
    GROUP BY file_id
),
call_counts AS (
    SELECT s.file_id, COUNT(cg.*)::INTEGER AS calls_count
    FROM symbols s
    JOIN call_graph cg ON cg.caller_symbol_id = s.id
    GROUP BY s.file_id
)
UPDATE files f
SET intelligence_analyzed_at = NOW(),
    intelligence_symbols_count = symbol_counts.symbols_count,
    intelligence_calls_count = COALESCE(call_counts.calls_count, 0)
FROM symbol_counts
LEFT JOIN call_counts ON call_counts.file_id = symbol_counts.file_id
WHERE f.id = symbol_counts.file_id
  AND f.intelligence_analyzed_at IS NULL;
