-- A medium source can contain tens of thousands of lexical segments. Check
-- the small registered source set once per statement rather than invoking the
-- application ACL function for every segment row under forced RLS.
DO $$
BEGIN
    IF pg_get_expr(
        (SELECT polqual FROM pg_policy
          WHERE polrelid = 'storage_v2_lexical_segment'::REGCLASS
            AND polname = 'storage_v2_lexical_segment_source'),
        'storage_v2_lexical_segment'::REGCLASS
    ) <> 'storage_v2_can_access_source(source_id, ''read''::text)'
       OR strpos(pg_get_functiondef(
           'storage_v2_source_segment_ranks(bigint[],text)'::REGPROCEDURE
       ), 'WHERE storage_v2_can_access_source(occurrence_row.source_id, ''read'')') = 0 THEN
        RAISE EXCEPTION 'storage-v2 segment authorization definition changed';
    END IF;
END
$$;

ALTER POLICY storage_v2_lexical_segment_source ON storage_v2_lexical_segment
    USING (source_id IN (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    ));

CREATE OR REPLACE FUNCTION storage_v2_source_segment_ranks(
    p_occurrence_ids BIGINT[], p_query TEXT
) RETURNS TABLE(occurrence_id BIGINT, score REAL, segment_order BIGINT)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    ), query AS (
        SELECT websearch_to_tsquery('simple', p_query) AS value
    )
    SELECT DISTINCT ON (segment.occurrence_id)
           segment.occurrence_id,
           ts_rank_cd(segment.fts_vector, query.value)::REAL AS score,
           segment.segment_order
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN authorized_source ON authorized_source.id = occurrence_row.source_id
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
     WHERE document.fts_simple @@ query.value
       AND segment.fts_vector @@ query.value
     ORDER BY segment.occurrence_id, score DESC, segment.segment_order
$$;
