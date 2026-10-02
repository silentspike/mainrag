-- Bound broad reader planning and avoid empty native/request work.
-- Preserve exact rows, canonical provenance, forced RLS and existing grants.
BEGIN;

DO $retained_guard$
DECLARE definition TEXT; valid BOOLEAN;
BEGIN
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_source_segment_rank_candidates(bigint[],text)'::regprocedure),'UTF8')),'hex')
      <> 'e1c5046f3a347135454f6bb980330649212bbf27c47090e2e24951e47668b42e' THEN
  RAISE EXCEPTION 'candidate helper identity differs';
 END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_source_segment_rank_candidates(bigint[],text)'::regprocedure)
      <> 'mainrag_v2_frontier_owner'::regrole
    OR EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
      aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission
      WHERE routine.oid='storage_v2_source_segment_rank_candidates(bigint[],text)'::regprocedure
        AND (permission.grantee NOT IN ('mainrag_v2_frontier_owner'::regrole,'mainrag'::regrole)
          OR permission.privilege_type<>'EXECUTE'
          OR (permission.is_grantable AND permission.grantee<>routine.proowner))) THEN
  RAISE EXCEPTION 'candidate helper authority differs';
 END IF;
 SELECT pg_get_indexdef(indexrelid),indisvalid AND indisready INTO definition,valid
   FROM pg_index WHERE indexrelid='public.idx_storage_v2_fragmented_artifact_occurrence'::regclass;
 IF valid IS DISTINCT FROM TRUE OR definition IS DISTINCT FROM
    'CREATE INDEX idx_storage_v2_fragmented_artifact_occurrence ON public.occurrence USING btree (id) WHERE ((role = ''artifact''::text) AND (locator @> ''{"fragmented": true}''::jsonb))' THEN
  RAISE EXCEPTION 'fragmented artifact occurrence index identity differs';
 END IF;
END $retained_guard$;
DO $index_guard$
DECLARE definition TEXT; valid BOOLEAN;
BEGIN
 IF (SELECT relowner FROM pg_class WHERE oid='public.storage_v2_search_posting'::regclass)
      <> 'mainrag'::regrole THEN
  RAISE EXCEPTION 'query posting index authority differs';
 END IF;
 IF to_regclass('public.idx_storage_v2_posting_query_scope') IS NULL THEN
  CREATE INDEX idx_storage_v2_posting_query_scope
    ON public.storage_v2_search_posting(term_sha256,document_id) INCLUDE(term_frequency);
 END IF;
 SELECT pg_get_indexdef(indexrelid),indisvalid AND indisready
   INTO definition,valid FROM pg_index
  WHERE indexrelid='public.idx_storage_v2_posting_query_scope'::regclass;
 IF valid IS DISTINCT FROM TRUE OR definition IS DISTINCT FROM
    'CREATE INDEX idx_storage_v2_posting_query_scope ON public.storage_v2_search_posting USING btree (term_sha256, document_id) INCLUDE (term_frequency)'
    OR (SELECT relowner FROM pg_class WHERE oid='public.idx_storage_v2_posting_query_scope'::regclass)
       <> 'mainrag'::regrole THEN
  RAISE EXCEPTION 'query posting index identity differs';
 END IF;
