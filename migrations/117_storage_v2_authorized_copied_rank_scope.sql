-- Bound copied lexical evidence and defer identity loads to selected results.
-- Source hints only restrict work: requested IDs, ACLs and canonical provenance
-- are independently verified. No generation, pointer or document is changed.
BEGIN;
DO $guard$ BEGIN
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_source_segment_rank_candidates(bigint[],text)'::regprocedure),'UTF8')),'hex') <>'e1c5046f3a347135454f6bb980330649212bbf27c47090e2e24951e47668b42e' THEN RAISE EXCEPTION 'candidate helper identity differs'; END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_source_segment_rank_candidates(bigint[],text)'::regprocedure)<>'mainrag_v2_frontier_owner'::regrole THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;
 IF EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission WHERE routine.oid='storage_v2_source_segment_rank_candidates(bigint[],text)'::regprocedure AND (permission.grantee NOT IN ('mainrag_v2_frontier_owner'::regrole,'mainrag'::regrole) OR permission.privilege_type<>'EXECUTE' OR (permission.is_grantable AND permission.grantee<>routine.proowner))) THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::regprocedure),'UTF8')),'hex') NOT IN ('30663056271974a8706256b0d101aff706a01f43bc647b129681f60d336f7936','6fdcdc0304c64fd58504a691cab9ed1eb5588573ab1b1db7f085597d67b85a3d') THEN RAISE EXCEPTION 'candidate helper identity differs'; END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::regprocedure)<>'mainrag'::regrole THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::regprocedure),'UTF8')),'hex') NOT IN ('c5a65e09acdd3def038f71f96caab6a5d87330c0dd445b3216c3d0b4c82d05f2','2e220670132d76e12bfed23aa935fcb5d5ab2a5659ed04cf9ce34542196874e3') THEN RAISE EXCEPTION 'candidate helper identity differs'; END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::regprocedure)<>'mainrag'::regrole THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;
 IF to_regprocedure('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])') IS NOT NULL THEN
  IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::regprocedure),'UTF8')),'hex')<>'b9d742e3d3a5676aa1c2f9a7daccb71100327e7c1f08bbd8c244cf984335077f' THEN RAISE EXCEPTION 'candidate helper identity differs'; END IF;
  IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::regprocedure)<>'mainrag_v2_frontier_owner'::regrole THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;
  IF NOT has_function_privilege('mainrag','storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])','EXECUTE') OR EXISTS (
   SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission
    WHERE routine.oid='storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::regprocedure
      AND (permission.grantee NOT IN ('mainrag_v2_frontier_owner'::regrole,'mainrag'::regrole)
           OR permission.privilege_type<>'EXECUTE'
           OR (permission.is_grantable AND permission.grantee<>routine.proowner)))
   THEN RAISE EXCEPTION 'candidate helper authority differs'; END IF;
 END IF;
END $guard$;
DO $index_guard$ DECLARE definition TEXT; valid BOOLEAN; BEGIN
 SELECT pg_get_indexdef(indexrelid),indisvalid AND indisready INTO definition,valid
 FROM pg_index WHERE indexrelid='public.idx_storage_v2_fragmented_artifact_occurrence'::REGCLASS;
 IF definition <> 'CREATE INDEX idx_storage_v2_fragmented_artifact_occurrence ON public.occurrence USING btree (id) WHERE ((role = ''artifact''::text) AND (locator @> ''{"fragmented": true}''::jsonb))'
    OR NOT valid THEN RAISE EXCEPTION 'fragmented artifact occurrence index identity differs'; END IF;
