-- Decode compact posting arrays once per block in bounded local memory.
-- Keep exact scope, duplicate term positions, frequencies and authority intact.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $guard$
DECLARE routine REGPROCEDURE := 'storage_v2_scoped_query_posting(bigint[],text[])'::REGPROCEDURE;
        required RECORD;
BEGIN
    IF encode(sha256(convert_to(pg_get_functiondef(routine),'UTF8')),'hex')
        <>'b0293a5422a0729fe76980004252c927aaf9c3b36ab634b3d76b22e5d71f765c' THEN
        RAISE EXCEPTION 'bounded compact decoder definition differs';
    END IF;
    IF (SELECT proowner FROM pg_proc WHERE oid=routine)<>'mainrag'::REGROLE
       OR (SELECT prosecdef OR NOT proisstrict OR provolatile<>'s' FROM pg_proc WHERE oid=routine)
       OR NOT has_function_privilege('mainrag',routine,'EXECUTE')
       OR EXISTS (SELECT 1 FROM pg_proc function CROSS JOIN LATERAL
            aclexplode(coalesce(function.proacl,acldefault('f',function.proowner))) permission
            WHERE function.oid=routine AND (permission.grantee<>'mainrag'::REGROLE
                OR permission.privilege_type<>'EXECUTE')) THEN
        RAISE EXCEPTION 'bounded compact decoder authority differs';
    END IF;
    FOR required IN SELECT * FROM (VALUES
        ('storage_v2_compact_posting_block_terms_check',
         'a4cdca0f1a4aae13746685bb008036894ba47692b45039c0a1492694a114f7f1'),
        ('storage_v2_compact_posting_block_check',
         'f9956d88b804475ce586bdcc42bb6427ba04ca427669291ff769f40a595bda5f')
    ) expected(name,definition_sha256) LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
            WHERE conrelid='storage_v2_compact_posting_block'::REGCLASS
              AND conname=required.name AND contype='c' AND convalidated
              AND encode(sha256(convert_to(pg_get_constraintdef(oid),'UTF8')),'hex')
                  =required.definition_sha256) THEN
            RAISE EXCEPTION 'bounded compact decoder bounds constraints differ';
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
        WHERE attrelid='storage_v2_compact_posting_block'::REGCLASS
          AND attname='terms' AND attnotnull AND atttypid='TEXT[]'::REGTYPE)
       OR NOT EXISTS (SELECT 1 FROM pg_attribute
        WHERE attrelid='storage_v2_compact_posting_block'::REGCLASS
          AND attname='term_frequencies' AND attnotnull AND atttypid='BIGINT[]'::REGTYPE) THEN
        RAISE EXCEPTION 'bounded compact decoder bounds constraints differ';
    END IF;
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
    v_block RECORD;
    v_block_terms TEXT[];
    v_block_frequencies BIGINT[];
    v_term TEXT;
    v_positions INTEGER[];
    v_position INTEGER;
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
    FOR v_block IN
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
    SELECT candidate.document_id, block.terms, block.term_frequencies
      FROM compact_candidate candidate
      CROSS JOIN LATERAL (
          SELECT block.terms,block.term_frequencies
            FROM public.storage_v2_compact_posting_block block
           WHERE block.document_id=candidate.document_id
             AND block.block_order=candidate.block_order OFFSET 0
      ) block
    LOOP
        -- Decode once in bounded per-block memory, outside SQL tuple caching.
        v_block_terms := v_block.terms || ARRAY[]::TEXT[];
        v_block_frequencies := v_block.term_frequencies || ARRAY[]::BIGINT[];
        FOREACH v_term IN ARRAY v_terms LOOP
            v_positions := array_positions(v_block_terms,v_term);
            FOREACH v_position IN ARRAY v_positions LOOP
                document_id := v_block.document_id;
                term := v_term;
                term_frequency := v_block_frequencies[v_position];
                RETURN NEXT;
            END LOOP;
        END LOOP;
    END LOOP;
END
$function$
;
COMMIT;
