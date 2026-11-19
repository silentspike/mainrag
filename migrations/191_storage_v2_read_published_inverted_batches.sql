-- Use published packed postings for words covered by the pinned generation.
BEGIN;
SET LOCAL search_path = pg_catalog, public, pg_temp;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT public.storage_v2_require_lexical_conversion_operator();

CREATE FUNCTION public.storage_v2_inverted_matching_generation(
    p_source_id BIGINT, p_occurrence_ids BIGINT[]
) RETURNS BIGINT
LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp SET row_security = on
SET plan_cache_mode = force_custom_plan SET jit = off AS $matching$
DECLARE
    candidate_generation BIGINT;
BEGIN
    IF public.storage_v2_can_access_source(p_source_id,'read') IS DISTINCT FROM TRUE
       OR NOT public.storage_v2_inverted_source_enabled(p_source_id) THEN
        RETURN NULL;
    END IF;
    SELECT manifest.generation_id INTO candidate_generation
      FROM public.storage_v2_inverted_generation manifest
     WHERE manifest.source_id = p_source_id
       AND manifest.published_at IS NOT NULL
       AND public.storage_v2_inverted_generation_ready(manifest.generation_id)
     ORDER BY manifest.generation_id DESC LIMIT 1;
    IF candidate_generation IS NULL THEN
        RETURN NULL;
    END IF;

    -- A retained conversion or later ingest can add a derived block after a
    -- generation was sealed. Scope the anti-join to the actual request.
    IF EXISTS (
        WITH requested AS MATERIALIZED (
            SELECT DISTINCT id FROM unnest(p_occurrence_ids) input(id) WHERE id IS NOT NULL
        )
        SELECT 1 FROM requested
        JOIN public.storage_v2_derived_lexical_block live ON live.occurrence_id = requested.id
        WHERE live.source_id = p_source_id
          AND NOT EXISTS (
            SELECT 1 FROM public.storage_v2_inverted_block packed
            WHERE packed.generation_id = candidate_generation
              AND (packed.occurrence_id, packed.block_order, packed.source_id, packed.artifact_version_id)
                = (live.occurrence_id, live.block_order, live.source_id, live.artifact_version_id)
          )
    ) THEN
        RETURN NULL;
    END IF;
    RETURN candidate_generation;
END $matching$;

CREATE FUNCTION public.storage_v2_inverted_word_first_candidates(
    p_generation_id BIGINT, p_source_id BIGINT, p_occurrence_ids BIGINT[], p_query TEXT
) RETURNS TABLE (
    occurrence_id BIGINT, source_id BIGINT, artifact_version_id BIGINT,
    segment_order BIGINT, lexical_score REAL
)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp SET row_security = on
SET plan_cache_mode = force_custom_plan SET jit = off AS $read$
WITH word AS MATERIALIZED (
    SELECT (tsvector_to_array(to_tsvector('simple',p_query)))[1] AS lexeme,
           websearch_to_tsquery('simple',p_query) AS predicate
     WHERE public.storage_v2_can_access_source(p_source_id,'read')
       AND p_query ~ '^[[:alnum:]_]+$' AND lower(p_query) NOT IN ('and','or','not')
       AND numnode(websearch_to_tsquery('simple',p_query)) = 1
       AND cardinality(tsvector_to_array(to_tsvector('simple',p_query))) = 1
), key AS MATERIALIZED (
    SELECT word.predicate, public.digest(word.lexeme,'sha256') AS hash FROM word
), buckets AS MATERIALIZED (
    SELECT packed.batch_no,
           substring(packed.postings FROM 1 FOR octet_length(packed.postings)) AS postings,
           get_byte(key.hash,1) AS low_key, substring(key.hash FROM 3 FOR 2) AS suffix,
           key.predicate
      FROM key JOIN public.storage_v2_inverted_prefix packed ON packed.prefix = get_byte(key.hash,0)
      JOIN public.storage_v2_inverted_generation manifest USING (generation_id)
     WHERE packed.generation_id = p_generation_id AND manifest.source_id = p_source_id
       AND manifest.published_at IS NOT NULL
), entries AS MATERIALIZED (
    SELECT bucket.batch_no,
           256 * get_byte(bucket.postings,slot.byte_offset+1)
               + get_byte(bucket.postings,slot.byte_offset+2) AS block_id,
           get_byte(bucket.postings,slot.byte_offset+5)+1 AS ordinal, bucket.predicate
      FROM buckets bucket
     CROSS JOIN LATERAL generate_series(0,octet_length(bucket.postings)-6,6) AS slot(byte_offset)
     WHERE get_byte(bucket.postings,slot.byte_offset) = bucket.low_key
       AND (get_byte(bucket.postings,slot.byte_offset+5) = 255
            OR substring(bucket.postings FROM slot.byte_offset+4 FOR 2) = bucket.suffix)
), checked AS MATERIALIZED (
    SELECT live.occurrence_id,live.source_id,live.artifact_version_id,live.block_order,
           live.segment_orders,live.text_byte_starts,live.text_byte_lengths,live.context_prefixes,
           live.chunk_types,entry.ordinal,entry.predicate,
           CASE WHEN entry.ordinal BETWEEN 1 AND cardinality(live.segment_orders) THEN
              (public.storage_v2_derived_lexical_vectors(live.occurrence_id,
                 ARRAY[live.text_byte_starts[entry.ordinal]], ARRAY[live.text_byte_lengths[entry.ordinal]],
                 ARRAY[live.context_prefixes[entry.ordinal]], ARRAY[live.chunk_types[entry.ordinal]]))[1]
                 @@ entry.predicate
           END AS verified
      FROM entries entry
      JOIN public.storage_v2_inverted_block packed
        ON packed.generation_id = p_generation_id
       AND packed.batch_no = entry.batch_no AND packed.block_id = entry.block_id
      JOIN public.storage_v2_derived_lexical_block live
        ON (live.occurrence_id,live.block_order,live.source_id,live.artifact_version_id)
         = (packed.occurrence_id,packed.block_order,packed.source_id,packed.artifact_version_id)
     WHERE live.source_id = p_source_id AND live.occurrence_id = ANY(p_occurrence_ids)
), fallback_input AS MATERIALIZED (
    SELECT * FROM checked WHERE checked.verified IS DISTINCT FROM TRUE
), hits AS (
    SELECT checked.occurrence_id,checked.source_id,checked.artifact_version_id,
           checked.segment_orders[checked.ordinal] AS segment_order
      FROM checked WHERE checked.verified
    UNION ALL
    SELECT pending.occurrence_id,pending.source_id,pending.artifact_version_id,part.segment_order
      FROM fallback_input pending
     CROSS JOIN LATERAL unnest(pending.segment_orders,
        public.storage_v2_derived_lexical_vectors(pending.occurrence_id,
            pending.text_byte_starts,pending.text_byte_lengths,
            pending.context_prefixes,pending.chunk_types)) AS part(segment_order,vector)
     WHERE part.vector @@ pending.predicate
)
SELECT hits.occurrence_id,hits.source_id,hits.artifact_version_id,min(hits.segment_order),0.0::REAL
  FROM hits GROUP BY hits.occurrence_id,hits.source_id,hits.artifact_version_id
