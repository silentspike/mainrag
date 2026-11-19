-- Evaluate the copied legacy fallback's generated-zero guard in one query.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT storage_v2_require_lexical_conversion_operator();
LOCK TABLE storage_v2_posting_conversion_contract,
           storage_v2_lexical_conversion_function_contract,
           storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

DO $copied_generated_zero$
DECLARE
    legacy_helper CONSTANT REGPROCEDURE :=
        'public.storage_v2_source_legacy_segment_matches(bigint,text)'::REGPROCEDURE;
    legacy_definition CONSTANT BYTEA :=
        decode('1d6a1c96cc5e9de6d65869e0cd3fb7b2c79545f97f844f146f69de5869fb083e','hex');
    copied_result_marker CONSTANT TEXT := '), copied_result AS MATERIALIZED (';
    legacy_call CONSTANT TEXT := 'storage_v2_source_legacy_segment_matches(projection.occurrence_id,p_query)';
    generated_zero_cte CONSTANT TEXT := $generated$), copied_generated_zero AS MATERIALIZED (
        -- These are the exact two header-only branches of the legacy
        -- helper's early FALSE guard, applied to authorized projections.
        SELECT projection.occurrence_id
          FROM authorized_projection projection
         WHERE EXISTS (
                   SELECT 1 FROM public.storage_v2_lexical_segment generated
                   WHERE generated.occurrence_id = projection.occurrence_id
                     AND generated.segment_order = 0
                   UNION ALL
                   SELECT 1 FROM public.storage_v2_lexical_block_all block
                   WHERE block.occurrence_id = projection.occurrence_id
                     AND block.block_order = 0
                     AND 0 = ANY(block.segment_orders)
               )
    ), copied_result AS MATERIALIZED ($generated$;
    guarded_legacy_call CONSTANT TEXT := $guarded$CASE WHEN EXISTS (
            SELECT 1 FROM copied_generated_zero generated
            WHERE generated.occurrence_id = projection.occurrence_id
        ) THEN FALSE ELSE storage_v2_source_legacy_segment_matches(projection.occurrence_id,p_query) END$guarded$;
    target RECORD;
    definition TEXT;
    installed BYTEA;
    metadata JSONB;
    before_seal storage_v2_lexical_conversion_contract_seal%ROWTYPE;
BEGIN
    IF sha256(convert_to(pg_get_functiondef(legacy_helper),'UTF8')) IS DISTINCT FROM legacy_definition
       OR NOT EXISTS (
           SELECT 1 FROM pg_proc p
           WHERE p.oid = legacy_helper AND p.proowner = 'mainrag_v2_frontier_owner'::REGROLE
             AND p.prosecdef AND p.proisstrict AND p.provolatile = 's'
       ) THEN
        RAISE EXCEPTION 'copied generated-zero helper predecessor differs';
    END IF;
    SELECT * INTO STRICT before_seal FROM storage_v2_lexical_conversion_contract_seal WHERE singleton;

    FOR target IN
        SELECT signature, decode(expected_definition_sha256, 'hex') AS predecessor
        FROM (VALUES
            ('storage_v2_source_segment_rank_candidates(bigint[],text)',
             '207ea574f624b542723d2425ee32713085f0504be02bdec3e640123bbdda6e2d'),
            ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])',
             'ce54a0a19c08e36848d578a6e7acb90b85d8931ccaa6425be9137705a98fb6c0')
        ) AS expected(signature, expected_definition_sha256)
    LOOP
        SELECT pg_get_functiondef(p.oid), to_jsonb(p)-'prosrc'
          INTO STRICT definition, metadata
          FROM pg_proc p
         WHERE p.oid = target.signature::REGPROCEDURE
           AND p.proowner = 'mainrag_v2_frontier_owner'::REGROLE
           AND p.prosecdef AND p.proisstrict AND p.provolatile = 's';
        IF sha256(convert_to(definition,'UTF8')) IS DISTINCT FROM target.predecessor
           OR (length(definition)-length(replace(definition,copied_result_marker,''))) <> length(copied_result_marker)
           OR (length(definition)-length(replace(definition,legacy_call,''))) <> length(legacy_call)
           OR EXISTS (SELECT 1 FROM storage_v2_posting_conversion_contract c
                      WHERE c.signature = target.signature) THEN
            RAISE EXCEPTION 'copied generated-zero rank predecessor differs: %', target.signature;
        END IF;

        EXECUTE replace(replace(definition, copied_result_marker, generated_zero_cte),
                        legacy_call, guarded_legacy_call);
        SELECT sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')) INTO STRICT installed
          FROM pg_proc p
         WHERE p.oid = target.signature::REGPROCEDURE AND to_jsonb(p)-'prosrc' = metadata;
        UPDATE storage_v2_lexical_conversion_function_contract c SET definition_sha256 = installed
          WHERE c.signature = target.signature AND c.definition_sha256 = target.predecessor;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'copied generated-zero dependency differs: %', target.signature;
        END IF;
    END LOOP;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                   WHERE tgrelid = 'storage_v2_lexical_conversion_contract_seal'::REGCLASS
                     AND tgname = 'storage_v2_lexical_conversion_seal_immutable'
                     AND tgenabled = 'O' AND tgtype = 27
                     AND tgfoid = 'storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'copied generated-zero requires the active lexical seal guard';
    END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
    UPDATE storage_v2_lexical_conversion_contract_seal s
       SET contract_sha256 = sha256(convert_to(jsonb_build_object(
           'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature)
                          FROM storage_v2_lexical_conversion_function_contract c),
           'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid)
                          FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
     WHERE s.singleton AND to_jsonb(s) = to_jsonb(before_seal);
    IF NOT FOUND THEN RAISE EXCEPTION 'copied generated-zero seal differs'; END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
END $copied_generated_zero$;

SELECT storage_v2_require_lexical_conversion_operator();
COMMIT;
