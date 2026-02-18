-- Rollback: Re-enable FORCE RLS on data tables
-- WARNING: This will cause significant FTS performance degradation (1-25s per query)

BEGIN;

ALTER TABLE sources FORCE ROW LEVEL SECURITY;
ALTER TABLE files FORCE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;

COMMIT;
