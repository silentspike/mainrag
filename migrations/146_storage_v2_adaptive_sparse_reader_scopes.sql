-- Preserve complete source scope and ranking while avoiding repeated large
-- requested-set expansion for at most 32 exact candidates. Dense matches retain
-- set joins. Compact fingerprints precede scope checks and exact term decoding.
BEGIN;

DO $guard$
DECLARE expected RECORD; routine RECORD;
BEGIN
    FOR expected IN SELECT * FROM (VALUES
        ('storage_v2_scoped_query_posting(bigint[],text[])','40c6eafb6f621955a31cec9d58062edad8c6ea35480c8de10f40663c2b85e957','mainrag',false,'mainrag'),
        ('storage_v2_source_segment_rank_candidates(bigint[],text)','a20f87ace69968ee4dfa94b57df44ce2b4de28a3c284666401ff74d7daaaf7f9','mainrag_v2_frontier_owner',true,'mainrag'),
        ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])','bc76ac7e8878909760e693b90c6eec22f47e5196fef51d51b60643b646a12b3c','mainrag_v2_frontier_owner',true,'mainrag'),
        ('storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)','057c63e225d88fca6c55769dab5f72722997120ae284e3746ec1d51eeeefb368','mainrag_v2_lexical_rank_owner',true,'mainrag_v2_frontier_owner'),
        ('storage_v2_reader_metadata_ready(bigint[])','709d69b40fd59f6ff7065fac9c8e43d644464f4f0547e616928707632201a065','mainrag_v2_metadata_reader',true,'mainrag')
    ) AS x(signature,sha256,owner_name,definer,caller_name) LOOP
        SELECT * INTO STRICT routine FROM pg_proc WHERE oid=expected.signature::REGPROCEDURE;
        IF encode(sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8')),'hex')<>expected.sha256
           OR routine.proowner<>expected.owner_name::REGROLE
           OR routine.prosecdef<>expected.definer
           OR routine.proisstrict IS DISTINCT FROM
                (expected.signature<>'storage_v2_reader_metadata_ready(bigint[])')
           OR routine.provolatile<>'s'
           OR NOT has_function_privilege(expected.caller_name,routine.oid,'EXECUTE')
           OR EXISTS(SELECT 1 FROM aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) a
                WHERE a.grantee NOT IN (routine.proowner,expected.caller_name::REGROLE)
                   OR a.privilege_type<>'EXECUTE'
                   OR (a.is_grantable AND a.grantee<>routine.proowner)) THEN
            RAISE EXCEPTION 'adaptive reader definition or authority differs: %',expected.signature;
        END IF;
    END LOOP;
END $guard$;

CREATE OR REPLACE FUNCTION public.storage_v2_scoped_query_posting(p_document_ids bigint[], p_terms text[])
 RETURNS TABLE(document_id bigint, term text, term_frequency bigint)
 LANGUAGE plpgsql
 STABLE STRICT
 SET search_path TO 'pg_catalog', 'public', 'pg_temp'
 SET plan_cache_mode TO 'force_custom_plan'
 SET jit TO 'off'
 SET enable_memoize TO 'off'
AS $function$
DECLARE
    v_terms TEXT[];
    v_hashes BYTEA[];
    v_min_document BIGINT;
    v_max_document BIGINT;
