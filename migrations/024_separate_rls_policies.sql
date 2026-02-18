-- Migration 024: Split OR+EXISTS RLS policies into separate admin/user policies
-- Rationale: The combined OR policy forces PG to evaluate both branches per row.
-- Separate policies allow PG to short-circuit on the admin check.
--
-- Rollback: migrations/024_rollback_separate_policies.sql

BEGIN;

-- ═══ CHUNKS: chunk_access_policy → 2 separate Policies ═══
DROP POLICY IF EXISTS chunk_access_policy ON chunks;

CREATE POLICY chunks_admin_policy ON chunks FOR SELECT
USING (
    EXISTS(
        SELECT 1 FROM users
        WHERE id = (NULLIF(current_setting('app.user_id', true), ''))::UUID
          AND is_admin = TRUE
    )
);

CREATE POLICY chunks_user_policy ON chunks FOR SELECT
USING (
    EXISTS(
        SELECT 1 FROM files f
        WHERE f.id = file_id
          AND user_can_access_source(
              (NULLIF(current_setting('app.user_id', true), ''))::UUID,
              f.source_id,
              'read'
          )
    )
);

-- ═══ FILES: file_access_policy → 2 separate Policies ═══
DROP POLICY IF EXISTS file_access_policy ON files;

CREATE POLICY files_admin_policy ON files FOR SELECT
USING (
    EXISTS(
        SELECT 1 FROM users
        WHERE id = (NULLIF(current_setting('app.user_id', true), ''))::UUID
          AND is_admin = TRUE
    )
);

CREATE POLICY files_user_policy ON files FOR SELECT
USING (
    user_can_access_source(
        (NULLIF(current_setting('app.user_id', true), ''))::UUID,
        source_id,
        'read'
    )
);

COMMIT;
