-- Migration 087: retain complete scoring while narrowing shared bindings and
-- planning against one precomputed lexical query.
-- Probe generated provenance only for occurrences without a legacy projection.
-- Authorization, forced RLS, immutable-body predicates and total tie order stay
-- unchanged. No source generation or reader binary is replaced.
CREATE OR REPLACE FUNCTION storage_v2_source_segment_ranks(
    p_occurrence_ids BIGINT[], p_query TEXT
) RETURNS TABLE(occurrence_id BIGINT, score REAL, segment_order BIGINT)
LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
DECLARE
    v_query TSQUERY := websearch_to_tsquery('simple',p_query);
BEGIN
    -- Keep the two provenance paths separate.  A single UNION plan makes the
    -- forced-RLS lexical scan repeat for every legacy projection row.
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    ), ranked_projection AS MATERIALIZED (
        SELECT DISTINCT ON (projection.occurrence_id)
               projection.occurrence_id, projection.artifact_version_id,
               (1000000.0 + ts_rank_cd(projection.fts_vector,v_query,0))::REAL AS score,
               projection.legacy_chunk_id
          FROM requested
          JOIN occurrence occurrence_row ON occurrence_row.id=requested.id
          JOIN authorized_source ON authorized_source.id=occurrence_row.source_id
          JOIN storage_v2_legacy_lexical_segment projection
            ON projection.occurrence_id=occurrence_row.id
           AND projection.source_id=occurrence_row.source_id
           AND projection.artifact_version_id=occurrence_row.artifact_version_id
         WHERE projection.fts_vector @@ v_query
         ORDER BY projection.occurrence_id,3 DESC,projection.legacy_chunk_id
    )
    SELECT projection.occurrence_id,projection.score,projection.legacy_chunk_id
      FROM ranked_projection projection
      JOIN occurrence occurrence_row ON occurrence_row.id=projection.occurrence_id
      JOIN artifact_version artifact ON artifact.id=projection.artifact_version_id
      JOIN storage_v2_search_view_document binding
        ON binding.view_id=occurrence_row.view_id AND binding.ordinal=0
      JOIN storage_v2_search_document document
        ON document.id=binding.document_id AND document.component_kind='node'
       AND document.node_id=artifact.content_root_node_id
     WHERE document.fts_simple @@ v_query;

    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    ), unprojected AS MATERIALIZED (
        SELECT requested.id FROM requested
         WHERE NOT EXISTS (
            SELECT 1 FROM storage_v2_legacy_lexical_segment projection
             WHERE projection.occurrence_id=requested.id
         )
    ), requested_kind AS MATERIALIZED (
        SELECT unprojected.id, EXISTS (
            SELECT 1 FROM storage_v2_lexical_segment marker
             WHERE marker.occurrence_id=unprojected.id AND marker.segment_order=0
        ) AS generated
          FROM unprojected
    ), eligible AS MATERIALIZED (
        SELECT occurrence_row.id, occurrence_row.source_id,
               occurrence_row.artifact_version_id, requested.generated
          FROM requested_kind requested
          JOIN occurrence occurrence_row ON occurrence_row.id=requested.id
          JOIN authorized_source ON authorized_source.id=occurrence_row.source_id
          JOIN artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id
          JOIN storage_v2_search_view_document binding
            ON binding.view_id=occurrence_row.view_id AND binding.ordinal=0
          JOIN storage_v2_search_document document
            ON document.id=binding.document_id AND document.component_kind='node'
           AND document.node_id=artifact.content_root_node_id
    )
    SELECT DISTINCT ON (segment.occurrence_id)
           segment.occurrence_id,
           (CASE WHEN NOT occurrence_row.generated
                   THEN 100000.0 + ts_rank_cd(segment.fts_vector,v_query)
                   ELSE LEAST(ts_rank_cd(segment.fts_vector,v_query),99999.0)
            END)::REAL,
           segment.segment_order
      FROM eligible occurrence_row
      JOIN storage_v2_lexical_segment segment
        ON segment.occurrence_id=occurrence_row.id
       AND segment.source_id=occurrence_row.source_id
       AND segment.artifact_version_id=occurrence_row.artifact_version_id
     WHERE segment.fts_vector @@ v_query
     ORDER BY segment.occurrence_id, 2 DESC, segment.segment_order;
END
$$;

