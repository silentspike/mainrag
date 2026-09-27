-- Evaluate source-backed lexical ranks once for the scoped occurrence set.
-- The search functions retain their complete-view scoring and AST contracts;
-- only the repeated per-occurrence lexical lookup is replaced.
CREATE FUNCTION storage_v2_source_segment_ranks(
    p_occurrence_ids BIGINT[], p_query TEXT
) RETURNS TABLE(occurrence_id BIGINT, score REAL, segment_order BIGINT)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), query AS (
        SELECT websearch_to_tsquery('simple', p_query) AS value
    )
    SELECT DISTINCT ON (segment.occurrence_id)
           segment.occurrence_id,
           ts_rank_cd(segment.fts_vector, query.value)::REAL AS score,
           segment.segment_order
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN artifact_version artifact
        ON artifact.id = occurrence_row.artifact_version_id
      JOIN storage_v2_search_view_document binding
        ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
      JOIN storage_v2_search_document document
        ON document.id = binding.document_id
       AND document.component_kind = 'node'
       AND document.node_id = artifact.content_root_node_id
      JOIN storage_v2_lexical_segment segment
        ON segment.occurrence_id = occurrence_row.id
       AND segment.source_id = occurrence_row.source_id
       AND segment.artifact_version_id = occurrence_row.artifact_version_id
      CROSS JOIN query
     WHERE storage_v2_can_access_source(occurrence_row.source_id, 'read')
       AND document.fts_simple @@ query.value
       AND segment.fts_vector @@ query.value
     ORDER BY segment.occurrence_id, score DESC, segment.segment_order
$$;

ALTER FUNCTION storage_v2_source_segment_ranks(BIGINT[], TEXT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_source_segment_ranks(BIGINT[], TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_ranks(BIGINT[], TEXT) TO mainrag;

DO $$
DECLARE
    v_function REGPROCEDURE;
    v_sql TEXT;
    v_boolean_old TEXT := E'    boolean_matched AS (\n        SELECT * FROM matched\n         WHERE (storage_v2_simple_and_query(p_ast) IS NULL AND (\n             storage_v2_search_ast_matches(\n                 p_ast, matched_terms, matched_phrases, matched_exact\n             ) OR (p_ast ->> ''type'' = ''term'' AND EXISTS (\n                 SELECT 1 FROM storage_v2_source_segment_rank(matched.id, p_ast ->> ''value'')\n             ))\n         )) OR (storage_v2_simple_and_query(p_ast) IS NOT NULL AND (\n             EXISTS (SELECT 1 FROM storage_v2_source_segment_rank(\n                 matched.id, storage_v2_simple_and_query(p_ast)\n             )) OR (NOT storage_v2_has_lexical_segment(matched.id) AND storage_v2_search_ast_matches(\n                 p_ast, matched_terms, matched_phrases, matched_exact\n             ))\n         ))\n    ),';
    v_boolean_new TEXT := E'    lexical_ranks AS MATERIALIZED (\n        SELECT ranked.*\n          FROM storage_v2_source_segment_ranks(\n              (SELECT array_agg(id) FROM matched),\n              CASE WHEN p_ast ->> ''type'' = ''term'' THEN p_ast ->> ''value''\n                   ELSE storage_v2_simple_and_query(p_ast) END\n          ) ranked\n    ),\n    boolean_matched AS (\n        SELECT matched.*, lexical_rank.score AS segment_score,\n               lexical_rank.segment_order AS lexical_sort_key\n          FROM matched\n          LEFT JOIN lexical_ranks lexical_rank\n            ON lexical_rank.occurrence_id = matched.id\n         WHERE (storage_v2_simple_and_query(p_ast) IS NULL AND (\n             storage_v2_search_ast_matches(\n                 p_ast, matched_terms, matched_phrases, matched_exact\n             ) OR (p_ast ->> ''type'' = ''term'' AND lexical_rank.occurrence_id IS NOT NULL)\n         )) OR (storage_v2_simple_and_query(p_ast) IS NOT NULL AND (\n             lexical_rank.occurrence_id IS NOT NULL\n             OR (NOT storage_v2_has_lexical_segment(matched.id) AND storage_v2_search_ast_matches(\n                 p_ast, matched_terms, matched_phrases, matched_exact\n             ))\n         ))\n    ),';
    v_rank_old TEXT := E'COALESCE(1000000.0 + lexical_rank.score, lexical_score)\n                 + graph_score + semantic_score + rerank_score AS final_score,\n               lexical_rank.segment_order AS lexical_sort_key\n          FROM staged\n          LEFT JOIN LATERAL storage_v2_source_segment_rank(\n              staged.id, COALESCE(storage_v2_simple_and_query(p_ast), p_ast ->> ''value'')\n          ) lexical_rank ON p_ast ->> ''type'' = ''term''\n              OR storage_v2_simple_and_query(p_ast) IS NOT NULL';
    v_rank_new TEXT := E'COALESCE(1000000.0 + staged.segment_score, lexical_score)\n                 + graph_score + semantic_score + rerank_score AS final_score\n          FROM staged';
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE,
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::REGPROCEDURE
    ] LOOP
        v_sql := pg_get_functiondef(v_function);
        IF strpos(v_sql, v_boolean_old) = 0 OR strpos(v_sql, v_rank_old) = 0
           OR strpos(v_sql, 'lexical_ranks AS MATERIALIZED') <> 0 THEN
            RAISE EXCEPTION 'storage-v2 lexical search definition changed before set ranking installation';
        END IF;
        v_sql := replace(v_sql, v_boolean_old, v_boolean_new);
        v_sql := replace(v_sql, v_rank_old, v_rank_new);
        EXECUTE v_sql;
    END LOOP;
END
$$;
