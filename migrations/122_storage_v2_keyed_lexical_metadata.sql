-- Probe lexical metadata by requested occurrence; never expand unrelated vectors.
-- Keep original authorization, source/artifact provenance, owners and grants.
BEGIN;
DO $guard$ BEGIN
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::regprocedure),'UTF8')),'hex')
      NOT IN ('b9d742e3d3a5676aa1c2f9a7daccb71100327e7c1f08bbd8c244cf984335077f','a64cf3e85ef5f9b823ce1ed83c8354906f575d0f00344aa9510970723ba6b75c') THEN
  RAISE EXCEPTION 'scoped lexical metadata identity differs';
 END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::regprocedure)
      <> 'mainrag_v2_frontier_owner'::regrole
    OR NOT has_function_privilege('mainrag','storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])','EXECUTE')
    OR EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
      aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission
      WHERE routine.oid='storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::regprocedure
        AND (permission.grantee NOT IN ('mainrag_v2_frontier_owner'::regrole,'mainrag'::regrole)
          OR permission.privilege_type<>'EXECUTE'
          OR (permission.is_grantable AND permission.grantee<>routine.proowner))) THEN
  RAISE EXCEPTION 'scoped lexical metadata authority differs';
 END IF;
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_source_segment_presence(bigint[])'::regprocedure),'UTF8')),'hex')
      NOT IN ('c239dd178726a92b2a127a9d54c14e5921afc9d57c6643dfa94a453333008f99','ca0d52dcbdd350c0ed8b91a6a630a618d4031ca30be0b4b91ee47ba03f1de207') THEN
  RAISE EXCEPTION 'scoped lexical metadata identity differs';
 END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_source_segment_presence(bigint[])'::regprocedure)
      <> 'mainrag_v2_frontier_owner'::regrole
    OR NOT has_function_privilege('mainrag','storage_v2_source_segment_presence(bigint[])','EXECUTE')
    OR EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
      aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission
      WHERE routine.oid='storage_v2_source_segment_presence(bigint[])'::regprocedure
        AND (permission.grantee NOT IN ('mainrag_v2_frontier_owner'::regrole,'mainrag'::regrole)
          OR permission.privilege_type<>'EXECUTE'
          OR (permission.is_grantable AND permission.grantee<>routine.proowner))) THEN
  RAISE EXCEPTION 'scoped lexical metadata authority differs';
 END IF;
END $guard$;
DO $replace$ BEGIN
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::regprocedure),'UTF8')),'hex')='b9d742e3d3a5676aa1c2f9a7daccb71100327e7c1f08bbd8c244cf984335077f' THEN
  EXECUTE $definition$CREATE OR REPLACE FUNCTION public.storage_v2_source_segment_rank_candidates(p_occurrence_ids bigint[], p_query text, p_source_ids bigint[])
 RETURNS TABLE(occurrence_id bigint, score double precision, segment_order bigint)
 LANGUAGE plpgsql
 STABLE STRICT SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
 SET row_security TO 'on'
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
DECLARE
    v_query TSQUERY := websearch_to_tsquery('simple',p_query);
    v_requested_sources BIGINT[];
    v_projection_occurrences BIGINT[];
    v_projection_artifacts BIGINT[];
    v_projection_scores DOUBLE PRECISION[];
    v_projection_chunks BIGINT[];
    v_projection_views BIGINT[];
    v_projection_documents BIGINT[];
    v_matching_legacy BIGINT[];