$read$;

GRANT SELECT ON public.storage_v2_inverted_generation,public.storage_v2_inverted_block
    TO mainrag_v2_frontier_owner;
GRANT EXECUTE ON FUNCTION public.storage_v2_inverted_generation_ready(BIGINT) TO mainrag_v2_frontier_owner;
ALTER FUNCTION public.storage_v2_inverted_matching_generation(BIGINT,BIGINT[]) OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION public.storage_v2_inverted_word_first_candidates(BIGINT,BIGINT,BIGINT[],TEXT)
    OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION public.storage_v2_inverted_matching_generation(BIGINT,BIGINT[]),
    public.storage_v2_inverted_word_first_candidates(BIGINT,BIGINT,BIGINT[],TEXT) FROM PUBLIC,mainrag;
GRANT EXECUTE ON FUNCTION public.storage_v2_inverted_matching_generation(BIGINT,BIGINT[]),
    public.storage_v2_inverted_word_first_candidates(BIGINT,BIGINT,BIGINT[],TEXT) TO mainrag_v2_frontier_owner;

LOCK TABLE storage_v2_lexical_conversion_function_contract,
           storage_v2_lexical_conversion_catalog_contract,
           storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

DO $route$
DECLARE
    target CONSTANT TEXT := 'storage_v2_derived_lexical_first_candidates(bigint[],bigint[],text)';
    predecessor CONSTANT BYTEA := decode('8b1baca8da3e7643f99432021beafb52a31e0b8bc098e8675661045b5c07e38d','hex');
    declaration CONSTANT TEXT := 'DECLARE sources BIGINT[]; requested RECORD; block RECORD; item RECORD;';
    source_guard CONSTANT TEXT := ' IF sources IS NULL THEN RETURN; END IF;';
    route_context CONSTANT TEXT := $route_context$
 IF cardinality(sources)=1 AND p_query ~ '^[[:alnum:]_]+$'
    AND lower(p_query) NOT IN ('and','or','not')
    AND numnode(query)=1
    AND cardinality(tsvector_to_array(to_tsvector('simple',p_query)))=1 THEN
  v_inverted := public.storage_v2_inverted_matching_generation(sources[1],p_occurrence_ids);
  IF v_inverted IS NOT NULL THEN
   RETURN QUERY SELECT * FROM public.storage_v2_inverted_word_first_candidates(
     v_inverted,sources[1],p_occurrence_ids,p_query);
   RETURN;
  END IF;
 END IF;$route_context$;
    definition TEXT;
    metadata JSONB;
    before_seal storage_v2_lexical_conversion_contract_seal%ROWTYPE;
    installed BYTEA;
    routine RECORD;
    dependency TEXT;
    target_relation REGCLASS;
