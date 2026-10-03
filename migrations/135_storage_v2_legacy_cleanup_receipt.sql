-- Record the transaction that retired an exact legacy PostgreSQL object set.
-- Application roles cannot create a receipt or use this as deletion authority.
BEGIN;
DO $guard$ BEGIN
 IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
  RAISE EXCEPTION 'legacy cleanup receipt installation requires the database administrator';
 END IF;
 IF to_regclass('public.storage_v2_legacy_cleanup_receipt') IS NOT NULL THEN
  RAISE EXCEPTION 'legacy cleanup receipt already exists; validate the installed migration instead of replacing it';
 END IF;
 -- This complete-set definer reads the protected ordinary-ingest receipt.
 -- mainrag cannot bypass that table's RLS after migration 067 moved its owner.
 -- Give the definer the existing dedicated owner, retaining narrow EXECUTE.
 IF NOT EXISTS(SELECT 1 FROM pg_proc p
   WHERE p.oid='public.storage_v2_require_complete_active_set(text)'::regprocedure
     AND p.proowner='mainrag'::regrole AND p.prosecdef AND p.provolatile='s'
     AND p.prorettype='void'::regtype
     AND p.proconfig=ARRAY['search_path=pg_catalog, public','row_security=off']
     AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')=
       'fb5eff5834014df9060d1bb8b02def24221a0f78a56d43849b00b8decab86fa7'
     AND NOT EXISTS(SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
       WHERE a.grantee<>p.proowner OR a.privilege_type<>'EXECUTE'))
   OR NOT EXISTS(SELECT 1 FROM pg_class c
     WHERE c.oid='public.storage_v2_active_ingest_receipt'::regclass
       AND c.relowner='mainrag_v2_frontier_owner'::regrole AND c.relrowsecurity)
   OR NOT pg_has_role('mainrag_v2_frontier_owner','mainrag','USAGE')
   OR pg_has_role('mainrag','mainrag_v2_frontier_owner','MEMBER') THEN
  RAISE EXCEPTION 'complete active-set receipt authority differs';
 END IF;
END $guard$;
ALTER FUNCTION public.storage_v2_require_complete_active_set(TEXT)
 OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION public.storage_v2_require_complete_active_set(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.storage_v2_require_complete_active_set(TEXT) TO mainrag;
CREATE TABLE public.storage_v2_legacy_cleanup_receipt (
 manifest_sha256 TEXT PRIMARY KEY CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
 before_state_sha256 TEXT NOT NULL CHECK(before_state_sha256 ~ '^[0-9a-f]{64}$'),
 pointer_set_sha256 TEXT NOT NULL CHECK(pointer_set_sha256 ~ '^[0-9a-f]{64}$'),
 runtime_package_sha256 TEXT NOT NULL CHECK(runtime_package_sha256 ~ '^[0-9a-f]{64}$'),
 approval_sha256 TEXT NOT NULL CHECK(approval_sha256 ~ '^[0-9a-f]{64}$'),
 result JSONB NOT NULL CHECK(jsonb_typeof(result)='object'),
 committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE public.storage_v2_legacy_cleanup_receipt ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.storage_v2_legacy_cleanup_receipt FROM PUBLIC, mainrag;
COMMIT;
