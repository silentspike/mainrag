-- Run as a database administrator after 065. The API login also owns the
-- historical schema, so revoking its frontier DML in 062/063 denied the
-- SECURITY DEFINER publishers themselves. A separate NOLOGIN owner restores
-- the intended boundary: publishers may write, while the API may only call
-- the checked publishers and read the frontier.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mainrag_v2_frontier_owner') THEN
        CREATE ROLE mainrag_v2_frontier_owner NOLOGIN INHERIT;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles
         WHERE rolname = 'mainrag_v2_frontier_owner'
           AND (rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb
                OR rolreplication OR rolbypassrls OR NOT rolinherit)
    ) OR pg_has_role('mainrag', 'mainrag_v2_frontier_owner', 'MEMBER') THEN
        RAISE EXCEPTION 'unsafe storage-v2 frontier owner role';
    END IF;
END
$$;

-- The publishers read and lock existing mainrag-owned ingest state. Inherit
-- those privileges in the definer role; do not grant the reverse membership.
GRANT mainrag TO mainrag_v2_frontier_owner;

ALTER TABLE storage_v2_append_frontier OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE storage_v2_managed_append_frontier OWNER TO mainrag_v2_frontier_owner;
-- ALTER OWNER carries the former owner's restricted ACL forward. Restore DML
-- only to the dedicated definer, never to the API login.
GRANT INSERT, UPDATE, DELETE ON
    storage_v2_append_frontier, storage_v2_managed_append_frontier
    TO mainrag_v2_frontier_owner;
ALTER FUNCTION storage_v2_publish_full_append_frontiers(BIGINT, TEXT[])
    OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION storage_v2_publish_managed_append_frontier(
    BIGINT, BIGINT, UUID, BIGINT, BYTEA, BOOLEAN
) OWNER TO mainrag_v2_frontier_owner;
-- The definer is no longer the owner of the ingest/run tables. Their RLS
-- policies must now be evaluated under the caller's app.user_id instead of
-- failing row_security=off at the first locked read.
ALTER FUNCTION storage_v2_publish_full_append_frontiers(BIGINT, TEXT[])
    SET row_security = on;
ALTER FUNCTION storage_v2_publish_managed_append_frontier(
    BIGINT, BIGINT, UUID, BIGINT, BYTEA, BOOLEAN
) SET row_security = on;

REVOKE INSERT, UPDATE, DELETE ON
    storage_v2_append_frontier, storage_v2_managed_append_frontier
    FROM PUBLIC, mainrag;
GRANT SELECT ON storage_v2_append_frontier, storage_v2_managed_append_frontier
    TO mainrag;
REVOKE EXECUTE ON FUNCTION storage_v2_publish_full_append_frontiers(BIGINT, TEXT[])
    FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION storage_v2_publish_managed_append_frontier(
    BIGINT, BIGINT, UUID, BIGINT, BYTEA, BOOLEAN
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_publish_full_append_frontiers(BIGINT, TEXT[])
    TO mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_publish_managed_append_frontier(
    BIGINT, BIGINT, UUID, BIGINT, BYTEA, BOOLEAN
) TO mainrag;

DO $$
BEGIN
    IF has_table_privilege('mainrag', 'storage_v2_append_frontier', 'INSERT')
       OR has_table_privilege('mainrag', 'storage_v2_append_frontier', 'UPDATE')
       OR has_table_privilege('mainrag', 'storage_v2_append_frontier', 'DELETE')
       OR has_table_privilege('mainrag', 'storage_v2_managed_append_frontier', 'INSERT')
       OR has_table_privilege('mainrag', 'storage_v2_managed_append_frontier', 'UPDATE')
       OR has_table_privilege('mainrag', 'storage_v2_managed_append_frontier', 'DELETE')
       OR NOT has_table_privilege('mainrag_v2_frontier_owner',
           'storage_v2_append_frontier', 'INSERT')
       OR NOT has_table_privilege('mainrag_v2_frontier_owner',
           'storage_v2_append_frontier', 'UPDATE')
       OR NOT has_table_privilege('mainrag_v2_frontier_owner',
           'storage_v2_managed_append_frontier', 'INSERT')
       OR NOT has_table_privilege('mainrag_v2_frontier_owner',
           'storage_v2_managed_append_frontier', 'UPDATE')
       OR NOT has_function_privilege('mainrag',
           'storage_v2_publish_full_append_frontiers(bigint,text[])', 'EXECUTE')
       OR NOT has_function_privilege('mainrag',
           'storage_v2_publish_managed_append_frontier(bigint,bigint,uuid,bigint,bytea,boolean)',
           'EXECUTE') THEN
        RAISE EXCEPTION 'storage-v2 frontier privilege boundary is incomplete';
    END IF;
END
$$;
