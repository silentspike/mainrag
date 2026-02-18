-- Rollback for Migration 024: Restore original combined OR+EXISTS policies
-- Use when Strategy C needs to be reverted.

BEGIN;

-- Chunks: restore original combined policy
DROP POLICY IF EXISTS chunks_admin_policy ON chunks;
DROP POLICY IF EXISTS chunks_user_policy ON chunks;
CREATE POLICY chunk_access_policy ON chunks
    FOR SELECT
    USING (
        EXISTS (SELECT 1 FROM users u
                WHERE u.id = (NULLIF(current_setting('app.user_id', true), ''))::UUID
                AND u.is_admin = TRUE)
        OR
        EXISTS (SELECT 1 FROM files f WHERE f.id = file_id
                AND user_can_access_source(
                    (NULLIF(current_setting('app.user_id', true), ''))::UUID,
                    f.source_id, 'read'))
    );

-- Files: restore original combined policy
DROP POLICY IF EXISTS files_admin_policy ON files;
DROP POLICY IF EXISTS files_user_policy ON files;
CREATE POLICY file_access_policy ON files
    FOR SELECT
    USING (
        EXISTS (SELECT 1 FROM users u
                WHERE u.id = (NULLIF(current_setting('app.user_id', true), ''))::UUID
                AND u.is_admin = TRUE)
        OR
        user_can_access_source(
            (NULLIF(current_setting('app.user_id', true), ''))::UUID,
            source_id, 'read')
    );

COMMIT;
