-- Run as a database administrator after 066. The API role owns the ordinary
-- ingest function and its receipt table in 065, but has no INSERT privilege on
-- that table. The dedicated NOLOGIN definer owns the receipt and controlled
-- function; only its checked function may advance an active pointer and write
-- the chained receipt in the same transaction.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'mainrag_v2_frontier_owner'
          AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreaterole
          AND NOT rolcreatedb AND NOT rolreplication AND NOT rolbypassrls
          AND rolinherit
    ) OR EXISTS (
        SELECT 1 FROM pg_roles login
         WHERE login.rolcanlogin AND NOT login.rolsuper
           AND pg_has_role(login.oid, 'mainrag_v2_frontier_owner', 'MEMBER')
    )
      OR NOT pg_has_role('mainrag_v2_frontier_owner', 'mainrag', 'USAGE') THEN
        RAISE EXCEPTION 'migration 066 dedicated definer role is required';
    END IF;
END
$$;

ALTER TABLE storage_v2_active_ingest_receipt OWNER TO mainrag_v2_frontier_owner;
GRANT INSERT, UPDATE, DELETE ON storage_v2_active_ingest_receipt
    TO mainrag_v2_frontier_owner;
ALTER FUNCTION storage_v2_activate_regular_ingest(
    TEXT, BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT
) OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION storage_v2_activate_regular_ingest(
    TEXT, BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT
) SET row_security = on;

-- Only the NOLOGIN definer may update the two pointer/state tables through
-- this path. Their ordinary owner rule remains unchanged for every other
-- table and role. The ingest function checks the exact activation manifest,
-- old pointer, generation root, watermark, and current receipt before these
-- updates; a failed receipt INSERT rolls the pointer updates back.
CREATE OR REPLACE FUNCTION storage_v2_guard_controlled_update() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_owner OID;
BEGIN
    SELECT relowner INTO v_owner FROM pg_class WHERE oid = TG_RELID;
    IF current_user::REGROLE::OID <> v_owner
       AND NOT (
           TG_OP = 'UPDATE'
           AND current_user = 'mainrag_v2_frontier_owner'
           AND TG_RELID IN (
               'public.logical_source'::REGCLASS,
               'public.source_generation'::REGCLASS
           )
       ) THEN
        RAISE EXCEPTION 'storage-v2 state changes require a controlled function'
            USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

REVOKE INSERT, UPDATE, DELETE ON storage_v2_active_ingest_receipt
    FROM PUBLIC, mainrag;
GRANT SELECT ON storage_v2_active_ingest_receipt TO mainrag;
REVOKE EXECUTE ON FUNCTION storage_v2_activate_regular_ingest(
    TEXT, BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_activate_regular_ingest(
    TEXT, BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT
) TO mainrag;

DO $$
BEGIN
    IF has_table_privilege('mainrag', 'storage_v2_active_ingest_receipt', 'INSERT')
       OR has_table_privilege('mainrag', 'storage_v2_active_ingest_receipt', 'UPDATE')
       OR has_table_privilege('mainrag', 'storage_v2_active_ingest_receipt', 'DELETE')
       OR NOT has_table_privilege('mainrag_v2_frontier_owner',
           'storage_v2_active_ingest_receipt', 'INSERT')
       OR NOT has_function_privilege('mainrag',
           'storage_v2_activate_regular_ingest(text,bigint,bigint,bigint,text,text,text,text,text)',
           'EXECUTE')
       OR (SELECT relowner::REGROLE::TEXT <> 'mainrag_v2_frontier_owner'
             FROM pg_class WHERE oid = 'public.storage_v2_active_ingest_receipt'::REGCLASS)
       OR (SELECT proowner::REGROLE::TEXT <> 'mainrag_v2_frontier_owner'
             FROM pg_proc WHERE oid =
               'public.storage_v2_activate_regular_ingest(text,bigint,bigint,bigint,text,text,text,text,text)'::REGPROCEDURE)
    THEN
        RAISE EXCEPTION 'active ingest receipt privilege boundary is incomplete';
    END IF;
END
$$;