BEGIN
    -- The reader already resolved its complete authorized source scope. Hints
    -- restrict work only; every emitted row still proves requested identity,
    -- source access, artifact identity and canonical document provenance.
    SELECT array_agg(source.id ORDER BY source.id) INTO v_requested_sources
      FROM sources source WHERE source.id=ANY(p_source_ids)
       AND storage_v2_can_access_source(source.id,'read');
    WITH requested AS MATERIALIZED (
        SELECT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE source.id=ANY(v_requested_sources)
           AND storage_v2_can_access_source(source.id, 'read')
    ), requested_sources AS MATERIALIZED (
        SELECT source_id FROM unnest(v_requested_sources) input(source_id)
    ), query_projection AS MATERIALIZED (
        SELECT projection.occurrence_id,projection.source_id,projection.artifact_version_id,
               projection.legacy_chunk_id,
               ts_rank_cd(projection.fts_vector,v_query,0)::DOUBLE PRECISION AS legacy_score
          FROM storage_v2_legacy_lexical_segment projection
         WHERE projection.fts_vector @@ v_query
           AND projection.source_id=ANY(v_requested_sources)
    ), ranked_projection AS MATERIALIZED (
        SELECT DISTINCT ON (projection.occurrence_id)
               projection.occurrence_id, projection.artifact_version_id,
               (1000000.0::DOUBLE PRECISION + projection.legacy_score) AS score,
               projection.legacy_chunk_id,occurrence_row.view_id
          FROM requested
          JOIN occurrence occurrence_row ON occurrence_row.id=requested.id
          JOIN query_projection projection
            ON projection.occurrence_id=occurrence_row.id
         -- Hints restrict sources; requested rows still bind actual provenance.
         WHERE CASE WHEN occurrence_row.source_id=ANY(v_requested_sources)
                     AND projection.source_id=occurrence_row.source_id
                     AND projection.artifact_version_id=occurrence_row.artifact_version_id
                    THEN TRUE ELSE FALSE END

         ORDER BY projection.occurrence_id,3 DESC,projection.legacy_chunk_id
    ), authorized_projection AS MATERIALIZED (
        SELECT projection.*, document.id AS canonical_document_id
          FROM ranked_projection projection
          JOIN artifact_version artifact ON artifact.id=projection.artifact_version_id
          JOIN storage_v2_search_view_document binding
            ON binding.view_id=projection.view_id AND binding.ordinal=0
          JOIN storage_v2_search_document document ON document.id=binding.document_id
         WHERE CASE WHEN document.component_kind='node'
                     AND document.node_id=artifact.content_root_node_id
                    THEN TRUE ELSE FALSE END
    )
    SELECT array_agg(projection.occurrence_id),array_agg(projection.artifact_version_id),
           array_agg(projection.score),array_agg(projection.legacy_chunk_id),array_agg(projection.view_id),
           array_agg(projection.canonical_document_id),
           ARRAY(SELECT DISTINCT matching.occurrence_id FROM query_projection matching)
      INTO v_projection_occurrences,v_projection_artifacts,v_projection_scores,
           v_projection_chunks,v_projection_views,v_projection_documents,v_matching_legacy
      FROM authorized_projection projection;

    -- Typed bounded IDs expose actual matching cardinality to custom planning.
    -- Canonical metadata and both provenance fences remain independently checked.
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT id FROM unnest(p_occurrence_ids) input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE source.id=ANY(v_requested_sources)
           AND storage_v2_can_access_source(source.id,'read')
    ), requested_sources AS MATERIALIZED (
        SELECT source_id FROM unnest(v_requested_sources) input(source_id)
    ), ranked_projection AS MATERIALIZED (
        SELECT * FROM unnest(v_projection_occurrences,v_projection_artifacts,
            v_projection_scores,v_projection_chunks,v_projection_views,v_projection_documents)
            projection(occurrence_id,artifact_version_id,score,legacy_chunk_id,view_id,canonical_document_id)
    )
