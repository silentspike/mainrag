-- Resolve source authorization once before reading immutable presence metadata.
-- The isolated role can only select; application roles cannot assume it.
BEGIN;

DO $guard$
DECLARE routine RECORD; relation REGCLASS;
BEGIN
    SELECT * INTO STRICT routine FROM pg_proc
     WHERE oid='storage_v2_source_segment_presence(bigint[])'::REGPROCEDURE;
    IF encode(sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8')),'hex')
          <>'7947e57eecb8ac8afbf147a38331a506ef014aaa1c2794f1a76c73c66312a843'
       OR routine.proowner<>'mainrag_v2_frontier_owner'::REGROLE
       OR NOT routine.prosecdef OR NOT routine.proisstrict OR routine.provolatile<>'s'
       OR NOT has_function_privilege('mainrag',routine.oid,'EXECUTE')
       OR EXISTS(SELECT 1 FROM aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) a
          WHERE a.grantee NOT IN (routine.proowner,'mainrag'::REGROLE)
             OR a.privilege_type<>'EXECUTE'
             OR (a.is_grantable AND a.grantee<>routine.proowner)) THEN
        RAISE EXCEPTION 'isolated presence reader definition or authority differs';
    END IF;
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mainrag_v2_presence_owner') THEN
        RAISE EXCEPTION 'isolated presence reader role already exists';
    END IF;
    FOREACH relation IN ARRAY ARRAY['occurrence'::REGCLASS,
        'storage_v2_legacy_rank_binding'::REGCLASS,'storage_v2_lexical_segment'::REGCLASS,
        'storage_v2_compact_lexical_block'::REGCLASS] LOOP
        IF NOT EXISTS(SELECT 1 FROM pg_class WHERE oid=relation AND relrowsecurity
              AND relforcerowsecurity=(relation=ANY(ARRAY[
                  'storage_v2_lexical_segment'::REGCLASS,'storage_v2_compact_lexical_block'::REGCLASS]))
              AND relowner=CASE WHEN relation='occurrence'::REGCLASS
                  THEN 'mainrag'::REGROLE ELSE 'mainrag_v2_frontier_owner'::REGROLE END)
           OR EXISTS(SELECT 1 FROM pg_policy WHERE polrelid=relation AND polname='storage_v2_presence_reader') THEN
            RAISE EXCEPTION 'isolated presence reader relation boundary differs';
        END IF;
    END LOOP;
END $guard$;

CREATE ROLE mainrag_v2_presence_owner NOLOGIN NOSUPERUSER NOCREATEDB
    NOCREATEROLE NOINHERIT NOBYPASSRLS;
GRANT USAGE ON SCHEMA public TO mainrag_v2_presence_owner;
-- The existing source policy consults only these two user columns.
GRANT SELECT(id,is_admin) ON users TO mainrag_v2_presence_owner;
GRANT SELECT ON sources,occurrence,storage_v2_legacy_rank_binding,
    storage_v2_lexical_segment,storage_v2_compact_lexical_block TO mainrag_v2_presence_owner;

CREATE POLICY storage_v2_presence_reader ON occurrence
    FOR SELECT TO mainrag_v2_presence_owner USING(TRUE);
CREATE POLICY storage_v2_presence_reader ON storage_v2_legacy_rank_binding
    FOR SELECT TO mainrag_v2_presence_owner USING(TRUE);
CREATE POLICY storage_v2_presence_reader ON storage_v2_lexical_segment
    FOR SELECT TO mainrag_v2_presence_owner USING(TRUE);
CREATE POLICY storage_v2_presence_reader ON storage_v2_compact_lexical_block
    FOR SELECT TO mainrag_v2_presence_owner USING(TRUE);

-- The original function already resolves authorized sources before inspecting
-- requested occurrences, and retains every physical identity predicate.
-- Only its owner changes; row_security remains on and the role cannot bypass it.
ALTER FUNCTION storage_v2_source_segment_presence(bigint[]) OWNER TO mainrag_v2_presence_owner;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_presence(bigint[])
    TO mainrag,mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_source_segment_presence(bigint[]) FROM PUBLIC;

DO $proof$
DECLARE reader REGROLE:='mainrag_v2_presence_owner'::REGROLE; relation REGCLASS;
BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE oid=reader AND NOT rolcanlogin
        AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole AND NOT rolcreatedb AND NOT rolinherit)
       OR EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid=reader OR member=reader)
       OR EXISTS(SELECT 1 FROM pg_proc WHERE proowner=reader
          AND oid<>'storage_v2_source_segment_presence(bigint[])'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'isolated presence reader role is not bounded';
    END IF;
    FOREACH relation IN ARRAY ARRAY['occurrence'::REGCLASS,
        'storage_v2_legacy_rank_binding'::REGCLASS,'storage_v2_lexical_segment'::REGCLASS,
        'storage_v2_compact_lexical_block'::REGCLASS] LOOP
        IF NOT has_table_privilege(reader,relation,'SELECT')
           OR has_table_privilege(reader,relation,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'isolated presence reader write privileges differ';
        END IF;
    END LOOP;
END $proof$;

COMMIT;
