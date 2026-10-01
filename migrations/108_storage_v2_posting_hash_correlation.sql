-- Preserve dependent hash checks and canonical query sets without repeated probes.
BEGIN;
DO $guard$ BEGIN
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_scoped_term_posting(bigint[],text)'::regprocedure),'UTF8')),'hex') NOT IN ('40fe652f17bbf8793eb7d82f0c343f9c105cccdcbe7a71ab4861e0179df489e0','bdb55c72f3d33a34219ae93c468a25697cde4a37fa2e78442ed6860c45237f2b') THEN RAISE EXCEPTION 'candidate helper identity differs'; END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_scoped_term_posting(bigint[],text)'::regprocedure)<>'mainrag'::regrole THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;
 IF EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission WHERE routine.oid='storage_v2_scoped_term_posting(bigint[],text)'::regprocedure AND (permission.grantee NOT IN ('mainrag'::regrole) OR permission.privilege_type<>'EXECUTE' OR (permission.is_grantable AND permission.grantee<>routine.proowner))) THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_source_segment_rank_candidates(bigint[],text)'::regprocedure),'UTF8')),'hex') NOT IN ('089974dc76f56c2acc1ede0a0d2dfeee17724fd6fbb14e1046ebeca6529d0ae8','e1c5046f3a347135454f6bb980330649212bbf27c47090e2e24951e47668b42e') THEN RAISE EXCEPTION 'candidate helper identity differs'; END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_source_segment_rank_candidates(bigint[],text)'::regprocedure)<>'mainrag_v2_frontier_owner'::regrole THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;
 IF EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission WHERE routine.oid='storage_v2_source_segment_rank_candidates(bigint[],text)'::regprocedure AND (permission.grantee NOT IN ('mainrag_v2_frontier_owner'::regrole,'mainrag'::regrole) OR permission.privilege_type<>'EXECUTE' OR (permission.is_grantable AND permission.grantee<>routine.proowner))) THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;

 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::regprocedure),'UTF8')),'hex')<>'30663056271974a8706256b0d101aff706a01f43bc647b129681f60d336f7936' THEN RAISE EXCEPTION 'candidate helper identity differs'; END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::regprocedure)<>'mainrag'::regrole THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::regprocedure),'UTF8')),'hex')<>'c5a65e09acdd3def038f71f96caab6a5d87330c0dd445b3216c3d0b4c82d05f2' THEN RAISE EXCEPTION 'candidate helper identity differs'; END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::regprocedure)<>'mainrag'::regrole THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;
END $guard$;
DO $index_guard$ DECLARE definition TEXT; valid BOOLEAN; BEGIN
 SELECT pg_get_indexdef(indexrelid),indisvalid AND indisready INTO definition,valid
 FROM pg_index WHERE indexrelid='public.idx_storage_v2_fragmented_artifact_occurrence'::REGCLASS;
 IF definition <> 'CREATE INDEX idx_storage_v2_fragmented_artifact_occurrence ON public.occurrence USING btree (id) WHERE ((role = ''artifact''::text) AND (locator @> ''{"fragmented": true}''::jsonb))'
    OR NOT valid THEN RAISE EXCEPTION 'fragmented artifact occurrence index identity differs'; END IF;
END $index_guard$;

CREATE OR REPLACE FUNCTION public.storage_v2_scoped_term_posting(p_document_ids bigint[], p_term text)
 RETURNS TABLE(document_id bigint, term text, term_frequency bigint)
 LANGUAGE plpgsql
 STABLE STRICT
 SET search_path TO 'pg_catalog', 'public', 'pg_temp'
 SET plan_cache_mode TO 'force_custom_plan'
 SET enable_nestloop TO 'off'
 SET jit TO 'off'
AS $function$
DECLARE
    v_document_estimate DOUBLE PRECISION;