ALTER FUNCTION storage_v2_source_segment_ranks(BIGINT[],TEXT) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_source_segment_ranks(BIGINT[],TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_ranks(BIGINT[],TEXT) TO mainrag;

-- All authorized views still receive their complete score. Only expensive
-- identity hydration is deferred to the exact top-k score/tie-key boundary.
-- Every boundary tie is retained before the original identity/id total order.
DO $migration$
DECLARE
    v_signature REGPROCEDURE;
    v_definition TEXT;
    v_old TEXT[] := ARRAY[
$old$SELECT visible.*, visible.id AS occurrence_id,$old$,
$old$artifact.expected_content_hash, view_row.view_digest,
               'storage-v2:' || encode(storage_v2_hash_parts(
                    'mainrag.external-hit.v1', ARRAY[
                        int8send(occurrence_row.source_id),
                        convert_to(item.item_key, 'UTF8'),
                        convert_to(artifact.expected_content_hash, 'UTF8'),
                        view_row.view_digest,
                        convert_to(occurrence_row.role, 'UTF8'),
                        int8send(occurrence_row.ordinal),
                        convert_to(occurrence_row.locator::TEXT, 'UTF8')
                    ]
               ), 'hex') AS external_hit_id$old$,
$old$    ordered AS (
        SELECT * FROM ranked
         ORDER BY final_score DESC, lexical_sort_key NULLS LAST, external_hit_id, id
         LIMIT p_limit
    ),
$old$];
    v_new TEXT[] := ARRAY[
$new$SELECT visible.id AS occurrence_id,$new$,
$new$artifact.expected_content_hash, view_row.view_digest$new$,
$new$    score_boundary AS MATERIALIZED (
        SELECT final_score,lexical_sort_key FROM (
            SELECT final_score,lexical_sort_key FROM ranked
             ORDER BY final_score DESC,lexical_sort_key NULLS LAST
             LIMIT p_limit
        ) top_scores
         ORDER BY final_score,lexical_sort_key DESC NULLS FIRST
         LIMIT 1
    ),
    bounded AS MATERIALIZED (
        SELECT ranked.* FROM ranked CROSS JOIN score_boundary boundary
         WHERE ranked.final_score>boundary.final_score
            OR (ranked.final_score=boundary.final_score AND (
                boundary.lexical_sort_key IS NULL
                OR ranked.lexical_sort_key<=boundary.lexical_sort_key
            ))
    ),
    identified AS MATERIALIZED (
        SELECT bounded.*,
               'storage-v2:' || encode(storage_v2_hash_parts(
                    'mainrag.external-hit.v1', ARRAY[
                        int8send(bounded.source_id),
                        convert_to(bounded.item_key, 'UTF8'),
                        convert_to(bounded.expected_content_hash, 'UTF8'),
                        bounded.view_digest,
                        convert_to(bounded.role, 'UTF8'),
                        int8send(bounded.ordinal),
                        convert_to(bounded.locator::TEXT, 'UTF8')
                    ]
               ), 'hex') AS external_hit_id
          FROM bounded
    ),
    ordered AS (
        SELECT * FROM identified
         ORDER BY final_score DESC,lexical_sort_key NULLS LAST,external_hit_id,id
         LIMIT p_limit
    ),
$new$];
    v_old_count INTEGER;
    v_new_count INTEGER;
    v_remaining TEXT;
    v_index INTEGER;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE,
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::REGPROCEDURE
    ] LOOP
        v_definition := pg_get_functiondef(v_signature);
        FOR v_index IN 1..array_length(v_old,1) LOOP
            v_old_count := (length(v_definition)-length(replace(v_definition,v_old[v_index],'')))/length(v_old[v_index]);
            v_remaining := replace(v_definition,v_old[v_index],'');
            v_new_count := (length(v_remaining)-length(replace(v_remaining,v_new[v_index],'')))/length(v_new[v_index]);
            IF v_old_count=0 AND v_new_count=1 THEN CONTINUE; END IF;
            IF v_old_count<>1 OR v_new_count<>0 THEN
                RAISE EXCEPTION 'storage-v2 scoped binding or identity boundary differs at %',v_index;
            END IF;
            v_definition := replace(v_definition,v_old[v_index],v_new[v_index]);
        END LOOP;
        EXECUTE v_definition;
    END LOOP;
END
$migration$;
