-- Rank a plain conjunction against the same verified, body-backed segment
-- projection used for single-term parity. Boolean, phrase and exact ASTs keep
-- their established semantics. No active pointer or legacy row is changed.
CREATE FUNCTION storage_v2_simple_and_query(p_ast JSONB)
RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_children JSONB;
    v_child JSONB;
    v_value TEXT;
    v_terms TEXT[] := ARRAY[]::TEXT[];
BEGIN
    IF p_ast ->> 'type' <> 'and' THEN RETURN NULL; END IF;
    v_children := p_ast -> 'children';
    IF jsonb_typeof(v_children) <> 'array'
       OR jsonb_array_length(v_children) NOT BETWEEN 2 AND 8 THEN
        RETURN NULL;
    END IF;
    FOR v_child IN SELECT value FROM jsonb_array_elements(v_children) LOOP
        v_value := v_child ->> 'value';
        IF v_child ->> 'type' <> 'term'
           OR v_value IS NULL
           OR lower(v_value) IN ('and', 'or', 'not')
           OR v_value !~ '^[[:alnum:]_]+$' THEN
            RETURN NULL;
        END IF;
        v_terms := array_append(v_terms, v_value);
    END LOOP;
    v_value := array_to_string(v_terms, ' ');
    IF octet_length(v_value) > 128 THEN RETURN NULL; END IF;
    RETURN v_value;
END
$$;

ALTER FUNCTION storage_v2_simple_and_query(JSONB) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_simple_and_query(JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_simple_and_query(JSONB) TO mainrag;

DO $$
DECLARE
    v_function REGPROCEDURE;
    v_sql TEXT;
    v_ast_old TEXT := E'WHERE storage_v2_search_ast_matches(\n             p_ast, matched_terms, matched_phrases, matched_exact\n         ) OR (p_ast ->> ''type'' = ''term'' AND EXISTS (\n             SELECT 1 FROM storage_v2_source_segment_rank(matched.id, p_ast ->> ''value'')\n         ))';
    v_ast_new TEXT := E'WHERE (storage_v2_simple_and_query(p_ast) IS NULL AND (\n             storage_v2_search_ast_matches(\n                 p_ast, matched_terms, matched_phrases, matched_exact\n             ) OR (p_ast ->> ''type'' = ''term'' AND EXISTS (\n                 SELECT 1 FROM storage_v2_source_segment_rank(matched.id, p_ast ->> ''value'')\n             ))\n         )) OR (storage_v2_simple_and_query(p_ast) IS NOT NULL AND (\n             EXISTS (SELECT 1 FROM storage_v2_source_segment_rank(\n                 matched.id, storage_v2_simple_and_query(p_ast)\n             )) OR (NOT EXISTS (\n                 SELECT 1 FROM storage_v2_lexical_segment segment\n                  WHERE segment.occurrence_id = matched.id\n             ) AND storage_v2_search_ast_matches(\n                 p_ast, matched_terms, matched_phrases, matched_exact\n             ))\n         ))';
    v_rank_old TEXT := E'LEFT JOIN LATERAL storage_v2_source_segment_rank(\n              staged.id, p_ast ->> ''value''\n          ) lexical_rank ON p_ast ->> ''type'' = ''term''';
    v_rank_new TEXT := E'LEFT JOIN LATERAL storage_v2_source_segment_rank(\n              staged.id, COALESCE(storage_v2_simple_and_query(p_ast), p_ast ->> ''value'')\n          ) lexical_rank ON p_ast ->> ''type'' = ''term''\n              OR storage_v2_simple_and_query(p_ast) IS NOT NULL';
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE,
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::REGPROCEDURE
    ] LOOP
        v_sql := pg_get_functiondef(v_function);
        IF strpos(v_sql, v_ast_old) = 0 OR strpos(v_sql, v_rank_old) = 0 THEN
            RAISE EXCEPTION 'storage-v2 lexical search definition changed before conjunction parity installation';
        END IF;
        v_sql := replace(v_sql, v_ast_old, v_ast_new);
        v_sql := replace(v_sql, v_rank_old, v_rank_new);
        EXECUTE v_sql;
    END LOOP;
END
$$;

-- The existing source-bound proof already checks immutable body text,
-- complete segment support, current indexed hits, path recall and order. A
-- simple conjunction uses those same checks against websearch_to_tsquery.
DO $$
DECLARE
    v_sql TEXT;
    v_old TEXT := 'p_query !~ ''^[[:alnum:]_]+$''';
    v_new TEXT := 'p_query !~ ''^[[:alnum:]_]+( [[:alnum:]_]+){0,7}$'' OR p_query ~* ''(^| )(and|or|not)( |$)''';
BEGIN
    v_sql := pg_get_functiondef(
        'storage_v2_candidate_query_evidence(bigint,bigint,text,text,bigint[],bigint[])'::REGPROCEDURE
    );
    IF strpos(v_sql, v_old) = 0
       OR strpos(v_sql, '''mainrag.storage-v2.query-coverage.v2''') = 0 THEN
        RAISE EXCEPTION 'storage-v2 query coverage definition changed before conjunction proof installation';
    END IF;
    v_sql := replace(v_sql, v_old, v_new);
    v_sql := replace(v_sql, '''mainrag.storage-v2.query-coverage.v2''',
                     '''mainrag.storage-v2.query-coverage.v3''');
    v_sql := replace(v_sql, 'bounded literal query and unique positive hit identities required',
                     'bounded simple query and unique positive hit identities required');
    EXECUTE v_sql;
END
$$;