END $index_guard$;

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
ALTER FUNCTION storage_v2_source_segment_rank_candidates(BIGINT[],TEXT,BIGINT[]) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_source_segment_rank_candidates(BIGINT[],TEXT,BIGINT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_rank_candidates(BIGINT[],TEXT,BIGINT[]) TO mainrag;

CREATE OR REPLACE FUNCTION public.storage_v2_search_exact(p_source_id bigint, p_generation_selector text, p_ast jsonb, p_filters jsonb DEFAULT '{}'::jsonb, p_limit bigint DEFAULT 20)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
 SET row_security TO 'off'
 SET plan_cache_mode TO 'force_custom_plan'
 SET jit TO 'off'
AS $function$
DECLARE
    v_generation source_generation;
    v_result JSONB;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'read') THEN
        RAISE EXCEPTION 'authorized generation selector required' USING ERRCODE = '42501';
    END IF;
    IF p_ast IS NULL OR NOT storage_v2_search_ast_is_valid(p_ast)
       OR NOT storage_v2_search_ast_has_anchor(p_ast)
       OR p_filters IS NULL OR jsonb_typeof(p_filters) <> 'object'
       OR EXISTS (
           SELECT 1 FROM jsonb_object_keys(p_filters) AS filter_key(value)
            WHERE filter_key.value NOT IN (
                'path_prefix', 'role', 'occurred_from', 'occurred_to',
                'graph_profile', 'semantic_profile', 'rerank_profile'
            )
       )
       OR EXISTS (
           SELECT 1 FROM jsonb_each(p_filters) AS entry(key, value)
            WHERE jsonb_typeof(entry.value) <> 'string'
               OR btrim(entry.value #>> '{}') = ''
       )
       OR p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'valid exact retrieval request required';
    END IF;
    v_generation := storage_v2_resolve_generation(p_source_id, p_generation_selector);

    WITH RECURSIVE
    ast_nodes(node, negated) AS (
        SELECT p_ast, FALSE
        UNION ALL
        SELECT child.value,
               parent.negated <> (parent.node ->> 'type' = 'not')
          FROM ast_nodes parent
          CROSS JOIN LATERAL jsonb_array_elements(
              CASE WHEN jsonb_typeof(parent.node -> 'children') = 'array'
                   THEN parent.node -> 'children' ELSE '[]'::JSONB END
          ) child
    ),
    leaves AS (
        SELECT node ->> 'type' AS kind, lower(node ->> 'value') AS value, negated
          FROM ast_nodes
         WHERE node ->> 'type' IN ('term', 'phrase', 'exact')
    ),
    query_values AS (
        SELECT
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'term'), ARRAY[]::TEXT[]) AS terms,
            COALESCE(array_agg(DISTINCT digest(value, 'sha256'))
                FILTER (WHERE kind = 'term'), ARRAY[]::BYTEA[]) AS term_hashes,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'term' AND NOT negated), ARRAY[]::TEXT[]) AS score_terms,
            COALESCE(array_agg(DISTINCT digest(value, 'sha256'))
                FILTER (WHERE kind = 'term' AND NOT negated), ARRAY[]::BYTEA[]) AS score_term_hashes,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'phrase'), ARRAY[]::TEXT[]) AS phrases,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'exact'), ARRAY[]::TEXT[]) AS exact_values
          FROM leaves
    ),
    visible_occurrence AS (
        SELECT occurrence_row.id,occurrence_row.source_id,
               occurrence_row.artifact_version_id,occurrence_row.view_id,
               occurrence_row.role,occurrence_row.ordinal
          FROM occurrence occurrence_row
          JOIN generation_item_version membership
            ON membership.source_id = p_source_id
           AND membership.artifact_version_id = occurrence_row.artifact_version_id
         WHERE occurrence_row.source_id = p_source_id
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > v_generation.generation_seq)
           AND (COALESCE(p_filters ->> 'path_prefix', '') = ''
                OR left(occurrence_row.source_path, char_length(p_filters ->> 'path_prefix'))
                   = p_filters ->> 'path_prefix')
           AND (COALESCE(p_filters ->> 'role', '') = ''
                OR occurrence_row.role = p_filters ->> 'role')
           AND (COALESCE(p_filters ->> 'occurred_from', '') = ''
                OR occurrence_row.occurred_at >= (p_filters ->> 'occurred_from')::TIMESTAMPTZ)
           AND (COALESCE(p_filters ->> 'occurred_to', '') = ''
                OR occurrence_row.occurred_at < (p_filters ->> 'occurred_to')::TIMESTAMPTZ)
    ),
    scoped_binding AS MATERIALIZED (
        SELECT visible.id AS occurrence_id,
               binding.ordinal AS component_ordinal, binding.document_id,
               binding.role_weight, document.token_count
          FROM visible_occurrence visible
          JOIN storage_v2_search_view_document binding ON binding.view_id = visible.view_id
          JOIN storage_v2_search_document document ON document.id = binding.document_id
    ),
    view_stats AS (
        SELECT occurrence_id, SUM(token_count)::DOUBLE PRECISION AS view_length
          FROM scoped_binding GROUP BY occurrence_id
    ),
    corpus_stats AS (
        SELECT COUNT(*)::DOUBLE PRECISION AS view_count,
               AVG(view_length) AS average_view_length FROM view_stats
    ),
    scoped_document AS MATERIALIZED (
        SELECT DISTINCT document_id FROM scoped_binding
    ),
    query_posting AS MATERIALIZED (
        SELECT posting.document_id,posting.term,posting.term_frequency
          FROM query_values query
          CROSS JOIN unnest(query.terms) requested_term(value)
          CROSS JOIN LATERAL storage_v2_scoped_term_posting(
              ARRAY(SELECT document_id FROM scoped_document),requested_term.value
          ) posting
    ),
    scoped_posting AS MATERIALIZED (
        SELECT binding.occurrence_id, binding.component_ordinal, binding.role_weight,
               posting.term, posting.term_frequency
          FROM scoped_binding binding
          JOIN query_posting posting ON posting.document_id = binding.document_id
    ),
    document_frequency AS (
        SELECT term, COUNT(DISTINCT occurrence_id)::DOUBLE PRECISION AS frequency
          FROM scoped_posting GROUP BY term
    ),
    term_rows AS (
        SELECT posting.occurrence_id, posting.term, posting.component_ordinal,
               posting.role_weight,
               posting.role_weight
                 * LN(1 + (stats.view_count + 1.0) / (frequency.frequency + 1.0))
                 * posting.term_frequency
                 / (posting.term_frequency + 0.5
                    + 0.5 * (view_stats.view_length / NULLIF(stats.average_view_length, 0)))
                 AS contribution
          FROM scoped_posting posting
          JOIN view_stats ON view_stats.occurrence_id = posting.occurrence_id
          JOIN document_frequency frequency ON frequency.term = posting.term
          CROSS JOIN corpus_stats stats
          CROSS JOIN query_values query
         WHERE posting.term = ANY(query.score_terms)
           AND posting.occurrence_id NOT IN (SELECT occurrence_id FROM lexical_ranks copied
              WHERE copied.score>=1000000.0 AND copied.occurrence_id IS NOT NULL)
    ),
    term_match_aggregate AS MATERIALIZED (
        SELECT occurrence_id, array_agg(DISTINCT term ORDER BY term) AS matched_terms
          FROM scoped_posting
         WHERE NOT ((p_ast->>'type'='term' OR storage_v2_simple_and_query(p_ast) IS NOT NULL)
           AND occurrence_id IN (SELECT occurrence_id FROM lexical_ranks WHERE score>=1000000.0))
         GROUP BY occurrence_id
    ),
    best_term AS (
        SELECT DISTINCT ON (occurrence_id, term)
               occurrence_id, term, component_ordinal, role_weight, contribution
          FROM term_rows
         ORDER BY occurrence_id, term, contribution DESC, component_ordinal
    ),
    term_aggregate AS MATERIALIZED (
        SELECT occurrence_id,SUM(contribution) AS lexical_terms
          FROM best_term GROUP BY occurrence_id
    ),
    phrase_aggregate AS MATERIALIZED (
        SELECT binding.occurrence_id,
               array_agg(DISTINCT phrase.value ORDER BY phrase.value) AS matched_phrases
          FROM (
              SELECT scope.occurrence_id, document.fts_simple, document.search_text
                FROM scoped_binding scope
                JOIN storage_v2_search_document document ON document.id = scope.document_id
          ) binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.phrases) AS phrase(value)
         WHERE cardinality((SELECT phrases FROM query_values)) > 0 AND storage_v2_phrase_matches(binding.fts_simple, binding.search_text, phrase.value)
         GROUP BY binding.occurrence_id
    ),
    exact_aggregate AS MATERIALIZED (
        SELECT binding.occurrence_id,
               array_agg(DISTINCT exact.value ORDER BY exact.value) AS matched_exact
          FROM (
              SELECT scope.occurrence_id, document.exact_identifiers
                FROM scoped_binding scope
                JOIN storage_v2_search_document document ON document.id = scope.document_id
          ) binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.exact_values) AS exact(value)
         WHERE cardinality((SELECT exact_values FROM query_values)) > 0 AND exact.value = ANY(binding.exact_identifiers)
         GROUP BY binding.occurrence_id
    ),
    evidence_occurrence AS MATERIALIZED (
        SELECT occurrence_id FROM term_match_aggregate
        UNION SELECT occurrence_id FROM phrase_aggregate
        UNION SELECT occurrence_id FROM exact_aggregate
        UNION SELECT occurrence_id FROM lexical_ranks
         WHERE score<1000000.0
            OR NOT (p_ast->>'type'='term' OR storage_v2_simple_and_query(p_ast) IS NOT NULL)
    ),
    matched AS MATERIALIZED (
        SELECT visible.*, view_stats.view_length,
               COALESCE(term_match_aggregate.matched_terms, ARRAY[]::TEXT[]) AS matched_terms,
               COALESCE(phrase_aggregate.matched_phrases, ARRAY[]::TEXT[]) AS matched_phrases,
               COALESCE(exact_aggregate.matched_exact, ARRAY[]::TEXT[]) AS matched_exact,
               COALESCE(term_aggregate.lexical_terms, 0.0)
                 + 1.5 * cardinality(COALESCE(phrase_aggregate.matched_phrases, ARRAY[]::TEXT[]))
                 + 2.0 * cardinality(COALESCE(exact_aggregate.matched_exact, ARRAY[]::TEXT[]))
                 AS lexical_score,
               '[]'::JSONB AS term_detail
          FROM visible_occurrence visible
          JOIN evidence_occurrence evidence ON evidence.occurrence_id=visible.id
          JOIN view_stats ON view_stats.occurrence_id = visible.id
          LEFT JOIN term_match_aggregate ON term_match_aggregate.occurrence_id = visible.id
          LEFT JOIN term_aggregate ON term_aggregate.occurrence_id = visible.id
          LEFT JOIN phrase_aggregate ON phrase_aggregate.occurrence_id = visible.id
          LEFT JOIN exact_aggregate ON exact_aggregate.occurrence_id = visible.id
         WHERE NOT ((p_ast->>'type'='term' OR storage_v2_simple_and_query(p_ast) IS NOT NULL)
           AND visible.id IN (SELECT occurrence_id FROM lexical_ranks WHERE score>=1000000.0))
        UNION ALL
        SELECT visible.*, view_stats.view_length,
               ARRAY[]::TEXT[],ARRAY[]::TEXT[],ARRAY[]::TEXT[],0.0::DOUBLE PRECISION,'[]'::JSONB
          FROM lexical_ranks copied
          JOIN visible_occurrence visible ON visible.id=copied.occurrence_id
          JOIN view_stats ON view_stats.occurrence_id=visible.id
         WHERE copied.score>=1000000.0
           AND (p_ast->>'type'='term' OR storage_v2_simple_and_query(p_ast) IS NOT NULL)
    ),
    lexical_ranks AS MATERIALIZED (
        SELECT ranked.*
          FROM storage_v2_source_segment_rank_candidates(
              (SELECT array_agg(id) FROM visible_occurrence),
              CASE WHEN p_ast ->> 'type' = 'term' THEN p_ast ->> 'value'
                   ELSE storage_v2_simple_and_query(p_ast) END,
              ARRAY[p_source_id]
          ) ranked
    ),
    lexical_presence AS MATERIALIZED (
        SELECT present.occurrence_id
          FROM storage_v2_source_segment_presence(
              CASE WHEN storage_v2_simple_and_query(p_ast) IS NOT NULL
                   THEN (SELECT array_agg(matched.id) FROM matched
                         LEFT JOIN lexical_ranks ranked ON ranked.occurrence_id=matched.id
                        WHERE ranked.occurrence_id IS NULL)
                   ELSE NULL::BIGINT[] END
          ) present
    ),
    boolean_matched AS (
        SELECT matched.*, lexical_rank.score AS segment_score,
               lexical_rank.segment_order AS candidate_sort_key
          FROM matched
          LEFT JOIN lexical_ranks lexical_rank
            ON lexical_rank.occurrence_id = matched.id
          LEFT JOIN lexical_presence presence
            ON presence.occurrence_id = matched.id
         WHERE CASE WHEN lexical_rank.occurrence_id IS NOT NULL
                    AND (p_ast->>'type'='term' OR storage_v2_simple_and_query(p_ast) IS NOT NULL)
                    THEN TRUE
                    WHEN lexical_rank.occurrence_id IS NOT NULL
                    OR cardinality(matched_terms)>0
                    OR cardinality(matched_phrases)>0
                    OR cardinality(matched_exact)>0 THEN
             (storage_v2_simple_and_query(p_ast) IS NULL AND (
                 storage_v2_search_ast_matches(
                     p_ast, matched_terms, matched_phrases, matched_exact
                 ) OR (p_ast ->> 'type' = 'term' AND lexical_rank.occurrence_id IS NOT NULL)
             )) OR (storage_v2_simple_and_query(p_ast) IS NOT NULL AND (
                 lexical_rank.occurrence_id IS NOT NULL
                 OR (presence.occurrence_id IS NULL AND storage_v2_search_ast_matches(
                     p_ast, matched_terms, matched_phrases, matched_exact
                 ))
             ))
             ELSE FALSE END
    ),
    staged AS (
        SELECT matched.*,
               graph.status AS graph_status, COALESCE(graph.score, 0.0) AS graph_score,
               semantic.status AS semantic_status, COALESCE(semantic.score, 0.0) AS semantic_score,
               rerank.status AS rerank_status, COALESCE(rerank.score, 0.0) AS rerank_score
          FROM boolean_matched matched
          LEFT JOIN storage_v2_occurrence_score_component graph
            ON graph.occurrence_id = matched.id AND graph.stage = 'graph'
           AND graph.profile_id = p_filters ->> 'graph_profile' AND graph.score IS NOT NULL AND graph.score<>0
          LEFT JOIN storage_v2_occurrence_score_component semantic
            ON semantic.occurrence_id = matched.id AND semantic.stage = 'semantic'
           AND semantic.profile_id = p_filters ->> 'semantic_profile' AND semantic.score IS NOT NULL AND semantic.score<>0
          LEFT JOIN storage_v2_occurrence_score_component rerank
            ON rerank.occurrence_id = matched.id AND rerank.stage = 'rerank'
           AND rerank.profile_id = p_filters ->> 'rerank_profile' AND rerank.score IS NOT NULL AND rerank.score<>0
    ),
    ranked AS (
        SELECT staged.*,
               CASE WHEN staged.segment_score >= 1000000.0
                    THEN staged.segment_score
                    ELSE lexical_score
               END + graph_score + semantic_score + rerank_score AS final_score
          FROM staged
    ),
    fragmented_match AS MATERIALIZED (
        SELECT ranked.id,ranked.source_id,fragment.source_path,ranked.final_score
          FROM ranked JOIN occurrence fragment ON fragment.id=ranked.id
         WHERE fragment.role='artifact' AND fragment.locator @> '{"fragmented":true}'::JSONB
    ),
    result_group_score AS (
        SELECT ranked.final_score FROM ranked
         WHERE NOT EXISTS (SELECT 1 FROM fragmented_match fragment WHERE fragment.id=ranked.id)
        UNION ALL
        SELECT max(fragment.final_score) FROM fragmented_match fragment
         GROUP BY fragment.source_id,fragment.source_path
    ),
    score_boundary AS MATERIALIZED (
        SELECT min(final_score) AS final_score FROM (
            SELECT final_score FROM result_group_score ORDER BY final_score DESC LIMIT p_limit
        ) top_scores
    ),
    bounded AS MATERIALIZED (
        SELECT ranked.* FROM ranked CROSS JOIN score_boundary boundary
         WHERE ranked.final_score>=boundary.final_score
    ),
    bounded_native_ranks AS MATERIALIZED (
        SELECT precision.* FROM storage_v2_source_segment_ranks_precise(
            ARRAY(SELECT id FROM bounded WHERE segment_score<1000000.0),
            CASE WHEN p_ast->>'type'='term' THEN p_ast->>'value'
                 ELSE storage_v2_simple_and_query(p_ast) END
        ) precision
    ),
    lexical_keyed AS MATERIALIZED (
        SELECT bounded.*, COALESCE(precision.segment_order,bounded.candidate_sort_key) AS lexical_sort_key
          FROM bounded
          LEFT JOIN bounded_native_ranks precision ON precision.occurrence_id=bounded.id
    ),
    fragment_prefix AS MATERIALIZED (
        SELECT DISTINCT ON (keyed.source_id,fragment.source_path)
               keyed.source_id,fragment.source_path,keyed.final_score,keyed.lexical_sort_key
          FROM lexical_keyed keyed JOIN fragmented_match fragment ON fragment.id=keyed.id
         ORDER BY keyed.source_id,fragment.source_path,keyed.final_score DESC,
                  keyed.lexical_sort_key NULLS LAST
    ),
    group_prefix AS (
        SELECT keyed.final_score,keyed.lexical_sort_key FROM lexical_keyed keyed
         WHERE NOT EXISTS (SELECT 1 FROM fragmented_match fragment WHERE fragment.id=keyed.id)
        UNION ALL
        SELECT prefix.final_score,prefix.lexical_sort_key FROM fragment_prefix prefix
    ),
    prefix_boundary AS MATERIALIZED (
        SELECT final_score,lexical_sort_key FROM (
            SELECT final_score,lexical_sort_key FROM group_prefix
             ORDER BY final_score DESC,lexical_sort_key NULLS LAST LIMIT p_limit
        ) prefix_top
         ORDER BY final_score,lexical_sort_key DESC NULLS FIRST LIMIT 1
    ),
    fragment_contenders AS MATERIALIZED (
        SELECT keyed.id FROM lexical_keyed keyed
          JOIN fragmented_match fragment ON fragment.id=keyed.id
          JOIN fragment_prefix prefix ON prefix.source_id=fragment.source_id
           AND prefix.source_path=fragment.source_path AND prefix.final_score=keyed.final_score
         WHERE prefix.lexical_sort_key IS NOT DISTINCT FROM keyed.lexical_sort_key
    ),
    identity_candidates AS MATERIALIZED (
        SELECT keyed.* FROM lexical_keyed keyed CROSS JOIN prefix_boundary boundary
         WHERE (keyed.final_score>boundary.final_score OR
                (keyed.final_score=boundary.final_score AND
                 (boundary.lexical_sort_key IS NULL OR keyed.lexical_sort_key<boundary.lexical_sort_key OR
                  keyed.lexical_sort_key IS NOT DISTINCT FROM boundary.lexical_sort_key)))
           AND (NOT EXISTS (SELECT 1 FROM fragmented_match fragment WHERE fragment.id=keyed.id)
                OR EXISTS (SELECT 1 FROM fragment_contenders contender WHERE contender.id=keyed.id))
    ),
    identified AS MATERIALIZED (
        SELECT bounded.*, identified_occurrence.source_path,
               identified_occurrence.locator, source.name AS source_name, item.item_key,
               artifact.expected_content_hash, view_row.view_digest,
               'storage-v2:' || encode(storage_v2_hash_parts(
                    'mainrag.external-hit.v1', ARRAY[
                        int8send(bounded.source_id),
                        convert_to(item.item_key, 'UTF8'),
                        convert_to(artifact.expected_content_hash, 'UTF8'),
                        view_row.view_digest,
                        convert_to(bounded.role, 'UTF8'),
                        int8send(bounded.ordinal),
                        convert_to(identified_occurrence.locator::TEXT, 'UTF8')
                    ]
               ), 'hex') AS external_hit_id
          FROM identity_candidates bounded
          JOIN LATERAL (SELECT row.* FROM occurrence row WHERE row.id=bounded.id OFFSET 0) identified_occurrence ON TRUE
          JOIN LATERAL (SELECT row.* FROM artifact_version row WHERE row.id=bounded.artifact_version_id OFFSET 0) artifact ON TRUE
          JOIN LATERAL (SELECT row.* FROM source_item row WHERE row.id=artifact.item_id OFFSET 0) item ON TRUE
          JOIN LATERAL (SELECT row.* FROM retrieval_view row WHERE row.id=bounded.view_id OFFSET 0) view_row ON TRUE
          JOIN LATERAL (SELECT row.* FROM sources row WHERE row.id=bounded.source_id OFFSET 0) source ON TRUE
    ),
    identified_grouped AS MATERIALIZED (
        SELECT * FROM identified
         WHERE NOT (role='artifact' AND locator @> '{"fragmented":true}'::JSONB)
        UNION ALL
        SELECT * FROM (
            SELECT DISTINCT ON (source_id,source_path) * FROM identified
             WHERE role='artifact' AND locator @> '{"fragmented":true}'::JSONB
             ORDER BY source_id,source_path,final_score DESC,
                      lexical_sort_key NULLS LAST,external_hit_id,id
        ) best_fragment
    ),
    ordered AS (
        SELECT * FROM identified_grouped
         ORDER BY final_score DESC,lexical_sort_key NULLS LAST,external_hit_id,id
         LIMIT p_limit
    ),
    returned_term_rows AS (
        SELECT posting.occurrence_id, posting.term, posting.component_ordinal,
               posting.role_weight,
               posting.role_weight
                 * LN(1 + (stats.view_count + 1.0) / (frequency.frequency + 1.0))
                 * posting.term_frequency
                 / (posting.term_frequency + 0.5
                    + 0.5 * (view_stats.view_length / NULLIF(stats.average_view_length, 0)))
                 AS contribution
          FROM scoped_posting posting
          JOIN ordered returned ON returned.id=posting.occurrence_id
          JOIN view_stats ON view_stats.occurrence_id = posting.occurrence_id
          JOIN document_frequency frequency ON frequency.term = posting.term
          CROSS JOIN corpus_stats stats
          CROSS JOIN query_values query
         WHERE posting.term = ANY(query.score_terms)
    ),
    returned_best_term AS (
        SELECT DISTINCT ON (occurrence_id, term)
               occurrence_id, term, component_ordinal, role_weight, contribution
          FROM returned_term_rows
         ORDER BY occurrence_id, term, contribution DESC, component_ordinal
    ),
    returned_term_aggregate AS MATERIALIZED (
        SELECT occurrence_id,SUM(contribution) AS lexical_terms
          FROM returned_best_term GROUP BY occurrence_id
    ),
    results AS (
        SELECT jsonb_agg(jsonb_build_object(
            'occurrence_id', id,
            'external_hit_id', external_hit_id,
            'view_id', view_id,
            'source_id', source_id,
            'source_name', source_name,
            'source_path', source_path,
            'locator', locator,
            'role', role,
            'content', (
                SELECT string_agg(document.search_text, E'\n' ORDER BY binding.ordinal)
                  FROM storage_v2_search_view_document binding
                  JOIN storage_v2_search_document document ON document.id = binding.document_id
                 WHERE binding.view_id = ordered.view_id
            ),
            'score', final_score,
            'score_explanation', jsonb_build_object(
                'lexical', CASE WHEN ordered.segment_score>=1000000.0 THEN
                    COALESCE((SELECT lexical_terms FROM returned_term_aggregate explanation
                               WHERE explanation.occurrence_id=ordered.id),0.0)
                    +1.5*cardinality(ordered.matched_phrases)
                    +2.0*cardinality(ordered.matched_exact)
                    ELSE lexical_score END,
                'role_weighted_terms', COALESCE((
                    SELECT jsonb_agg(jsonb_build_object(
                        'term', detail.term, 'component_ordinal', detail.component_ordinal,
                        'role_weight', detail.role_weight, 'score', detail.contribution
                    ) ORDER BY detail.term)
                    FROM returned_best_term detail WHERE detail.occurrence_id=ordered.id
                ),'[]'::JSONB),
                'normalization', jsonb_build_object(
                    'view_token_count', view_length,
                    'scope_average_view_token_count', (SELECT average_view_length FROM corpus_stats)
                ),
                'graph', jsonb_build_object(
                    'status', COALESCE((SELECT component.status
                        FROM storage_v2_occurrence_score_component component
                       WHERE component.occurrence_id=ordered.id AND component.stage='graph'
                         AND component.profile_id=p_filters->>'graph_profile'),
                        CASE WHEN p_filters ? 'graph_profile' THEN 'unavailable' ELSE 'not_requested' END),
                    'score', graph_score
                ),
                'semantic', jsonb_build_object(
                    'status', COALESCE((SELECT component.status
                        FROM storage_v2_occurrence_score_component component
                       WHERE component.occurrence_id=ordered.id AND component.stage='semantic'
                         AND component.profile_id=p_filters->>'semantic_profile'),
                        CASE WHEN p_filters ? 'semantic_profile' THEN 'unavailable' ELSE 'not_requested' END),
                    'score', semantic_score
                ),
                'rerank', jsonb_build_object(
                    'status', COALESCE((SELECT component.status
                        FROM storage_v2_occurrence_score_component component
                       WHERE component.occurrence_id=ordered.id AND component.stage='rerank'
                         AND component.profile_id=p_filters->>'rerank_profile'),
                        CASE WHEN p_filters ? 'rerank_profile' THEN 'unavailable' ELSE 'not_requested' END),
                    'score', rerank_score
                ),
                'execution', 'complete_scoped_view_evaluation',
                'pruning', 'disabled_unsafe_bounds'
            ),
            'legacy_successors', COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'old_hit_id', mapping.old_hit_id,
                    'ordinal', mapping.ordinal,
                    'relation_kind', mapping.relation_kind
                ) ORDER BY mapping.old_hit_id, mapping.ordinal)
                  FROM legacy_hit_mapping mapping WHERE mapping.occurrence_id = ordered.id
            ), '[]'::JSONB)
        ) ORDER BY final_score DESC, lexical_sort_key NULLS LAST, external_hit_id, id) AS value FROM ordered
    )
    SELECT jsonb_build_object(
        'generation_seq', v_generation.generation_seq,
        'execution', 'complete_scoped_view_evaluation',
        'fully_scored_views', (SELECT COUNT(*) FROM view_stats),
        'total', (SELECT COUNT(*) FROM ranked),
        'results', COALESCE((SELECT value FROM results), '[]'::JSONB)
    ) INTO v_result;

    IF EXISTS (
        SELECT 1 FROM occurrence occurrence_row
        JOIN artifact_version artifact ON artifact.id = occurrence_row.artifact_version_id
        JOIN generation_item_version membership
          ON membership.source_id = p_source_id
         AND membership.source_item_id = artifact.item_id
         AND membership.artifact_version_id = artifact.id
       WHERE occurrence_row.source_id = p_source_id
         AND membership.valid_from_seq <= v_generation.generation_seq
         AND (membership.valid_to_seq IS NULL
              OR membership.valid_to_seq > v_generation.generation_seq)
         AND (COALESCE(p_filters ->> 'path_prefix', '') = ''
              OR left(occurrence_row.source_path, char_length(p_filters ->> 'path_prefix'))
                 = p_filters ->> 'path_prefix')
         AND (COALESCE(p_filters ->> 'role', '') = ''
              OR occurrence_row.role = p_filters ->> 'role')
         AND (COALESCE(p_filters ->> 'occurred_from', '') = ''
              OR occurrence_row.occurred_at >= (p_filters ->> 'occurred_from')::TIMESTAMPTZ)
         AND (COALESCE(p_filters ->> 'occurred_to', '') = ''
              OR occurrence_row.occurred_at < (p_filters ->> 'occurred_to')::TIMESTAMPTZ)
         AND NOT EXISTS (
             SELECT 1 FROM storage_v2_search_view_document binding
              WHERE binding.view_id = occurrence_row.view_id
         )
    ) THEN
        RAISE EXCEPTION 'required lexical search document missing';
    END IF;
    RETURN v_result;
