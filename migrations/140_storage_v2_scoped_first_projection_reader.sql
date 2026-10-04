-- Match the established lexical reader boundary for exact first positions.
-- Only the isolated definer role gets the fast policy. Its guarded functions
-- authorize the complete requested source set before reading projection rows.
BEGIN;

DO $guard$
DECLARE reader REGROLE:='mainrag_v2_lexical_rank_owner'::REGROLE;
        routine RECORD; relation RECORD; projection REGCLASS;
BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE oid=reader AND NOT rolcanlogin
        AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole)
       OR EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid=reader) THEN
        RAISE EXCEPTION 'ordinary first-term isolated reader role differs';
    END IF;
    SELECT * INTO STRICT routine FROM pg_proc
     WHERE oid='storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)'::REGPROCEDURE;
    IF encode(sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8')),'hex')
        <>'057c63e225d88fca6c55769dab5f72722997120ae284e3746ec1d51eeeefb368'
       OR routine.proowner<>reader OR NOT routine.prosecdef
       OR NOT has_function_privilege('mainrag_v2_frontier_owner',routine.oid,'EXECUTE')
       OR EXISTS(SELECT 1 FROM aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) permission
          WHERE permission.grantee NOT IN (reader,'mainrag_v2_frontier_owner'::REGROLE)
             OR permission.privilege_type<>'EXECUTE') THEN
        RAISE EXCEPTION 'ordinary first-term bounded reader authority differs';
    END IF;
    FOREACH projection IN ARRAY ARRAY['storage_v2_ordinary_first_coverage'::REGCLASS,
                                      'storage_v2_ordinary_first_term'::REGCLASS] LOOP
        SELECT * INTO STRICT relation FROM pg_class WHERE oid=projection;
        IF NOT relation.relrowsecurity OR NOT relation.relforcerowsecurity
           OR NOT EXISTS(SELECT 1 FROM pg_roles WHERE oid=relation.relowner AND rolsuper)
           OR NOT has_table_privilege(reader,projection,'SELECT')
           OR EXISTS(SELECT 1 FROM aclexplode(coalesce(relation.relacl,acldefault('r',relation.relowner))) permission
              WHERE permission.grantee<>relation.relowner
                AND (permission.grantee<>reader OR permission.privilege_type<>'SELECT'
                     OR permission.is_grantable))
           OR EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=projection AND attacl IS NOT NULL)
           OR (SELECT count(*) FROM pg_policy WHERE polrelid=projection)<>1
           OR NOT EXISTS(SELECT 1 FROM pg_policy WHERE polrelid=projection AND polcmd='r'
                AND polpermissive AND polroles=ARRAY[0::OID] AND polwithcheck IS NULL
                AND pg_get_expr(polqual,polrelid)='storage_v2_can_access_source(source_id, ''read''::text)') THEN
            RAISE EXCEPTION 'ordinary first-term projection authority differs';
        END IF;
    END LOOP;
END $guard$;

CREATE POLICY ordinary_first_coverage_rank_reader ON storage_v2_ordinary_first_coverage
    FOR SELECT TO mainrag_v2_lexical_rank_owner USING(TRUE);
CREATE POLICY ordinary_first_term_rank_reader ON storage_v2_ordinary_first_term
    FOR SELECT TO mainrag_v2_lexical_rank_owner USING(TRUE);

COMMIT;
