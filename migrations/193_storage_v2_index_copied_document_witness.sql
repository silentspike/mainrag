-- Give stored FTS and reconstructed vectors independent access paths.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT storage_v2_require_lexical_conversion_operator();
LOCK TABLE storage_v2_posting_conversion_contract,
           storage_v2_lexical_conversion_function_contract,
           storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

DO $copied_document_witness$
DECLARE
    matching_start CONSTANT TEXT := '), matching_document AS MATERIALIZED (';
    matching_end CONSTANT TEXT := '), fragment_targets AS MATERIALIZED (';
    matching_witness CONSTANT TEXT := $matching$), matching_document AS MATERIALIZED (
        -- Both paths are scoped to the authorized canonical documents.
        -- An independent indexable term keeps reconstructed body work out
        -- of the stored-vector access path.
        SELECT document.id
          FROM public.storage_v2_search_document document
         WHERE cardinality(p_occurrence_ids) >= 1024
           AND document.fts_simple @@ v_query
           AND (document.fts_simple_derived IS FALSE
                OR public.storage_v2_plain_document_fingerprints(p_query) IS NOT NULL)
           AND EXISTS (SELECT 1 FROM authorized_projection authorized
                       WHERE authorized.canonical_document_id = document.id)
        UNION ALL
        SELECT document.id
          FROM public.storage_v2_search_document document
         WHERE cardinality(p_occurrence_ids) >= 1024
           AND document.fts_simple_derived
           AND CASE WHEN public.storage_v2_plain_document_fingerprints(p_query) IS NOT NULL THEN
                 document.fts_simple IS NULL
                 AND document.fts_simple_fingerprints @> public.storage_v2_plain_document_fingerprints(p_query)
               ELSE TRUE END
           AND EXISTS (SELECT 1 FROM authorized_projection authorized
                       WHERE authorized.canonical_document_id = document.id)
           AND public.storage_v2_safe_tsvector(document.search_text) @@ v_query
    $matching$;
    target RECORD;
    definition TEXT;
    installed BYTEA;
    metadata JSONB;
    before_seal storage_v2_lexical_conversion_contract_seal%ROWTYPE;
    first_position INTEGER;
    last_position INTEGER;
BEGIN
    SELECT * INTO STRICT before_seal FROM storage_v2_lexical_conversion_contract_seal WHERE singleton;

    FOR target IN
        SELECT signature, decode(expected_definition_sha256, 'hex') AS predecessor
        FROM (VALUES
            ('storage_v2_source_segment_rank_candidates(bigint[],text)',
             '6184061512a2884e66746b4ddcbfe94d9f21577fe6a9543576ef75c378394e7e'),
            ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])',
             '0d31071759e538d2dd5ee16113f1a1c638c03777a726ea64274ff780e6a01109')
        ) AS expected(signature, expected_definition_sha256)
    LOOP
        SELECT pg_get_functiondef(p.oid), to_jsonb(p)-'prosrc'
          INTO STRICT definition, metadata
          FROM pg_proc p
         WHERE p.oid = target.signature::REGPROCEDURE
           AND p.proowner = 'mainrag_v2_frontier_owner'::REGROLE
           AND p.prosecdef AND p.proisstrict AND p.provolatile = 's';
        first_position := strpos(definition, matching_start);
        last_position := strpos(definition, matching_end);
        IF sha256(convert_to(definition,'UTF8')) IS DISTINCT FROM target.predecessor
           OR first_position = 0 OR last_position <= first_position
           OR (length(definition)-length(replace(definition,matching_start,''))) <> length(matching_start)
           OR (length(definition)-length(replace(definition,matching_end,''))) <> length(matching_end)
           OR EXISTS (SELECT 1 FROM storage_v2_posting_conversion_contract c
                      WHERE c.signature = target.signature) THEN
            RAISE EXCEPTION 'copied document witness rank predecessor differs: %', target.signature;
        END IF;

        EXECUTE overlay(definition PLACING matching_witness FROM first_position FOR last_position-first_position);
        SELECT sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')) INTO STRICT installed
          FROM pg_proc p
         WHERE p.oid = target.signature::REGPROCEDURE AND to_jsonb(p)-'prosrc' = metadata;
        UPDATE storage_v2_lexical_conversion_function_contract c SET definition_sha256 = installed
          WHERE c.signature = target.signature AND c.definition_sha256 = target.predecessor;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'copied document witness dependency differs: %', target.signature;
        END IF;
    END LOOP;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                   WHERE tgrelid = 'storage_v2_lexical_conversion_contract_seal'::REGCLASS
                     AND tgname = 'storage_v2_lexical_conversion_seal_immutable'
                     AND tgenabled = 'O' AND tgtype = 27
                     AND tgfoid = 'storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'copied document witness requires the active lexical seal guard';
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
    IF NOT FOUND THEN RAISE EXCEPTION 'copied document witness seal differs'; END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
END $copied_document_witness$;

SELECT storage_v2_require_lexical_conversion_operator();
COMMIT;
