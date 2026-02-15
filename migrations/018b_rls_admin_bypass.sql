-- Migration 018b: RLS Admin Bypass via app.is_admin
-- Sprint 1.4: Admin users should see all data without being filtered by RLS
--
-- IMPORTANT: current_setting with missing_ok=true (second param)!
-- Without this, background jobs and migrations crash because app.user_id is not set.
-- NULL::uuid = user_id → NULL (not false!) → PostgreSQL RLS treats NULL as "deny" → safe.

-- Update RLS policies on sources to allow admin bypass
DO $$ BEGIN
    -- Drop existing policy if it exists and recreate with admin bypass
    IF EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'sources' AND policyname = 'sources_user_isolation') THEN
        DROP POLICY sources_user_isolation ON sources;
    END IF;
END $$;

-- Recreate with admin bypass (only if RLS is enabled on sources)
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE tablename = 'sources' AND rowsecurity = true) THEN
        CREATE POLICY sources_user_isolation ON sources
            USING (
                user_id = current_setting('app.user_id', true)::uuid
                OR current_setting('app.is_admin', true) = 'true'
            );
    END IF;
END $$;

-- Add similar bypass to other RLS-enabled tables if they exist
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE tablename = 'files' AND rowsecurity = true) THEN
        IF EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'files' AND policyname = 'files_user_isolation') THEN
            DROP POLICY files_user_isolation ON files;
        END IF;
        CREATE POLICY files_user_isolation ON files
            USING (
                source_id IN (SELECT id FROM sources WHERE user_id = current_setting('app.user_id', true)::uuid)
                OR current_setting('app.is_admin', true) = 'true'
            );
    END IF;
END $$;

COMMENT ON POLICY sources_user_isolation ON sources IS 'RLS with admin bypass: agents see own data, admins see all';
