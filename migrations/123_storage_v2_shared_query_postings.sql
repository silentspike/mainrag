-- Share posting scope and compact-block work across all query terms. The
-- covering digest index avoids fetching unrelated full terms from the heap.
-- Existing single-term readers and their indexes remain supported.
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

DO $function_guard$
DECLARE routine_oid OID := to_regprocedure('public.storage_v2_scoped_query_posting(bigint[],text[])');
BEGIN
 IF routine_oid IS NULL THEN RETURN; END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_proc routine
      WHERE routine.oid=routine_oid
        AND encode(sha256(convert_to(routine.prosrc,'UTF8')),'hex')='b92bb66db029c18b5f2a9454011658ea05750f5a00a2d5bdd42774a42136ef46'
        AND routine.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')
        AND routine.provolatile='s' AND routine.proisstrict AND NOT routine.prosecdef
        AND routine.proretset AND routine.prorettype='record'::regtype
        AND routine.proallargtypes=ARRAY['bigint[]'::regtype,'text[]'::regtype,
          'bigint'::regtype,'text'::regtype,'bigint'::regtype]::oid[]
        AND routine.proargnames=ARRAY['p_document_ids','p_terms','document_id','term','term_frequency']
        AND routine.proconfig=ARRAY['search_path=pg_catalog, public, pg_temp','plan_cache_mode=force_custom_plan','jit=off']
        AND routine.proowner='mainrag'::regrole) THEN
  RAISE EXCEPTION 'shared query posting definition differs';
 END IF;
 IF (NOT has_function_privilege('mainrag',routine_oid,'EXECUTE')
      OR EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
        aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission
        WHERE routine.oid=routine_oid
          AND (permission.grantee<>'mainrag'::regrole OR permission.privilege_type<>'EXECUTE'
            OR (permission.is_grantable AND permission.grantee<>routine.proowner)))) THEN
  RAISE EXCEPTION 'shared query posting authority differs';
 END IF;
END $function_guard$;

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
    ), compact_candidate AS MATERIALIZED (
        -- Identify blocks without expanding or carrying their payload arrays.
        SELECT block.document_id,block.block_order
          FROM public.storage_v2_compact_posting_block block
         WHERE block.fingerprints && public.storage_v2_posting_fingerprints(v_terms)
           AND EXISTS (SELECT 1 FROM requested WHERE requested.id=block.document_id)
    )
    SELECT candidate.document_id,posting.term,candidate.term_frequency::BIGINT
      FROM scoped_candidate candidate
      CROSS JOIN LATERAL (
          SELECT posting.term FROM public.storage_v2_search_posting posting
           WHERE posting.document_id=candidate.document_id
             AND posting.term_sha256=candidate.term_sha256 OFFSET 0
      ) posting
     WHERE posting.term=ANY(v_terms)
    UNION ALL
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
ALTER FUNCTION public.storage_v2_scoped_query_posting(bigint[],text[]) OWNER TO mainrag;
REVOKE ALL ON FUNCTION public.storage_v2_scoped_query_posting(bigint[],text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.storage_v2_scoped_query_posting(bigint[],text[]) TO mainrag;

DO $reader_guard$ BEGIN
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::regprocedure),'UTF8')),'hex')
      NOT IN ('6fdcdc0304c64fd58504a691cab9ed1eb5588573ab1b1db7f085597d67b85a3d','2701a549f0a210b9f4ad6ebea6b7483e528711d9d47effe93048b5802091a0a6') THEN
  RAISE EXCEPTION 'shared query reader identity differs';
 END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::regprocedure)<>'mainrag'::regrole
    OR EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
      aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission
      WHERE routine.oid='storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::regprocedure
        AND (permission.grantee NOT IN (0::oid,'mainrag'::regrole::oid) OR permission.privilege_type<>'EXECUTE'
          OR (permission.is_grantable AND permission.grantee<>routine.proowner))) THEN
  RAISE EXCEPTION 'shared query reader authority differs';
 END IF;
 IF NOT has_function_privilege('mainrag','storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)','EXECUTE') THEN
  RAISE EXCEPTION 'shared query reader authority differs';
 END IF;
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::regprocedure),'UTF8')),'hex')
      NOT IN ('2e220670132d76e12bfed23aa935fcb5d5ab2a5659ed04cf9ce34542196874e3','6b6dfbc62add876aa498a3a8beb31c7c68d891206806c3b3a17d250112a266b7') THEN
  RAISE EXCEPTION 'shared query reader identity differs';
 END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::regprocedure)<>'mainrag'::regrole
    OR EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
      aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission
      WHERE routine.oid='storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::regprocedure
        AND (permission.grantee<>'mainrag'::regrole OR permission.privilege_type<>'EXECUTE'
          OR (permission.is_grantable AND permission.grantee<>routine.proowner))) THEN
  RAISE EXCEPTION 'shared query reader authority differs';
 END IF;
 IF NOT has_function_privilege('mainrag','storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)','EXECUTE') THEN
  RAISE EXCEPTION 'shared query reader authority differs';
 END IF;
END $reader_guard$;

DO $readers$
DECLARE
 definition TEXT;
 old_probe TEXT := $old$          CROSS JOIN unnest(query.terms) requested_term(value)
          CROSS JOIN LATERAL storage_v2_scoped_term_posting(
              ARRAY(SELECT document_id FROM scoped_document),requested_term.value
          ) posting$old$;
 new_probe TEXT := $new$          CROSS JOIN LATERAL storage_v2_scoped_query_posting(
              ARRAY(SELECT document_id FROM scoped_document),query.terms
          ) posting$new$;
BEGIN
 definition:=pg_get_functiondef('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::regprocedure);
 IF strpos(definition,old_probe)>0 THEN
  IF (length(definition)-length(replace(definition,old_probe,'')))/length(old_probe)<>1 THEN
   RAISE EXCEPTION 'shared query reader probe differs';
  END IF;
  EXECUTE replace(definition,old_probe,new_probe);
 END IF;
 definition:=pg_get_functiondef('storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::regprocedure);
 IF strpos(definition,old_probe)>0 THEN
  IF (length(definition)-length(replace(definition,old_probe,'')))/length(old_probe)<>1 THEN
   RAISE EXCEPTION 'shared query reader probe differs';
  END IF;
  EXECUTE replace(definition,old_probe,new_probe);
 END IF;
END $readers$;

COMMIT;
