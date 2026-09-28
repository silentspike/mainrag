-- Migration 084: materialize generated/copied provenance once per requested
-- occurrence before joining its segments. The previous late marker LEFT JOIN
-- could compare every matching segment against the complete marker CTE. Rank,
-- ordering, immutable-document matching and explicit authorization are retained.
-- Keep FORCE RLS and its write check. The read policy evaluates the same ACL
-- over the visible source set once, rather than invoking it for every segment.
ALTER POLICY storage_v2_lexical_segment_source ON storage_v2_lexical_segment
    USING (source_id IN (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id,'read')
    ));

CREATE OR REPLACE FUNCTION storage_v2_source_segment_ranks(
    p_occurrence_ids BIGINT[], p_query TEXT
) RETURNS TABLE(occurrence_id BIGINT, score REAL, segment_order BIGINT)
LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
BEGIN
    -- Keep the two provenance paths separate.  A single UNION plan makes the
    -- forced-RLS lexical scan repeat for every legacy projection row.
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    ), query AS (
        SELECT websearch_to_tsquery('simple', p_query) AS value
    )
    SELECT DISTINCT ON (projection.occurrence_id)
           projection.occurrence_id,
           (1000000.0 + ts_rank_cd(projection.fts_vector, query.value, 0))::REAL,
           projection.legacy_chunk_id
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN authorized_source
        ON authorized_source.id = occurrence_row.source_id
      JOIN artifact_version artifact
        ON artifact.id = occurrence_row.artifact_version_id
      JOIN storage_v2_search_view_document binding
        ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
      JOIN storage_v2_search_document document
        ON document.id = binding.document_id
       AND document.component_kind = 'node'
       AND document.node_id = artifact.content_root_node_id
      JOIN storage_v2_legacy_lexical_segment projection
        ON projection.occurrence_id = occurrence_row.id
       AND projection.source_id = occurrence_row.source_id
       AND projection.artifact_version_id = occurrence_row.artifact_version_id
      CROSS JOIN query
     WHERE document.fts_simple @@ query.value
       AND projection.fts_vector @@ query.value
     ORDER BY projection.occurrence_id, 2 DESC, projection.legacy_chunk_id;

    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    ), query AS (
        SELECT websearch_to_tsquery('simple', p_query) AS value
    ), requested_kind AS MATERIALIZED (
        SELECT requested.id, EXISTS (
            SELECT 1 FROM storage_v2_lexical_segment marker
             WHERE marker.occurrence_id=requested.id AND marker.segment_order=0
        ) AS generated
          FROM requested
    )
    SELECT DISTINCT ON (segment.occurrence_id)
           segment.occurrence_id,
           (CASE WHEN NOT requested.generated
                   THEN 100000.0 + ts_rank_cd(segment.fts_vector, query.value)
                   ELSE LEAST(ts_rank_cd(segment.fts_vector, query.value), 99999.0)
            END)::REAL,
           segment.segment_order
      FROM requested_kind requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN authorized_source
        ON authorized_source.id = occurrence_row.source_id
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
     WHERE segment.fts_vector @@ query.value
       AND NOT EXISTS (
           SELECT 1
             FROM storage_v2_legacy_lexical_segment projection
            WHERE projection.occurrence_id = segment.occurrence_id
       )
     ORDER BY segment.occurrence_id, 2 DESC, segment.segment_order;
END
$$;

ALTER FUNCTION storage_v2_source_segment_ranks(BIGINT[],TEXT) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_source_segment_ranks(BIGINT[],TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_ranks(BIGINT[],TEXT) TO mainrag;