END
$function$

;

CREATE OR REPLACE FUNCTION public.storage_v2_search_active_unchecked(p_manifest_sha256 text, p_ast jsonb, p_filters jsonb DEFAULT '{}'::jsonb, p_limit bigint DEFAULT 20, p_source_id bigint DEFAULT NULL::bigint, p_include_test boolean DEFAULT false)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
 SET row_security TO 'off'
 SET plan_cache_mode TO 'force_custom_plan'
 SET jit TO 'off'
AS $function$
DECLARE
    v_receipt storage_v2_activation_set_evidence;
    v_result JSONB;
BEGIN
    IF p_manifest_sha256 IS NULL OR p_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'exact activated manifest digest is required';
    END IF;
    SELECT * INTO v_receipt FROM storage_v2_activation_set_evidence
     ORDER BY created_at DESC, id DESC LIMIT 1;
    IF NOT FOUND OR v_receipt.manifest_sha256 <> p_manifest_sha256
       OR v_receipt.source_count <> (SELECT COUNT(*) FROM sources)
       OR v_receipt.source_count <> (SELECT COUNT(*) FROM logical_source)
       OR v_receipt.source_classification_sha256 IS DISTINCT FROM (
           SELECT encode(digest(convert_to(COALESCE(jsonb_agg(jsonb_build_object(
               'source_id', source.id, 'is_test', source.is_test
           ) ORDER BY source.id), '[]'::JSONB)::TEXT, 'UTF8'), 'sha256'), 'hex')
             FROM sources source
       )
       OR EXISTS (
           SELECT 1 FROM logical_source pointer
           LEFT JOIN source_generation active_generation
             ON active_generation.id = pointer.active_generation_id
            AND active_generation.source_id = pointer.id
           WHERE active_generation.id IS NULL OR active_generation.status <> 'active'
       ) THEN
        RAISE EXCEPTION 'complete activated source set and exact receipt are required';
    END IF;
    IF p_include_test AND NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'test scope requires administrator authority' USING ERRCODE = '42501';
    END IF;
    IF p_source_id IS NOT NULL THEN
        PERFORM storage_v2_require_test_scope(p_source_id, p_include_test);
    END IF;
    IF p_ast IS NULL OR NOT storage_v2_search_ast_is_valid(p_ast)
       OR NOT storage_v2_search_ast_has_anchor(p_ast)
       OR p_filters IS NULL OR jsonb_typeof(p_filters) <> 'object'
       OR EXISTS (
           SELECT 1 FROM jsonb_object_keys(p_filters) AS filter_key(value)
            WHERE filter_key.value NOT IN (
                'path_prefix', 'role', 'occurred_from', 'occurred_to',
                'graph_profile', 'semantic_profile', 'rerank_profile'
            )
       )
       OR EXISTS (
           SELECT 1 FROM jsonb_each(p_filters) AS entry(key, value)
            WHERE jsonb_typeof(entry.value) <> 'string'
               OR btrim(entry.value #>> '{}') = ''
       )
       OR p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'valid exact retrieval request required';
    END IF;

    WITH RECURSIVE
    eligible_source AS MATERIALIZED (
        SELECT source.id, source.name, active_generation.generation_seq
          FROM sources source
          JOIN logical_source pointer ON pointer.id = source.id
          JOIN source_generation active_generation
            ON active_generation.id = pointer.active_generation_id
           AND active_generation.source_id = pointer.id
           AND active_generation.status = 'active'
         WHERE (p_source_id IS NULL OR source.id = p_source_id)
           AND storage_v2_can_access_source(source.id, 'read')
           AND (p_include_test OR NOT source.is_test)
    ),
    ast_nodes(node, negated) AS (
        SELECT p_ast, FALSE
        UNION ALL
        SELECT child.value,
               parent.negated <> (parent.node ->> 'type' = 'not')
          FROM ast_nodes parent
          CROSS JOIN LATERAL jsonb_array_elements(
              CASE WHEN jsonb_typeof(parent.node -> 'children') = 'array'
                   THEN parent.node -> 'children' ELSE '[]'::JSONB END
          ) child
    ),
    leaves AS (
        SELECT node ->> 'type' AS kind, lower(node ->> 'value') AS value, negated
          FROM ast_nodes
         WHERE node ->> 'type' IN ('term', 'phrase', 'exact')
    ),
    query_values AS (
        SELECT
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'term'), ARRAY[]::TEXT[]) AS terms,
            COALESCE(array_agg(DISTINCT digest(value, 'sha256'))
                FILTER (WHERE kind = 'term'), ARRAY[]::BYTEA[]) AS term_hashes,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'term' AND NOT negated), ARRAY[]::TEXT[]) AS score_terms,
            COALESCE(array_agg(DISTINCT digest(value, 'sha256'))
                FILTER (WHERE kind = 'term' AND NOT negated), ARRAY[]::BYTEA[]) AS score_term_hashes,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'phrase'), ARRAY[]::TEXT[]) AS phrases,
            COALESCE(array_agg(DISTINCT value ORDER BY value)
                FILTER (WHERE kind = 'exact'), ARRAY[]::TEXT[]) AS exact_values
          FROM leaves
    ),
    visible_occurrence AS (
        SELECT occurrence_row.id,occurrence_row.source_id,
               occurrence_row.artifact_version_id,occurrence_row.view_id,
               occurrence_row.role,occurrence_row.ordinal, source.name AS source_name, source.generation_seq
          FROM occurrence occurrence_row
          JOIN eligible_source source ON source.id = occurrence_row.source_id
          JOIN generation_item_version membership
            ON membership.source_id = occurrence_row.source_id
           AND membership.artifact_version_id = occurrence_row.artifact_version_id
         WHERE membership.valid_from_seq <= source.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > source.generation_seq)
           AND (COALESCE(p_filters ->> 'path_prefix', '') = ''
                OR left(occurrence_row.source_path, char_length(p_filters ->> 'path_prefix'))
                   = p_filters ->> 'path_prefix')
           AND (COALESCE(p_filters ->> 'role', '') = ''
                OR occurrence_row.role = p_filters ->> 'role')
           AND (COALESCE(p_filters ->> 'occurred_from', '') = ''
                OR occurrence_row.occurred_at >= (p_filters ->> 'occurred_from')::TIMESTAMPTZ)
           AND (COALESCE(p_filters ->> 'occurred_to', '') = ''
                OR occurrence_row.occurred_at < (p_filters ->> 'occurred_to')::TIMESTAMPTZ)
    ),
    scoped_binding AS MATERIALIZED (
        SELECT visible.id AS occurrence_id,
               binding.ordinal AS component_ordinal, binding.document_id,
               binding.role_weight, document.token_count
          FROM visible_occurrence visible
          JOIN storage_v2_search_view_document binding ON binding.view_id = visible.view_id
          JOIN storage_v2_search_document document ON document.id = binding.document_id
    ),
    view_stats AS (
        SELECT occurrence_id, SUM(token_count)::DOUBLE PRECISION AS view_length
          FROM scoped_binding GROUP BY occurrence_id
    ),
    corpus_stats AS (
        SELECT COUNT(*)::DOUBLE PRECISION AS view_count,
               AVG(view_length) AS average_view_length FROM view_stats
    ),
    scoped_document AS MATERIALIZED (
        SELECT DISTINCT document_id FROM scoped_binding
    ),
    query_posting AS MATERIALIZED (
        SELECT posting.document_id,posting.term,posting.term_frequency
          FROM query_values query
          CROSS JOIN unnest(query.terms) requested_term(value)
          CROSS JOIN LATERAL storage_v2_scoped_term_posting(
              ARRAY(SELECT document_id FROM scoped_document),requested_term.value
          ) posting
    ),
    scoped_posting AS MATERIALIZED (
        SELECT binding.occurrence_id, binding.component_ordinal, binding.role_weight,
               posting.term, posting.term_frequency
          FROM scoped_binding binding
          JOIN query_posting posting ON posting.document_id = binding.document_id
    ),
    document_frequency AS (
        SELECT term, COUNT(DISTINCT occurrence_id)::DOUBLE PRECISION AS frequency
          FROM scoped_posting GROUP BY term
    ),
    term_rows AS (
        SELECT posting.occurrence_id, posting.term, posting.component_ordinal,
               posting.role_weight,
               posting.role_weight
                 * LN(1 + (stats.view_count + 1.0) / (frequency.frequency + 1.0))
                 * posting.term_frequency
                 / (posting.term_frequency + 0.5
                    + 0.5 * (view_stats.view_length / NULLIF(stats.average_view_length, 0)))
                 AS contribution
          FROM scoped_posting posting
          JOIN view_stats ON view_stats.occurrence_id = posting.occurrence_id
          JOIN document_frequency frequency ON frequency.term = posting.term
          CROSS JOIN corpus_stats stats
          CROSS JOIN query_values query
         WHERE posting.term = ANY(query.score_terms)
           AND posting.occurrence_id NOT IN (SELECT occurrence_id FROM lexical_ranks copied
              WHERE copied.score>=1000000.0 AND copied.occurrence_id IS NOT NULL)
    ),
    term_match_aggregate AS MATERIALIZED (
        SELECT occurrence_id, array_agg(DISTINCT term ORDER BY term) AS matched_terms
          FROM scoped_posting
         WHERE NOT ((p_ast->>'type'='term' OR storage_v2_simple_and_query(p_ast) IS NOT NULL)
           AND occurrence_id IN (SELECT occurrence_id FROM lexical_ranks WHERE score>=1000000.0))
         GROUP BY occurrence_id
    ),
    best_term AS (
        SELECT DISTINCT ON (occurrence_id, term)
               occurrence_id, term, component_ordinal, role_weight, contribution
          FROM term_rows
         ORDER BY occurrence_id, term, contribution DESC, component_ordinal
    ),
    term_aggregate AS MATERIALIZED (
        SELECT occurrence_id,SUM(contribution) AS lexical_terms
          FROM best_term GROUP BY occurrence_id
    ),
    phrase_aggregate AS MATERIALIZED (
        SELECT binding.occurrence_id,
               array_agg(DISTINCT phrase.value ORDER BY phrase.value) AS matched_phrases
          FROM (
              SELECT scope.occurrence_id, document.fts_simple, document.search_text
                FROM scoped_binding scope
                JOIN storage_v2_search_document document ON document.id = scope.document_id
          ) binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.phrases) AS phrase(value)
         WHERE cardinality((SELECT phrases FROM query_values)) > 0 AND storage_v2_phrase_matches(binding.fts_simple, binding.search_text, phrase.value)
         GROUP BY binding.occurrence_id
    ),
    exact_aggregate AS MATERIALIZED (
        SELECT binding.occurrence_id,
               array_agg(DISTINCT exact.value ORDER BY exact.value) AS matched_exact
          FROM (
              SELECT scope.occurrence_id, document.exact_identifiers
                FROM scoped_binding scope
                JOIN storage_v2_search_document document ON document.id = scope.document_id
          ) binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.exact_values) AS exact(value)
         WHERE cardinality((SELECT exact_values FROM query_values)) > 0 AND exact.value = ANY(binding.exact_identifiers)
         GROUP BY binding.occurrence_id
    ),
    evidence_occurrence AS MATERIALIZED (
        SELECT occurrence_id FROM term_match_aggregate
        UNION SELECT occurrence_id FROM phrase_aggregate
        UNION SELECT occurrence_id FROM exact_aggregate
        UNION SELECT occurrence_id FROM lexical_ranks
         WHERE score<1000000.0
            OR NOT (p_ast->>'type'='term' OR storage_v2_simple_and_query(p_ast) IS NOT NULL)
    ),
    matched AS MATERIALIZED (
        SELECT visible.*, view_stats.view_length,
               COALESCE(term_match_aggregate.matched_terms, ARRAY[]::TEXT[]) AS matched_terms,
               COALESCE(phrase_aggregate.matched_phrases, ARRAY[]::TEXT[]) AS matched_phrases,
               COALESCE(exact_aggregate.matched_exact, ARRAY[]::TEXT[]) AS matched_exact,
               COALESCE(term_aggregate.lexical_terms, 0.0)
                 + 1.5 * cardinality(COALESCE(phrase_aggregate.matched_phrases, ARRAY[]::TEXT[]))
                 + 2.0 * cardinality(COALESCE(exact_aggregate.matched_exact, ARRAY[]::TEXT[]))
                 AS lexical_score,
               '[]'::JSONB AS term_detail
          FROM visible_occurrence visible
          JOIN evidence_occurrence evidence ON evidence.occurrence_id=visible.id
          JOIN view_stats ON view_stats.occurrence_id = visible.id
          LEFT JOIN term_match_aggregate ON term_match_aggregate.occurrence_id = visible.id
          LEFT JOIN term_aggregate ON term_aggregate.occurrence_id = visible.id
          LEFT JOIN phrase_aggregate ON phrase_aggregate.occurrence_id = visible.id
          LEFT JOIN exact_aggregate ON exact_aggregate.occurrence_id = visible.id
         WHERE NOT ((p_ast->>'type'='term' OR storage_v2_simple_and_query(p_ast) IS NOT NULL)
           AND visible.id IN (SELECT occurrence_id FROM lexical_ranks WHERE score>=1000000.0))
        UNION ALL
        SELECT visible.*, view_stats.view_length,
               ARRAY[]::TEXT[],ARRAY[]::TEXT[],ARRAY[]::TEXT[],0.0::DOUBLE PRECISION,'[]'::JSONB
          FROM lexical_ranks copied
          JOIN visible_occurrence visible ON visible.id=copied.occurrence_id
          JOIN view_stats ON view_stats.occurrence_id=visible.id
         WHERE copied.score>=1000000.0
           AND (p_ast->>'type'='term' OR storage_v2_simple_and_query(p_ast) IS NOT NULL)
    ),
    lexical_ranks AS MATERIALIZED (
        SELECT ranked.*
          FROM storage_v2_source_segment_rank_candidates(
              (SELECT array_agg(id) FROM visible_occurrence),
              CASE WHEN p_ast ->> 'type' = 'term' THEN p_ast ->> 'value'
                   ELSE storage_v2_simple_and_query(p_ast) END,
              ARRAY(SELECT id FROM eligible_source)
          ) ranked
    ),
    lexical_presence AS MATERIALIZED (
        SELECT present.occurrence_id
          FROM storage_v2_source_segment_presence(
              CASE WHEN storage_v2_simple_and_query(p_ast) IS NOT NULL
                   THEN (SELECT array_agg(matched.id) FROM matched
                         LEFT JOIN lexical_ranks ranked ON ranked.occurrence_id=matched.id
                        WHERE ranked.occurrence_id IS NULL)
                   ELSE NULL::BIGINT[] END
          ) present
    ),
    boolean_matched AS (
        SELECT matched.*, lexical_rank.score AS segment_score,
               lexical_rank.segment_order AS candidate_sort_key
          FROM matched
          LEFT JOIN lexical_ranks lexical_rank
            ON lexical_rank.occurrence_id = matched.id
          LEFT JOIN lexical_presence presence
            ON presence.occurrence_id = matched.id
         WHERE CASE WHEN lexical_rank.occurrence_id IS NOT NULL
                    AND (p_ast->>'type'='term' OR storage_v2_simple_and_query(p_ast) IS NOT NULL)
                    THEN TRUE
                    WHEN lexical_rank.occurrence_id IS NOT NULL
                    OR cardinality(matched_terms)>0
                    OR cardinality(matched_phrases)>0
                    OR cardinality(matched_exact)>0 THEN
             (storage_v2_simple_and_query(p_ast) IS NULL AND (
                 storage_v2_search_ast_matches(
                     p_ast, matched_terms, matched_phrases, matched_exact
                 ) OR (p_ast ->> 'type' = 'term' AND lexical_rank.occurrence_id IS NOT NULL)
             )) OR (storage_v2_simple_and_query(p_ast) IS NOT NULL AND (
                 lexical_rank.occurrence_id IS NOT NULL
                 OR (presence.occurrence_id IS NULL AND storage_v2_search_ast_matches(
                     p_ast, matched_terms, matched_phrases, matched_exact
                 ))
             ))
             ELSE FALSE END
    ),
    staged AS (
        SELECT matched.*,
               graph.status AS graph_status, COALESCE(graph.score, 0.0) AS graph_score,
               semantic.status AS semantic_status, COALESCE(semantic.score, 0.0) AS semantic_score,
               rerank.status AS rerank_status, COALESCE(rerank.score, 0.0) AS rerank_score
          FROM boolean_matched matched
          LEFT JOIN storage_v2_occurrence_score_component graph
            ON graph.occurrence_id = matched.id AND graph.stage = 'graph'
           AND graph.profile_id = p_filters ->> 'graph_profile' AND graph.score IS NOT NULL AND graph.score<>0
          LEFT JOIN storage_v2_occurrence_score_component semantic
            ON semantic.occurrence_id = matched.id AND semantic.stage = 'semantic'
           AND semantic.profile_id = p_filters ->> 'semantic_profile' AND semantic.score IS NOT NULL AND semantic.score<>0
          LEFT JOIN storage_v2_occurrence_score_component rerank
            ON rerank.occurrence_id = matched.id AND rerank.stage = 'rerank'
           AND rerank.profile_id = p_filters ->> 'rerank_profile' AND rerank.score IS NOT NULL AND rerank.score<>0
    ),
    ranked AS (
        SELECT staged.*,
               CASE WHEN staged.segment_score >= 1000000.0
                    THEN staged.segment_score
                    ELSE lexical_score
               END + graph_score + semantic_score + rerank_score AS final_score
          FROM staged
    ),
    fragmented_match AS MATERIALIZED (
        SELECT ranked.id,ranked.source_id,fragment.source_path,ranked.final_score
          FROM ranked JOIN occurrence fragment ON fragment.id=ranked.id
         WHERE fragment.role='artifact' AND fragment.locator @> '{"fragmented":true}'::JSONB
    ),
    result_group_score AS (
        SELECT ranked.final_score FROM ranked
         WHERE NOT EXISTS (SELECT 1 FROM fragmented_match fragment WHERE fragment.id=ranked.id)
        UNION ALL
        SELECT max(fragment.final_score) FROM fragmented_match fragment
         GROUP BY fragment.source_id,fragment.source_path
    ),
    score_boundary AS MATERIALIZED (
        SELECT min(final_score) AS final_score FROM (
            SELECT final_score FROM result_group_score ORDER BY final_score DESC LIMIT p_limit
        ) top_scores
    ),
    bounded AS MATERIALIZED (
        SELECT ranked.* FROM ranked CROSS JOIN score_boundary boundary
         WHERE ranked.final_score>=boundary.final_score
    ),
    bounded_native_ranks AS MATERIALIZED (
        SELECT precision.* FROM storage_v2_source_segment_ranks_precise(
            ARRAY(SELECT id FROM bounded WHERE segment_score<1000000.0),
            CASE WHEN p_ast->>'type'='term' THEN p_ast->>'value'
                 ELSE storage_v2_simple_and_query(p_ast) END
        ) precision
    ),
    lexical_keyed AS MATERIALIZED (
        SELECT bounded.*, COALESCE(precision.segment_order,bounded.candidate_sort_key) AS lexical_sort_key
          FROM bounded
          LEFT JOIN bounded_native_ranks precision ON precision.occurrence_id=bounded.id
    ),
    fragment_prefix AS MATERIALIZED (
        SELECT DISTINCT ON (keyed.source_id,fragment.source_path)
               keyed.source_id,fragment.source_path,keyed.final_score,keyed.lexical_sort_key
          FROM lexical_keyed keyed JOIN fragmented_match fragment ON fragment.id=keyed.id
         ORDER BY keyed.source_id,fragment.source_path,keyed.final_score DESC,
                  keyed.lexical_sort_key NULLS LAST
    ),
    group_prefix AS (
        SELECT keyed.final_score,keyed.lexical_sort_key FROM lexical_keyed keyed
         WHERE NOT EXISTS (SELECT 1 FROM fragmented_match fragment WHERE fragment.id=keyed.id)
        UNION ALL
        SELECT prefix.final_score,prefix.lexical_sort_key FROM fragment_prefix prefix
    ),
    prefix_boundary AS MATERIALIZED (
        SELECT final_score,lexical_sort_key FROM (
            SELECT final_score,lexical_sort_key FROM group_prefix
             ORDER BY final_score DESC,lexical_sort_key NULLS LAST LIMIT p_limit
        ) prefix_top
         ORDER BY final_score,lexical_sort_key DESC NULLS FIRST LIMIT 1
    ),
    fragment_contenders AS MATERIALIZED (
        SELECT keyed.id FROM lexical_keyed keyed
          JOIN fragmented_match fragment ON fragment.id=keyed.id
          JOIN fragment_prefix prefix ON prefix.source_id=fragment.source_id
           AND prefix.source_path=fragment.source_path AND prefix.final_score=keyed.final_score
         WHERE prefix.lexical_sort_key IS NOT DISTINCT FROM keyed.lexical_sort_key
    ),
    identity_candidates AS MATERIALIZED (
        SELECT keyed.* FROM lexical_keyed keyed CROSS JOIN prefix_boundary boundary
         WHERE (keyed.final_score>boundary.final_score OR
                (keyed.final_score=boundary.final_score AND
                 (boundary.lexical_sort_key IS NULL OR keyed.lexical_sort_key<boundary.lexical_sort_key OR
                  keyed.lexical_sort_key IS NOT DISTINCT FROM boundary.lexical_sort_key)))
           AND (NOT EXISTS (SELECT 1 FROM fragmented_match fragment WHERE fragment.id=keyed.id)
                OR EXISTS (SELECT 1 FROM fragment_contenders contender WHERE contender.id=keyed.id))
    ),
    identified AS MATERIALIZED (
        SELECT bounded.*, identified_occurrence.source_path,
               identified_occurrence.locator, item.item_key,
               artifact.expected_content_hash, view_row.view_digest,
               'storage-v2:' || encode(storage_v2_hash_parts(
                    'mainrag.external-hit.v1', ARRAY[
                        int8send(bounded.source_id),
                        convert_to(item.item_key, 'UTF8'),
                        convert_to(artifact.expected_content_hash, 'UTF8'),
                        view_row.view_digest,
                        convert_to(bounded.role, 'UTF8'),
                        int8send(bounded.ordinal),
                        convert_to(identified_occurrence.locator::TEXT, 'UTF8')
                    ]
               ), 'hex') AS external_hit_id
          FROM identity_candidates bounded
          JOIN LATERAL (SELECT row.* FROM occurrence row WHERE row.id=bounded.id OFFSET 0) identified_occurrence ON TRUE
          JOIN LATERAL (SELECT row.* FROM artifact_version row WHERE row.id=bounded.artifact_version_id OFFSET 0) artifact ON TRUE
          JOIN LATERAL (SELECT row.* FROM source_item row WHERE row.id=artifact.item_id OFFSET 0) item ON TRUE
          JOIN LATERAL (SELECT row.* FROM retrieval_view row WHERE row.id=bounded.view_id OFFSET 0) view_row ON TRUE
    ),
    identified_grouped AS MATERIALIZED (
        SELECT * FROM identified
         WHERE NOT (role='artifact' AND locator @> '{"fragmented":true}'::JSONB)
        UNION ALL
        SELECT * FROM (
            SELECT DISTINCT ON (source_id,source_path) * FROM identified
             WHERE role='artifact' AND locator @> '{"fragmented":true}'::JSONB
             ORDER BY source_id,source_path,final_score DESC,
                      lexical_sort_key NULLS LAST,external_hit_id,id
        ) best_fragment
    ),
    ordered AS (
        SELECT * FROM identified_grouped
         ORDER BY final_score DESC,lexical_sort_key NULLS LAST,external_hit_id,id
         LIMIT p_limit
    ),
    returned_term_rows AS (
        SELECT posting.occurrence_id, posting.term, posting.component_ordinal,
               posting.role_weight,
               posting.role_weight
                 * LN(1 + (stats.view_count + 1.0) / (frequency.frequency + 1.0))
                 * posting.term_frequency
                 / (posting.term_frequency + 0.5
                    + 0.5 * (view_stats.view_length / NULLIF(stats.average_view_length, 0)))
                 AS contribution
          FROM scoped_posting posting
          JOIN ordered returned ON returned.id=posting.occurrence_id
          JOIN view_stats ON view_stats.occurrence_id = posting.occurrence_id
          JOIN document_frequency frequency ON frequency.term = posting.term
          CROSS JOIN corpus_stats stats
          CROSS JOIN query_values query
         WHERE posting.term = ANY(query.score_terms)
    ),
    returned_best_term AS (
        SELECT DISTINCT ON (occurrence_id, term)
               occurrence_id, term, component_ordinal, role_weight, contribution
          FROM returned_term_rows
         ORDER BY occurrence_id, term, contribution DESC, component_ordinal
    ),
    returned_term_aggregate AS MATERIALIZED (
        SELECT occurrence_id,SUM(contribution) AS lexical_terms
          FROM returned_best_term GROUP BY occurrence_id
    ),
    results AS (
        SELECT jsonb_agg(jsonb_build_object(
            'occurrence_id', id,
            'external_hit_id', external_hit_id,
            'view_id', view_id,
            'source_id', source_id,
            'generation_seq', generation_seq,
            'source_name', source_name,
            'source_path', source_path,
            'locator', locator,
            'role', role,
            'content', (
                SELECT string_agg(document.search_text, E'\n' ORDER BY binding.ordinal)
                  FROM storage_v2_search_view_document binding
                  JOIN storage_v2_search_document document ON document.id = binding.document_id
                 WHERE binding.view_id = ordered.view_id
            ),
            'score', final_score,
            'score_explanation', jsonb_build_object(
                'lexical', CASE WHEN ordered.segment_score>=1000000.0 THEN
                    COALESCE((SELECT lexical_terms FROM returned_term_aggregate explanation
                               WHERE explanation.occurrence_id=ordered.id),0.0)
                    +1.5*cardinality(ordered.matched_phrases)
                    +2.0*cardinality(ordered.matched_exact)
                    ELSE lexical_score END,
                'role_weighted_terms', COALESCE((
                    SELECT jsonb_agg(jsonb_build_object(
                        'term', detail.term, 'component_ordinal', detail.component_ordinal,
                        'role_weight', detail.role_weight, 'score', detail.contribution
                    ) ORDER BY detail.term)
                    FROM returned_best_term detail WHERE detail.occurrence_id=ordered.id
                ),'[]'::JSONB),
                'normalization', jsonb_build_object(
                    'view_token_count', view_length,
                    'scope_average_view_token_count', (SELECT average_view_length FROM corpus_stats)
                ),
                'graph', jsonb_build_object(
                    'status', COALESCE((SELECT component.status
                        FROM storage_v2_occurrence_score_component component
                       WHERE component.occurrence_id=ordered.id AND component.stage='graph'
                         AND component.profile_id=p_filters->>'graph_profile'),
                        CASE WHEN p_filters ? 'graph_profile' THEN 'unavailable' ELSE 'not_requested' END),
                    'score', graph_score
                ),
                'semantic', jsonb_build_object(
                    'status', COALESCE((SELECT component.status
                        FROM storage_v2_occurrence_score_component component
                       WHERE component.occurrence_id=ordered.id AND component.stage='semantic'
                         AND component.profile_id=p_filters->>'semantic_profile'),
                        CASE WHEN p_filters ? 'semantic_profile' THEN 'unavailable' ELSE 'not_requested' END),
                    'score', semantic_score
                ),
                'rerank', jsonb_build_object(
                    'status', COALESCE((SELECT component.status
                        FROM storage_v2_occurrence_score_component component
                       WHERE component.occurrence_id=ordered.id AND component.stage='rerank'
                         AND component.profile_id=p_filters->>'rerank_profile'),
                        CASE WHEN p_filters ? 'rerank_profile' THEN 'unavailable' ELSE 'not_requested' END),
                    'score', rerank_score
                ),
                'execution', 'complete_scoped_view_evaluation',
                'pruning', 'disabled_unsafe_bounds'
            ),
            'legacy_successors', COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'old_hit_id', mapping.old_hit_id,
                    'ordinal', mapping.ordinal,
                    'relation_kind', mapping.relation_kind
                ) ORDER BY mapping.old_hit_id, mapping.ordinal)
                  FROM legacy_hit_mapping mapping WHERE mapping.occurrence_id = ordered.id
            ), '[]'::JSONB)
        ) ORDER BY final_score DESC, lexical_sort_key NULLS LAST, external_hit_id, id) AS value FROM ordered
    )
    SELECT jsonb_build_object(
        'generation_seq', NULL,
        'execution', 'complete_scoped_view_evaluation',
        'fully_scored_views', (SELECT COUNT(*) FROM view_stats),
        'total', (SELECT COUNT(*) FROM ranked),
        'results', COALESCE((SELECT value FROM results), '[]'::JSONB)
    ) INTO v_result;

    IF EXISTS (
        SELECT 1 FROM occurrence occurrence_row
        JOIN artifact_version artifact ON artifact.id = occurrence_row.artifact_version_id
        JOIN sources source ON source.id = occurrence_row.source_id
        JOIN logical_source pointer ON pointer.id = source.id
        JOIN source_generation active_generation
          ON active_generation.id = pointer.active_generation_id
         AND active_generation.source_id = pointer.id
         AND active_generation.status = 'active'
        JOIN generation_item_version membership
          ON membership.source_id = occurrence_row.source_id
         AND membership.source_item_id = artifact.item_id
         AND membership.artifact_version_id = artifact.id
       WHERE (p_source_id IS NULL OR occurrence_row.source_id = p_source_id)
           AND storage_v2_can_access_source(occurrence_row.source_id, 'read')
           AND (p_include_test OR NOT source.is_test)
         AND membership.valid_from_seq <= active_generation.generation_seq
         AND (membership.valid_to_seq IS NULL
              OR membership.valid_to_seq > active_generation.generation_seq)
         AND (COALESCE(p_filters ->> 'path_prefix', '') = ''
              OR left(occurrence_row.source_path, char_length(p_filters ->> 'path_prefix'))
                 = p_filters ->> 'path_prefix')
         AND (COALESCE(p_filters ->> 'role', '') = ''
              OR occurrence_row.role = p_filters ->> 'role')
         AND (COALESCE(p_filters ->> 'occurred_from', '') = ''
              OR occurrence_row.occurred_at >= (p_filters ->> 'occurred_from')::TIMESTAMPTZ)
         AND (COALESCE(p_filters ->> 'occurred_to', '') = ''
              OR occurrence_row.occurred_at < (p_filters ->> 'occurred_to')::TIMESTAMPTZ)
         AND NOT EXISTS (
             SELECT 1 FROM storage_v2_search_view_document binding
              WHERE binding.view_id = occurrence_row.view_id
         )
    ) THEN
        RAISE EXCEPTION 'required lexical search document missing';
    END IF;
    RETURN v_result;
END
$function$

;

COMMIT;