BEGIN
    SELECT array_agg(DISTINCT value ORDER BY value) INTO v_terms
      FROM unnest(p_terms) input(value) WHERE value IS NOT NULL;
    IF cardinality(p_document_ids)=0 OR v_terms IS NULL THEN RETURN; END IF;
    IF cardinality(p_document_ids)<=1024 THEN
        RETURN QUERY
        SELECT requested.id,posting.term,posting.term_frequency
          FROM (SELECT DISTINCT id FROM unnest(p_document_ids) input(id)) requested
          CROSS JOIN unnest(v_terms) requested_term(value)
          CROSS JOIN LATERAL public.storage_v2_document_posting(requested.id,requested_term.value) posting;
        RETURN;
    END IF;
    SELECT min(id),max(id) INTO v_min_document,v_max_document
      FROM unnest(p_document_ids) input(id);
    IF v_min_document IS NULL THEN RETURN; END IF;
    SELECT array_agg(DISTINCT public.digest(value,'sha256')) INTO v_hashes
      FROM unnest(v_terms) input(value);
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_document_ids) input(id)
    ), ordinary_candidate AS MATERIALIZED (
        -- Only fixed-width keys/frequencies are fetched before source scope.
        SELECT posting.document_id,posting.term_sha256,posting.term_frequency
          FROM public.storage_v2_search_posting posting
         WHERE posting.term_sha256=ANY(v_hashes)
           AND posting.document_id BETWEEN v_min_document AND v_max_document
    ), scoped_candidate AS MATERIALIZED (
        SELECT candidate.* FROM ordinary_candidate candidate
          WHERE CASE WHEN EXISTS(SELECT 1 FROM ordinary_candidate OFFSET 32)
                     THEN EXISTS(SELECT 1 FROM requested WHERE requested.id=candidate.document_id)
                     ELSE candidate.document_id=ANY(p_document_ids) END
    )
    SELECT candidate.document_id,posting.term,candidate.term_frequency::BIGINT
      FROM scoped_candidate candidate
      CROSS JOIN LATERAL (
          SELECT posting.term FROM public.storage_v2_search_posting posting
           WHERE posting.document_id=candidate.document_id
             AND posting.term_sha256=candidate.term_sha256 OFFSET 0
      ) posting
     WHERE posting.term=ANY(v_terms);
    -- One indexed interval absence check replaces a compact probe for every
    -- document. Bounds are necessary only: exact requested membership remains
    -- below, including sparse and duplicate IDs within the interval.
    IF NOT EXISTS(SELECT 1 FROM public.storage_v2_compact_posting_block block
                   WHERE block.document_id BETWEEN v_min_document AND v_max_document)
    THEN RETURN; END IF;
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_document_ids) input(id)
    ), matching_block AS MATERIALIZED (
        -- Materialize narrow candidate keys before joining the document scope.
        -- This prevents one fingerprint probe for every requested document.
        SELECT block.document_id,block.block_order
          FROM public.storage_v2_compact_posting_block block
         WHERE block.fingerprints && public.storage_v2_posting_fingerprints(v_terms)
           AND block.document_id BETWEEN v_min_document AND v_max_document
    ), compact_candidate AS MATERIALIZED (
        SELECT block.* FROM matching_block block
          WHERE CASE WHEN EXISTS(SELECT 1 FROM matching_block OFFSET 32)
                     THEN EXISTS(SELECT 1 FROM requested WHERE requested.id=block.document_id)
                     ELSE block.document_id=ANY(p_document_ids) END
    )
    SELECT candidate.document_id,requested_term.value,
           block.term_frequencies[position.ordinal]
      FROM compact_candidate candidate
      CROSS JOIN LATERAL (
          SELECT block.terms,block.term_frequencies
            FROM public.storage_v2_compact_posting_block block
           WHERE block.document_id=candidate.document_id
             AND block.block_order=candidate.block_order OFFSET 0
      ) block
      CROSS JOIN unnest(v_terms) requested_term(value)
      CROSS JOIN LATERAL unnest(array_positions(block.terms,requested_term.value)) position(ordinal);
END
$function$;

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
         FROM public.storage_v2_authorized_lexical_first_candidates(
             CASE WHEN coalesce(cardinality(v_matching_legacy),0)<33
                  THEN p_occurrence_ids ELSE ARRAY(SELECT id FROM requested_unprojected) END,
             ARRAY(SELECT source_id FROM requested_sources),p_query
         ) segment
    ), requested_matches AS MATERIALIZED (
        -- The authorized helper already proved membership in its requested
        -- scope. Preserve distinct identities without rebuilding the full set.
        SELECT DISTINCT segment.occurrence_id AS id FROM matching_segment segment
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
                SELECT marker.block_order
                  FROM public.storage_v2_compact_lexical_block marker
                 WHERE marker.occurrence_id=unprojected.id AND marker.block_order=0 OFFSET 0
            ) compact
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
$function$;

