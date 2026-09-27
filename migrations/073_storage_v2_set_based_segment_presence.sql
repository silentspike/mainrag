-- Conjunctive fallback must distinguish an old occurrence with no lexical
-- projection from a projected occurrence whose segments do not match. Gather
-- presence once for the scoped set; a per-occurrence helper call would repeat
-- the authorized-source RLS subplan for every candidate row.
CREATE FUNCTION storage_v2_source_segment_presence(
    p_occurrence_ids BIGINT[]
) RETURNS TABLE(occurrence_id BIGINT)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    )
    SELECT DISTINCT segment.occurrence_id
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN authorized_source ON authorized_source.id = occurrence_row.source_id
      JOIN storage_v2_lexical_segment segment
        ON segment.occurrence_id = occurrence_row.id
       AND segment.source_id = occurrence_row.source_id
       AND segment.artifact_version_id = occurrence_row.artifact_version_id
$$;

ALTER FUNCTION storage_v2_source_segment_presence(BIGINT[]) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_source_segment_presence(BIGINT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_presence(BIGINT[]) TO mainrag;

DO $$
DECLARE
    v_function REGPROCEDURE;
    v_sql TEXT;
    v_boolean_old TEXT := '    boolean_matched AS (';
    v_boolean_new TEXT := E'    lexical_presence AS MATERIALIZED (\n        SELECT present.occurrence_id\n          FROM storage_v2_source_segment_presence(\n              CASE WHEN storage_v2_simple_and_query(p_ast) IS NOT NULL\n                   THEN (SELECT array_agg(id) FROM matched)\n                   ELSE NULL::BIGINT[] END\n          ) present\n    ),\n    boolean_matched AS (';
    v_join_old TEXT := E'          LEFT JOIN lexical_ranks lexical_rank\n            ON lexical_rank.occurrence_id = matched.id';
    v_join_new TEXT := E'          LEFT JOIN lexical_ranks lexical_rank\n            ON lexical_rank.occurrence_id = matched.id\n          LEFT JOIN lexical_presence presence\n            ON presence.occurrence_id = matched.id';
    v_presence_old TEXT := 'NOT storage_v2_has_lexical_segment(matched.id)';
    v_presence_new TEXT := 'presence.occurrence_id IS NULL';
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE,
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::REGPROCEDURE
    ] LOOP
        v_sql := pg_get_functiondef(v_function);
        IF strpos(v_sql, v_boolean_old) = 0 OR strpos(v_sql, v_join_old) = 0
           OR strpos(v_sql, v_presence_old) = 0
           OR strpos(v_sql, 'lexical_presence AS MATERIALIZED') <> 0 THEN
            RAISE EXCEPTION 'storage-v2 conjunctive presence definition changed';
        END IF;
        v_sql := replace(v_sql, v_boolean_old, v_boolean_new);
        v_sql := replace(v_sql, v_join_old, v_join_new);
        v_sql := replace(v_sql, v_presence_old, v_presence_new);
        EXECUTE v_sql;
    END LOOP;
END
$$;