, authorized_projection AS MATERIALIZED (
        -- Canonical source, artifact, view and node provenance was checked
        -- before capturing these parallel arrays in the same stable snapshot.
        SELECT * FROM ranked_projection
    ), matching_document AS MATERIALIZED (
        SELECT document.id FROM public.storage_v2_search_document document
         WHERE cardinality(p_occurrence_ids)>=1024
           AND document.fts_simple@@v_query
           AND CASE WHEN cardinality(v_projection_documents)<=8192
                    THEN document.id=ANY(v_projection_documents)
                    ELSE EXISTS (SELECT 1 FROM authorized_projection authorized
                                  WHERE authorized.canonical_document_id=document.id) END
    ), copied_result AS MATERIALIZED (
    SELECT projection.occurrence_id,projection.score,projection.legacy_chunk_id
      FROM authorized_projection projection
     WHERE CASE WHEN cardinality(p_occurrence_ids)>=1024
                    THEN EXISTS (SELECT 1 FROM matching_document matched
                                  WHERE matched.id=projection.canonical_document_id)
                    ELSE EXISTS (SELECT 1 FROM public.storage_v2_search_document document WHERE document.id=projection.canonical_document_id AND document.fts_simple@@v_query) END
        OR storage_v2_source_legacy_segment_matches(projection.occurrence_id,p_query)
    ), matching_legacy AS MATERIALIZED (
        SELECT projection.occurrence_id FROM unnest(v_matching_legacy) projection(occurrence_id)
    ), requested_unprojected AS MATERIALIZED (
        SELECT requested.id FROM requested
          WHERE NOT EXISTS (SELECT 1 FROM matching_legacy projection
                             WHERE projection.occurrence_id=requested.id)
    ), matching_segment AS MATERIALIZED (
        SELECT segment.*
         FROM public.storage_v2_authorized_lexical_candidates(
             ARRAY(SELECT id FROM requested_unprojected),
             ARRAY(SELECT source_id FROM requested_sources),p_query
         ) segment
    ), requested_matches AS MATERIALIZED (
        SELECT requested.id
          FROM requested
          JOIN (SELECT DISTINCT segment.occurrence_id FROM matching_segment segment) matched
            ON matched.occurrence_id=requested.id
    ), unprojected AS MATERIALIZED (
        SELECT matched.id FROM requested_matches matched
          LEFT JOIN matching_legacy projection ON projection.occurrence_id=matched.id
         WHERE projection.occurrence_id IS NULL
    ), requested_kind AS MATERIALIZED (
        SELECT unprojected.id, EXISTS (
            SELECT 1 FROM (
                SELECT marker.segment_order
                  FROM public.storage_v2_lexical_segment marker
                 WHERE marker.occurrence_id=unprojected.id AND marker.segment_order=0 OFFSET 0
            ) ordinary
            UNION ALL
            SELECT 1 FROM (
                SELECT marker.segment_orders
                  FROM public.storage_v2_compact_lexical_block marker
                 WHERE marker.occurrence_id=unprojected.id OFFSET 0
            ) compact
             WHERE 0=ANY(compact.segment_orders)
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
    ), native_result AS MATERIALIZED (
    SELECT DISTINCT ON (segment.occurrence_id)
           segment.occurrence_id,
           (CASE WHEN NOT occurrence_row.generated
                   THEN 100000.0 + segment.lexical_score
                   ELSE LEAST(segment.lexical_score,99999.0)
            END)::DOUBLE PRECISION,
           segment.segment_order
      FROM eligible occurrence_row
      JOIN matching_segment segment
        ON segment.occurrence_id=occurrence_row.id
       AND segment.source_id=occurrence_row.source_id
       AND segment.artifact_version_id=occurrence_row.artifact_version_id
     -- Matching lexical inputs already proved the query predicate.
     ORDER BY segment.occurrence_id, 2 DESC, segment.segment_order
    )
    SELECT * FROM copied_result UNION ALL SELECT * FROM native_result;
END
$function$
$definition$;
 END IF;
END $replace$;
DO $replace$ BEGIN
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_source_segment_presence(bigint[])'::regprocedure),'UTF8')),'hex')='c239dd178726a92b2a127a9d54c14e5921afc9d57c6643dfa94a453333008f99' THEN
  EXECUTE $definition$CREATE OR REPLACE FUNCTION public.storage_v2_source_segment_presence(p_occurrence_ids bigint[])
 RETURNS TABLE(occurrence_id bigint)
 LANGUAGE plpgsql
 STABLE STRICT SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
 SET row_security TO 'on'
AS $function$
BEGIN
    -- One authorized requested item yields one presence row. Presence does not
    -- need to enumerate all of its immutable lexical segments.
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    )
    SELECT occurrence_row.id
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN authorized_source ON authorized_source.id = occurrence_row.source_id
     WHERE EXISTS (
               SELECT 1 FROM storage_v2_legacy_lexical_segment projection
                WHERE projection.occurrence_id = occurrence_row.id
           ) OR EXISTS (
               SELECT 1 FROM (
                   SELECT segment.source_id, segment.artifact_version_id
                     FROM public.storage_v2_lexical_segment segment
                    WHERE segment.occurrence_id = occurrence_row.id OFFSET 0
               ) segment
                WHERE segment.source_id = occurrence_row.source_id
                  AND segment.artifact_version_id = occurrence_row.artifact_version_id
           ) OR EXISTS (
               SELECT 1 FROM (
                   SELECT segment.source_id, segment.artifact_version_id, segment.segment_orders
                     FROM public.storage_v2_compact_lexical_block segment
                    WHERE segment.occurrence_id = occurrence_row.id OFFSET 0
               ) segment
                WHERE segment.source_id = occurrence_row.source_id
                  AND segment.artifact_version_id = occurrence_row.artifact_version_id
                  AND cardinality(segment.segment_orders)>0
           );
END
$function$
$definition$;
 END IF;
END $replace$;
COMMIT;