CREATE OR REPLACE FUNCTION public.storage_v2_source_segment_rank_candidates(p_occurrence_ids bigint[], p_query text, p_source_ids bigint[])
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

    ), ranked_payload AS MATERIALIZED (
        SELECT payload.id,payload.source_id,
               ts_rank_cd(payload.fts_vector,v_query,0)::DOUBLE PRECISION AS legacy_score
          FROM storage_v2_legacy_rank_payload payload
         WHERE payload.fts_vector @@ v_query
           AND payload.source_id=ANY(v_requested_sources)
    ), unscoped_projection AS MATERIALIZED (
        SELECT binding.occurrence_id,binding.source_id,binding.artifact_version_id,
               binding.legacy_chunk_id,payload.legacy_score
          FROM ranked_payload payload JOIN storage_v2_legacy_rank_binding binding
            ON binding.payload_id=payload.id AND binding.source_id=payload.source_id
    ), query_projection AS MATERIALIZED (
        SELECT projection.* FROM unscoped_projection projection
         WHERE CASE WHEN EXISTS(SELECT 1 FROM unscoped_projection OFFSET 32)
                    THEN EXISTS(SELECT 1 FROM requested WHERE requested.id=projection.occurrence_id)
                    ELSE projection.occurrence_id=ANY(p_occurrence_ids) END
    ), ranked_projection AS MATERIALIZED (
        SELECT DISTINCT ON (projection.occurrence_id)
               projection.occurrence_id, projection.artifact_version_id,
               (1000000.0::DOUBLE PRECISION + projection.legacy_score) AS score,
               projection.legacy_chunk_id,occurrence_row.view_id
          FROM query_projection projection
          JOIN LATERAL (SELECT row.* FROM occurrence row
                         WHERE row.id=projection.occurrence_id OFFSET 0) occurrence_row ON TRUE
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
        -- Keep scope predicates indexable. A CASE around the ID predicate
        -- previously hid a tiny canonical-document set from the planner.
        SELECT document.id FROM public.storage_v2_search_document document
         WHERE cardinality(p_occurrence_ids)>=1024
           AND cardinality(v_projection_documents)<=8192
           AND document.id=ANY(v_projection_documents)
           AND document.fts_simple@@v_query
        UNION ALL
        SELECT document.id FROM public.storage_v2_search_document document
         WHERE cardinality(p_occurrence_ids)>=1024
           AND cardinality(v_projection_documents)>8192
           AND document.fts_simple@@v_query
           AND EXISTS (SELECT 1 FROM authorized_projection authorized
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
         FROM public.storage_v2_authorized_lexical_first_candidates(
             CASE WHEN coalesce(cardinality(v_matching_legacy),0)<33
                  THEN p_occurrence_ids ELSE ARRAY(SELECT id FROM requested_unprojected) END,
             ARRAY(SELECT source_id FROM requested_sources),p_query
         ) segment
    ), requested_matches AS MATERIALIZED (
        -- The authorized helper already proved membership in its requested
        -- scope. Preserve distinct identities without rebuilding the full set.
        SELECT DISTINCT segment.occurrence_id AS id FROM matching_segment segment
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
                SELECT marker.block_order
                  FROM public.storage_v2_compact_lexical_block marker
                 WHERE marker.occurrence_id=unprojected.id AND marker.block_order=0 OFFSET 0
            ) compact
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
$function$;

CREATE OR REPLACE FUNCTION public.storage_v2_authorized_lexical_first_candidates(p_occurrence_ids bigint[], p_source_ids bigint[], p_query text)
 RETURNS TABLE(occurrence_id bigint, source_id bigint, artifact_version_id bigint, segment_order bigint, lexical_score real)
 LANGUAGE plpgsql
 STABLE STRICT SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public', 'pg_temp'
 SET row_security TO 'on'
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
DECLARE
    v_source_ids BIGINT[];
    v_has_ordinary BOOLEAN;
    v_has_compact BOOLEAN;
    v_query TSQUERY := websearch_to_tsquery('simple',p_query);
BEGIN
    IF cardinality(p_occurrence_ids)=0 OR cardinality(p_source_ids)=0 THEN
        RETURN;
    END IF;
    -- Resolve authorization before inspecting source-local physical metadata.
    SELECT array_agg(source.id ORDER BY source.id) INTO v_source_ids
      FROM public.sources source WHERE source.id=ANY(p_source_ids)
       AND public.storage_v2_can_access_source(source.id,'read');
    IF v_source_ids IS NULL THEN RETURN; END IF;
    SELECT EXISTS(SELECT 1 FROM public.storage_v2_lexical_segment segment
                   WHERE segment.source_id=ANY(v_source_ids)),
           EXISTS(SELECT 1 FROM public.storage_v2_compact_lexical_block block
                   WHERE block.source_id=ANY(v_source_ids))
      INTO v_has_ordinary,v_has_compact;
    IF NOT v_has_ordinary AND NOT v_has_compact THEN RETURN; END IF;
    IF v_has_ordinary THEN
    IF v_query::TEXT=quote_literal(lower(p_query)) AND EXISTS(
        SELECT 1 FROM public.storage_v2_ordinary_first_coverage coverage
         WHERE coverage.occurrence_id=ANY(p_occurrence_ids) AND coverage.source_id=ANY(v_source_ids)) THEN
        RETURN QUERY
        WITH requested AS MATERIALIZED (
            SELECT DISTINCT id FROM unnest(p_occurrence_ids) input(id) WHERE id IS NOT NULL
        ), covered AS MATERIALIZED (
            SELECT coverage.occurrence_id FROM public.storage_v2_ordinary_first_coverage coverage
             WHERE coverage.source_id=ANY(v_source_ids)
               AND coverage.occurrence_id=ANY(p_occurrence_ids)
        ), uncovered AS MATERIALIZED (
            SELECT requested.id FROM requested
             WHERE NOT EXISTS(SELECT 1 FROM covered WHERE covered.occurrence_id=requested.id)
        ), indexed AS (
            SELECT projection.occurrence_id,projection.source_id,projection.artifact_version_id,
                   projection.segment_order
          FROM public.storage_v2_ordinary_first_term projection
         WHERE projection.lexeme=lower(p_query) AND projection.source_id=ANY(v_source_ids)
               AND projection.occurrence_id=ANY(ARRAY(SELECT covered.occurrence_id FROM covered))
        ), fallback AS (
            SELECT segment.occurrence_id,segment.source_id,segment.artifact_version_id,
                   min(segment.segment_order) segment_order
              FROM public.storage_v2_lexical_segment segment
             WHERE EXISTS(SELECT 1 FROM uncovered)
               AND segment.source_id=ANY(v_source_ids) AND segment.fts_vector@@v_query
               AND segment.occurrence_id=ANY(ARRAY(SELECT uncovered.id FROM uncovered))
             GROUP BY segment.occurrence_id,segment.source_id,segment.artifact_version_id
        )
        SELECT indexed.*,0.0::REAL FROM indexed
        UNION ALL SELECT fallback.*,0.0::REAL FROM fallback;
    ELSE
    RETURN QUERY
    WITH authorized_sources AS MATERIALIZED (
        SELECT source.id FROM public.sources source
         WHERE source.id=ANY(v_source_ids)
           AND public.storage_v2_can_access_source(source.id,'read')
    ), requested AS MATERIALIZED (
        SELECT id FROM unnest(p_occurrence_ids) AS input(id)
    ), matching AS MATERIALIZED (
        SELECT segment.occurrence_id,segment.source_id,segment.artifact_version_id,
               min(segment.segment_order) AS segment_order
          FROM public.storage_v2_lexical_segment segment
         WHERE segment.fts_vector@@v_query
           AND segment.source_id IN (SELECT id FROM authorized_sources)
           AND segment.source_id=ANY(v_source_ids)
         GROUP BY segment.occurrence_id,segment.source_id,segment.artifact_version_id
    ), eligible AS MATERIALIZED (
        SELECT matching.* FROM matching
          -- Avoid expanding a large requested set when exact native matches are absent.
          WHERE EXISTS (SELECT 1 FROM matching)
            AND CASE WHEN EXISTS(SELECT 1 FROM matching OFFSET 32)
                     THEN EXISTS(SELECT 1 FROM requested WHERE requested.id=matching.occurrence_id)
                     ELSE matching.occurrence_id=ANY(p_occurrence_ids) END
    )
    SELECT eligible.occurrence_id,eligible.source_id,eligible.artifact_version_id,
           eligible.segment_order,0.0::REAL FROM eligible;
    END IF;
    END IF;

    IF v_has_compact THEN
        IF p_query ~ '^[[:alnum:]_]+([[:space:]]+[[:alnum:]_]+)*$'
           AND lower(p_query) !~ '(^|[[:space:]])or([[:space:]]|$)' THEN
            -- A direct fingerprint predicate can use GIN. It is only a
            -- necessary block predicate: full per-segment vectors decide.
            RETURN QUERY
            WITH matching_blocks AS MATERIALIZED (
                SELECT block.* FROM public.storage_v2_compact_lexical_block block
                 WHERE block.source_id=ANY(v_source_ids)
                   AND block.fingerprints @> public.storage_v2_posting_fingerprints(
                       tsvector_to_array(to_tsvector('simple',p_query)))
            ), requested AS MATERIALIZED (
                SELECT id FROM unnest(p_occurrence_ids) input(id)
            ), eligible_blocks AS MATERIALIZED (
                SELECT block.* FROM matching_blocks block
                 WHERE EXISTS (SELECT 1 FROM requested WHERE requested.id=block.occurrence_id)
            )
            SELECT block.occurrence_id,block.source_id,block.artifact_version_id,
                   min(item.segment_order),0.0::REAL
              FROM eligible_blocks block
              CROSS JOIN LATERAL unnest(block.segment_orders,block.fts_vectors)
                  item(segment_order,fts_vector)
             WHERE item.fts_vector@@v_query
             GROUP BY block.occurrence_id,block.source_id,block.artifact_version_id;
        ELSE
            -- OR, phrases and negation keep the complete vector predicate.
            RETURN QUERY
            WITH requested AS MATERIALIZED (
                SELECT id FROM unnest(p_occurrence_ids) input(id)
            ), eligible_blocks AS MATERIALIZED (
                SELECT block.* FROM public.storage_v2_compact_lexical_block block
                 WHERE block.source_id=ANY(v_source_ids)
                   AND EXISTS (SELECT 1 FROM requested WHERE requested.id=block.occurrence_id)
            )
            SELECT block.occurrence_id,block.source_id,block.artifact_version_id,
                   min(item.segment_order),0.0::REAL
              FROM eligible_blocks block
              CROSS JOIN LATERAL unnest(block.segment_orders,block.fts_vectors)
                  item(segment_order,fts_vector)
             WHERE item.fts_vector@@v_query
             GROUP BY block.occurrence_id,block.source_id,block.artifact_version_id;
        END IF;
    END IF;
END
$function$;

-- Every completeness count remains exact. These short index-only checks save
-- more startup work than parallel workers contribute for a single generation.
ALTER FUNCTION storage_v2_reader_metadata_ready(bigint[])
    SET max_parallel_workers_per_gather TO '0';
COMMIT;
