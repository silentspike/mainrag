-- Keep lexical-presence checks inside the table's forced source RLS boundary.
-- The exact and active search functions use row_security=off for their existing
-- controlled reads; a direct scan of this table in either function is denied.
CREATE FUNCTION storage_v2_has_lexical_segment(p_occurrence_id BIGINT)
RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
    SELECT EXISTS (
        SELECT 1
          FROM storage_v2_lexical_segment segment
         WHERE segment.occurrence_id = p_occurrence_id
           AND storage_v2_can_access_source(segment.source_id, 'read')
    )
$$;

ALTER FUNCTION storage_v2_has_lexical_segment(BIGINT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_has_lexical_segment(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_has_lexical_segment(BIGINT) TO mainrag;

DO $$
DECLARE
    v_function REGPROCEDURE;
    v_sql TEXT;
    v_old TEXT := E'NOT EXISTS (\n                 SELECT 1 FROM storage_v2_lexical_segment segment\n                  WHERE segment.occurrence_id = matched.id\n             )';
    v_new TEXT := 'NOT storage_v2_has_lexical_segment(matched.id)';
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE,
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::REGPROCEDURE
    ] LOOP
        v_sql := pg_get_functiondef(v_function);
        IF strpos(v_sql, v_old) = 0 OR strpos(v_sql, v_new) <> 0 THEN
            RAISE EXCEPTION 'storage-v2 conjunctive lexical definition changed before RLS repair';
        END IF;
        EXECUTE replace(v_sql, v_old, v_new);
    END LOOP;
END
$$;