BEGIN
    SELECT GREATEST(reltuples,1) INTO v_document_estimate FROM pg_class
     WHERE oid='public.storage_v2_search_document'::REGCLASS;
    -- Large corpus fractions use sets; small fractions use exact keyed probes.
    -- Both ordinary and compact paths retain exact IDs and terms.
    IF cardinality(p_document_ids)>4096
       AND cardinality(p_document_ids)>v_document_estimate*0.10 THEN
        RETURN QUERY
        WITH requested AS MATERIALIZED (
            SELECT id FROM unnest(p_document_ids) input(id)
        )
        SELECT posting.document_id,posting.term,posting.term_frequency::BIGINT
          FROM (
              SELECT probe.document_id,probe.term,probe.term_frequency
                FROM public.storage_v2_search_posting probe
               WHERE probe.term=p_term
                 AND CASE WHEN probe.term_sha256=public.digest(p_term,'sha256')
                          THEN TRUE ELSE FALSE END OFFSET 0
          ) posting
         WHERE EXISTS (SELECT 1 FROM requested WHERE requested.id=posting.document_id)
        UNION ALL
        SELECT block.document_id,item.term,item.frequency
          FROM public.storage_v2_compact_posting_block block
          CROSS JOIN LATERAL unnest(block.terms,block.term_frequencies) item(term,frequency)
         WHERE block.fingerprints @> ARRAY[public.storage_v2_posting_fingerprint(p_term)]
           AND item.term=p_term
           AND EXISTS (SELECT 1 FROM requested WHERE requested.id=block.document_id);
    ELSE
        RETURN QUERY
        SELECT requested.id,posting.term,posting.term_frequency
          FROM (SELECT DISTINCT id FROM unnest(p_document_ids) input(id)) requested
          CROSS JOIN LATERAL (
              SELECT probe.term,probe.term_frequency
                FROM public.storage_v2_document_posting(requested.id,p_term) probe OFFSET 0
          ) posting;
    END IF;
END
$function$
;
CREATE OR REPLACE FUNCTION public.storage_v2_source_segment_rank_candidates(p_occurrence_ids bigint[], p_query text)
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
    v_matching_legacy BIGINT[];
BEGIN
    WITH requested AS MATERIALIZED (
        SELECT id FROM unnest(p_occurrence_ids) input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id,'read')
    )
    SELECT array_agg(requested_source.source_id ORDER BY requested_source.source_id)
      INTO v_requested_sources FROM (
        SELECT occurrence_row.source_id FROM requested
          JOIN occurrence occurrence_row ON occurrence_row.id=requested.id
          JOIN authorized_source ON authorized_source.id=occurrence_row.source_id
         GROUP BY occurrence_row.source_id
      ) requested_source;
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
         -- v_requested_sources was populated only from authorized requested rows.
         WHERE CASE WHEN occurrence_row.source_id=ANY(v_requested_sources)
                     AND projection.source_id=occurrence_row.source_id
                     AND projection.artifact_version_id=occurrence_row.artifact_version_id
                    THEN TRUE ELSE FALSE END

         ORDER BY projection.occurrence_id,3 DESC,projection.legacy_chunk_id
    )
    SELECT array_agg(projection.occurrence_id),array_agg(projection.artifact_version_id),
           array_agg(projection.score),array_agg(projection.legacy_chunk_id),array_agg(projection.view_id),
           ARRAY(SELECT DISTINCT matching.occurrence_id FROM query_projection matching)
      INTO v_projection_occurrences,v_projection_artifacts,v_projection_scores,
           v_projection_chunks,v_projection_views,v_matching_legacy
      FROM ranked_projection projection;

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
            v_projection_scores,v_projection_chunks,v_projection_views)
            projection(occurrence_id,artifact_version_id,score,legacy_chunk_id,view_id)
    )
, authorized_projection AS MATERIALIZED (
        SELECT projection.*, document.id AS canonical_document_id
          FROM ranked_projection projection
          JOIN artifact_version artifact ON artifact.id=projection.artifact_version_id
          JOIN storage_v2_search_view_document binding
            ON binding.view_id=projection.view_id AND binding.ordinal=0
          JOIN storage_v2_search_document document
            ON document.id=binding.document_id
         WHERE CASE WHEN document.component_kind='node'
                     AND document.node_id=artifact.content_root_node_id
                    THEN TRUE ELSE FALSE END
    ), matching_document AS MATERIALIZED (
        SELECT document.id FROM (
            SELECT candidate.id FROM public.storage_v2_search_document candidate
             WHERE cardinality(p_occurrence_ids)>=1024
               AND candidate.fts_simple@@v_query OFFSET 0
        ) document
         WHERE EXISTS (SELECT 1 FROM authorized_projection authorized
                        WHERE authorized.canonical_document_id=document.id)
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
            SELECT 1 FROM public.storage_v2_lexical_segment_all marker
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
;
COMMIT;