BEGIN
    SELECT pg_get_functiondef(p.oid),to_jsonb(p)-'prosrc' INTO STRICT definition,metadata
      FROM pg_proc p WHERE p.oid=target::REGPROCEDURE AND p.proowner='mainrag_v2_frontier_owner'::REGROLE;
    IF sha256(convert_to(definition,'UTF8')) IS DISTINCT FROM predecessor
       OR length(definition)-length(replace(definition,declaration,''))<>length(declaration)
       OR length(definition)-length(replace(definition,source_guard,''))<>length(source_guard)
       OR EXISTS(SELECT 1 FROM storage_v2_posting_conversion_contract p WHERE p.signature=target) THEN
        RAISE EXCEPTION 'inverted first reader predecessor differs';
    END IF;
    SELECT * INTO STRICT before_seal FROM storage_v2_lexical_conversion_contract_seal WHERE singleton;
    definition:=replace(definition,declaration,declaration||E'\n        v_inverted BIGINT;');
    EXECUTE replace(definition,source_guard,source_guard||route_context);
    SELECT sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')) INTO STRICT installed
      FROM pg_proc p WHERE p.oid=target::REGPROCEDURE AND to_jsonb(p)-'prosrc'=metadata;
    INSERT INTO storage_v2_lexical_conversion_function_contract AS pin
        (signature,definition_sha256,owner_oid,acl_sha256)
    SELECT target,installed,p.proowner,sha256(convert_to(coalesce(p.proacl::TEXT,''),'UTF8'))
      FROM pg_proc p WHERE p.oid=target::REGPROCEDURE
    ON CONFLICT (signature) DO UPDATE SET definition_sha256=installed
     WHERE pin.definition_sha256=predecessor AND pin.owner_oid=excluded.owner_oid
       AND pin.acl_sha256=excluded.acl_sha256;
    IF NOT FOUND THEN RAISE EXCEPTION 'inverted first reader dependency differs'; END IF;

    FOREACH dependency IN ARRAY ARRAY[
        'storage_v2_guard_inverted_generation_mutation()',
        'storage_v2_append_inverted_batch(bigint,integer,integer)',
        'storage_v2_publish_inverted_generation(bigint,integer)',
        'storage_v2_inverted_generation_ready(bigint)',
        'storage_v2_inverted_matching_generation(bigint,bigint[])',
        'storage_v2_inverted_word_first_candidates(bigint,bigint,bigint[],text)'
    ] LOOP
        SELECT p.oid,p.proowner,p.proacl INTO STRICT routine FROM pg_proc p WHERE p.oid=dependency::REGPROCEDURE;
        INSERT INTO storage_v2_lexical_conversion_function_contract(signature,definition_sha256,owner_oid,acl_sha256)
        VALUES(dependency,sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8')),routine.proowner,
               sha256(convert_to(coalesce(routine.proacl::TEXT,''),'UTF8')));
    END LOOP;
    FOREACH target_relation IN ARRAY ARRAY[
        'public.storage_v2_inverted_generation'::REGCLASS,
        'public.storage_v2_inverted_batch'::REGCLASS,
        'public.storage_v2_inverted_block'::REGCLASS,
        'public.storage_v2_inverted_prefix'::REGCLASS
    ] LOOP
        INSERT INTO storage_v2_lexical_conversion_catalog_contract(relation_oid,identity_sha256)
        VALUES(target_relation,storage_v2_posting_conversion_relation_sha256(target_relation));
    END LOOP;

    IF NOT EXISTS(SELECT 1 FROM pg_trigger
        WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
          AND tgname='storage_v2_lexical_conversion_seal_immutable' AND tgenabled='O' AND tgtype=27
          AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'inverted first reader requires the active lexical seal guard';
    END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
    UPDATE storage_v2_lexical_conversion_contract_seal s
       SET function_count=(SELECT count(*) FROM storage_v2_lexical_conversion_function_contract),
           catalog_count=(SELECT count(*) FROM storage_v2_lexical_conversion_catalog_contract),
           contract_sha256=sha256(convert_to(jsonb_build_object(
               'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature)
                              FROM storage_v2_lexical_conversion_function_contract c),
               'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid)
                              FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
     WHERE s.singleton AND to_jsonb(s)=to_jsonb(before_seal);
    IF NOT FOUND THEN RAISE EXCEPTION 'inverted first reader seal differs'; END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
END $route$;
SELECT public.storage_v2_require_lexical_conversion_operator();
COMMIT;