END $index_guard$;
DO $guard$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_proc p WHERE p.oid='storage_v2_scoped_query_posting(bigint[],text[])'::regprocedure
   AND p.proowner='mainrag'::regrole AND p.proconfig=ARRAY['search_path=pg_catalog, public, pg_temp','plan_cache_mode=force_custom_plan','jit=off']
   AND p.provolatile='s' AND p.proisstrict AND p.proretset
   AND p.prosecdef=FALSE
   AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')
   AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex') IN ('6257bca0e54b188b88c7099cb2a1c65cb4d344d35e3f61d594b4d6331239e009','606ed6188559320293bdf7ee0831a20b062f94ea3f41d52e649b865e842ef423')) THEN
  RAISE EXCEPTION 'bounded reader definition differs: storage_v2_scoped_query_posting(bigint[],text[])';
 END IF;
 IF NOT has_function_privilege('mainrag','storage_v2_scoped_query_posting(bigint[],text[])','EXECUTE') OR EXISTS(
  SELECT required.role_id FROM unnest(ARRAY['mainrag'::regrole]) required(role_id)
   WHERE NOT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL
    aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
    WHERE p.oid='storage_v2_scoped_query_posting(bigint[],text[])'::regprocedure AND a.grantee=required.role_id AND a.privilege_type='EXECUTE')) OR EXISTS(
  SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
  WHERE p.oid='storage_v2_scoped_query_posting(bigint[],text[])'::regprocedure AND (a.grantee NOT IN ('mainrag'::regrole)
   OR a.privilege_type<>'EXECUTE' OR (a.is_grantable AND a.grantee<>p.proowner))) THEN
  RAISE EXCEPTION 'bounded reader authority differs: storage_v2_scoped_query_posting(bigint[],text[])';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_proc p WHERE p.oid='storage_v2_source_segment_presence(bigint[])'::regprocedure
   AND p.proowner='mainrag_v2_frontier_owner'::regrole AND p.proconfig=ARRAY['search_path=pg_catalog, public','row_security=on']
   AND p.provolatile='s' AND p.proisstrict AND p.proretset
   AND p.prosecdef=TRUE
   AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')
   AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex') IN ('ba6bcb09d55da99be98526624de50877352ee810d2b1e6216f5efa55b17691b2','f3559a12f32ac3f25b6698b397dcb36a78cefc8dfba1b7094624bfa8b58d1d0b')) THEN
  RAISE EXCEPTION 'bounded reader definition differs: storage_v2_source_segment_presence(bigint[])';
 END IF;
 IF NOT has_function_privilege('mainrag_v2_frontier_owner','storage_v2_source_segment_presence(bigint[])','EXECUTE') OR EXISTS(
  SELECT required.role_id FROM unnest(ARRAY['mainrag_v2_frontier_owner'::regrole,'mainrag'::regrole]) required(role_id)
   WHERE NOT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL
    aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
    WHERE p.oid='storage_v2_source_segment_presence(bigint[])'::regprocedure AND a.grantee=required.role_id AND a.privilege_type='EXECUTE')) OR EXISTS(
  SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
  WHERE p.oid='storage_v2_source_segment_presence(bigint[])'::regprocedure AND (a.grantee NOT IN ('mainrag_v2_frontier_owner'::regrole,'mainrag'::regrole)
   OR a.privilege_type<>'EXECUTE' OR (a.is_grantable AND a.grantee<>p.proowner))) THEN
  RAISE EXCEPTION 'bounded reader authority differs: storage_v2_source_segment_presence(bigint[])';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_proc p WHERE p.oid='storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)'::regprocedure
   AND p.proowner='mainrag_v2_lexical_rank_owner'::regrole AND p.proconfig=ARRAY['search_path=pg_catalog, public, pg_temp','row_security=on','plan_cache_mode=force_custom_plan']
   AND p.provolatile='s' AND p.proisstrict AND p.proretset
   AND p.prosecdef=TRUE
   AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')
   AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex') IN ('341cb9c60afb57dcce6bc47dbbdd7e1858d5fc15734566e4aaa33f9b05e4d715','2b633d8ec29cd30a6d0f726279d6a2e25c62db89c7c44457c92558a0d96172bd')) THEN
  RAISE EXCEPTION 'bounded reader definition differs: storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)';
 END IF;
 IF NOT has_function_privilege('mainrag_v2_lexical_rank_owner','storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)','EXECUTE') OR EXISTS(
  SELECT required.role_id FROM unnest(ARRAY['mainrag_v2_lexical_rank_owner'::regrole,'mainrag_v2_frontier_owner'::regrole]) required(role_id)
   WHERE NOT EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL
    aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
    WHERE p.oid='storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)'::regprocedure AND a.grantee=required.role_id AND a.privilege_type='EXECUTE')) OR EXISTS(
  SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
  WHERE p.oid='storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)'::regprocedure AND (a.grantee NOT IN ('mainrag_v2_lexical_rank_owner'::regrole,'mainrag_v2_frontier_owner'::regrole)
   OR a.privilege_type<>'EXECUTE' OR (a.is_grantable AND a.grantee<>p.proowner))) THEN
  RAISE EXCEPTION 'bounded reader authority differs: storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)';
 END IF;
END $guard$;
CREATE OR REPLACE FUNCTION public.storage_v2_scoped_query_posting(p_document_ids bigint[], p_terms text[])
 RETURNS TABLE(document_id bigint, term text, term_frequency bigint)
 LANGUAGE plpgsql
 STABLE STRICT
 SET search_path TO 'pg_catalog', 'public', 'pg_temp'
 SET plan_cache_mode TO 'force_custom_plan'
 SET jit TO 'off'
