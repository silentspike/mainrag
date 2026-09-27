-- Copied legacy chunks retain their installed FTS vectors and chunk-id sort
-- keys. Put those source-backed copies ahead of newly segmented matches so a
-- new candidate-only hit cannot displace a matching legacy Top-10 path.
-- Generated segment sets always start at order zero; copied chunk IDs are
-- positive. This provenance marker is immutable and survives legacy cleanup.
DO $$
DECLARE
    v_sql TEXT := pg_get_functiondef(
        'storage_v2_source_segment_ranks(bigint[],text)'::REGPROCEDURE
    );
    v_cte_old TEXT := E'    ), query AS (\n        SELECT websearch_to_tsquery(''simple'', p_query) AS value\n    )';
    v_cte_new TEXT := E'    ), generated AS MATERIALIZED (\n        SELECT marker.occurrence_id\n          FROM requested\n          JOIN storage_v2_lexical_segment marker\n            ON marker.occurrence_id = requested.id\n           AND marker.segment_order = 0\n    ), query AS (\n        SELECT websearch_to_tsquery(''simple'', p_query) AS value\n    )';
    v_score_old TEXT := 'ts_rank_cd(segment.fts_vector, query.value)::REAL AS score,';
    v_score_new TEXT := E'(CASE WHEN generated.occurrence_id IS NULL\n                    THEN 100000.0 + ts_rank_cd(segment.fts_vector, query.value)\n                    ELSE LEAST(ts_rank_cd(segment.fts_vector, query.value), 99999.0)\n               END)::REAL AS score,';
    v_join_old TEXT := E'      CROSS JOIN query\n     WHERE segment.fts_vector @@ query.value';
    v_join_new TEXT := E'      LEFT JOIN generated\n        ON generated.occurrence_id = segment.occurrence_id\n      CROSS JOIN query\n     WHERE segment.fts_vector @@ query.value';
BEGIN
    IF strpos(v_sql, v_cte_old) = 0 OR strpos(v_sql, v_score_old) = 0
       OR strpos(v_sql, v_join_old) = 0
       OR strpos(v_sql, 'generated AS MATERIALIZED') <> 0 THEN
        RAISE EXCEPTION 'source segment ranking differs before copied-segment tier';
    END IF;
    v_sql := replace(v_sql, v_cte_old, v_cte_new);
    v_sql := replace(v_sql, v_score_old, v_score_new);
    EXECUTE replace(v_sql, v_join_old, v_join_new);
END
$$;
