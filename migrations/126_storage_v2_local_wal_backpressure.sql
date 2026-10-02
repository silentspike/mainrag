-- Read only the local pending WAL byte count for checkpoint backpressure.
-- The application receives no filenames, directory access or archive controls.
BEGIN;
DO $guard$
DECLARE signature OID := to_regprocedure('public.storage_v2_local_wal_ready_bytes()');
BEGIN
 IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
  RAISE EXCEPTION 'local WAL observer installation requires the database administrator';
 END IF;
 IF signature IS NOT NULL THEN
  IF NOT EXISTS(SELECT 1 FROM pg_proc p WHERE p.oid=signature
      AND p.proowner=current_user::regrole AND p.prosecdef AND p.proisstrict
      AND p.provolatile='s' AND NOT p.proretset AND p.prorettype='bigint'::regtype
      AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='sql')
      AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp','row_security=on']
      AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')='54f8cd4d738960e913d06d99c02e4ce9e426ccd4473a35561df4052eba4fb927') THEN
   RAISE EXCEPTION 'local WAL observer definition differs';
  END IF;
  IF NOT has_function_privilege('mainrag',signature,'EXECUTE') OR EXISTS(
    SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
    WHERE p.oid=signature AND (a.grantee NOT IN (p.proowner,'mainrag'::regrole)
      OR a.privilege_type<>'EXECUTE' OR (a.is_grantable AND a.grantee<>p.proowner))) THEN
   RAISE EXCEPTION 'local WAL observer authority differs';
  END IF;
 END IF;
END $guard$;
CREATE OR REPLACE FUNCTION public.storage_v2_local_wal_ready_bytes()
 RETURNS bigint LANGUAGE sql STABLE STRICT SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp SET row_security=on
AS $function$
 SELECT count(*)::bigint * pg_catalog.pg_size_bytes(pg_catalog.current_setting('wal_segment_size'))
 FROM pg_catalog.pg_ls_archive_statusdir() entry WHERE entry.name LIKE '%.ready'
$function$;
REVOKE ALL ON FUNCTION public.storage_v2_local_wal_ready_bytes() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.storage_v2_local_wal_ready_bytes() TO mainrag;
COMMIT;