AS $function$
DECLARE
    v_terms TEXT[];
    v_hashes BYTEA[];
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
    ), scoped_candidate AS MATERIALIZED (
        SELECT candidate.* FROM ordinary_candidate candidate
          JOIN requested ON requested.id=candidate.document_id
    )
    SELECT candidate.document_id,posting.term,candidate.term_frequency::BIGINT
      FROM scoped_candidate candidate
      CROSS JOIN LATERAL (
          SELECT posting.term FROM public.storage_v2_search_posting posting
           WHERE posting.document_id=candidate.document_id
             AND posting.term_sha256=candidate.term_sha256 OFFSET 0
      ) posting
     WHERE posting.term=ANY(v_terms);
    -- A source with only ordinary postings needs no global compact term scan.
    IF NOT EXISTS (
        SELECT 1 FROM unnest(p_document_ids) requested(id)
          CROSS JOIN LATERAL (
              SELECT 1 FROM public.storage_v2_compact_posting_block block
               WHERE block.document_id=requested.id OFFSET 0
          ) present
    ) THEN RETURN; END IF;
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_document_ids) input(id)
    ), matching_block AS MATERIALIZED (
        -- Materialize narrow candidate keys before joining the document scope.
        -- This prevents one fingerprint probe for every requested document.
        SELECT block.document_id,block.block_order
          FROM public.storage_v2_compact_posting_block block
         WHERE block.fingerprints && public.storage_v2_posting_fingerprints(v_terms)
    ), compact_candidate AS MATERIALIZED (
        SELECT block.* FROM matching_block block
          JOIN requested ON requested.id=block.document_id
    )
    SELECT candidate.document_id,requested_term.value,
           block.term_frequencies[array_lower(block.term_frequencies,1)
               + position.ordinal-array_lower(block.terms,1)]
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
CREATE OR REPLACE FUNCTION public.storage_v2_source_segment_presence(p_occurrence_ids bigint[])
 RETURNS TABLE(occurrence_id bigint)
 LANGUAGE plpgsql
 STABLE STRICT SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
 SET row_security TO 'on'
AS $function$
DECLARE
    v_authorized_sources BIGINT[];
BEGIN
    SELECT array_agg(source.id) INTO v_authorized_sources FROM public.sources source
     WHERE storage_v2_can_access_source(source.id,'read');
    IF v_authorized_sources IS NULL THEN RETURN; END IF;
    -- One authorized requested item yields one presence row. Presence does not
    -- need to enumerate all of its immutable lexical segments.
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    )
    SELECT occurrence_row.id
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
     WHERE occurrence_row.source_id=ANY(v_authorized_sources)
       AND (EXISTS (
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
           ));
END
$function$;
CREATE OR REPLACE FUNCTION public.storage_v2_authorized_lexical_candidates(p_occurrence_ids bigint[], p_source_ids bigint[], p_query text)
 RETURNS TABLE(occurrence_id bigint, source_id bigint, artifact_version_id bigint, segment_order bigint, lexical_score real)
 LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
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
    RETURN QUERY
    WITH authorized_sources AS MATERIALIZED (
        SELECT source.id FROM public.sources source
         WHERE source.id=ANY(v_source_ids)
           AND public.storage_v2_can_access_source(source.id,'read')
    ), requested AS MATERIALIZED (
        SELECT id FROM unnest(p_occurrence_ids) AS input(id)
    ), matching AS MATERIALIZED (
        SELECT segment.occurrence_id,segment.source_id,segment.artifact_version_id,
               segment.segment_order
          FROM public.storage_v2_lexical_segment segment
         WHERE segment.fts_vector@@v_query
           AND segment.source_id IN (SELECT id FROM authorized_sources)
           AND segment.source_id=ANY(v_source_ids)
    ), eligible AS MATERIALIZED (
        SELECT matching.* FROM matching
          -- Avoid expanding a large requested set when exact native matches are absent.
          WHERE EXISTS (SELECT 1 FROM matching)
            AND EXISTS (SELECT 1 FROM requested WHERE requested.id=matching.occurrence_id)
    )
    SELECT eligible.occurrence_id,eligible.source_id,eligible.artifact_version_id,
           eligible.segment_order,0.0::REAL FROM eligible;
    END IF;
    IF v_has_compact THEN
    RETURN QUERY
    WITH authorized_sources AS MATERIALIZED (
        SELECT source.id FROM public.sources source
         WHERE source.id=ANY(v_source_ids)
           AND public.storage_v2_can_access_source(source.id,'read')
    ), requested AS MATERIALIZED (
        SELECT id FROM unnest(p_occurrence_ids) input(id)
    ), eligible_blocks AS MATERIALIZED (
        SELECT block.* FROM public.storage_v2_compact_lexical_block block
         WHERE block.source_id=ANY(v_source_ids)
           AND block.source_id IN (SELECT id FROM authorized_sources)
           AND EXISTS (SELECT 1 FROM requested WHERE requested.id=block.occurrence_id)
           AND CASE WHEN p_query ~ '^[[:alnum:]_]+([[:space:]]+[[:alnum:]_]+)*$'
                         AND lower(p_query) !~ '(^|[[:space:]])or([[:space:]]|$)'
                    THEN block.fingerprints @> public.storage_v2_posting_fingerprints(
                             tsvector_to_array(to_tsvector('simple',p_query)))
                    ELSE TRUE END
    )
    SELECT block.occurrence_id,block.source_id,block.artifact_version_id,
           item.segment_order,0.0::REAL
      FROM eligible_blocks block
      CROSS JOIN LATERAL unnest(block.segment_orders,block.fts_vectors)
          item(segment_order,fts_vector)
     WHERE item.fts_vector@@v_query;
    END IF;
END
$function$;
COMMIT;
