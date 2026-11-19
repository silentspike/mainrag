-- Filter fingerprint-selected blocks by the requested occurrence set.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT storage_v2_require_lexical_conversion_operator();
LOCK TABLE storage_v2_lexical_conversion_function_contract,
           storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

DO $candidate_scope$
DECLARE
    target_signature CONSTANT TEXT := 'storage_v2_derived_lexical_candidate_occurrences(bigint[],bigint[],integer[])';
    predecessor CONSTANT BYTEA := decode('a5c7eb3bb02f2a49d1e4e8f2fbbfa749f4d6b11590fcf4a5052b1e71c384f96e','hex');
    old_filter CONSTANT TEXT := 'matching.occurrence_id = ANY((SELECT p_occurrence_ids OFFSET 0)::BIGINT[])';
    new_filter CONSTANT TEXT := 'matching.occurrence_id IN (SELECT id FROM unnest(p_occurrence_ids) requested(id))';
    definition TEXT;
    installed BYTEA;
    metadata JSONB;
    before_seal storage_v2_lexical_conversion_contract_seal%ROWTYPE;
BEGIN
    SELECT pg_get_functiondef(p.oid), to_jsonb(p)-'prosrc'
      INTO STRICT definition, metadata
      FROM pg_proc p WHERE p.oid=target_signature::REGPROCEDURE;
    IF sha256(convert_to(definition,'UTF8')) IS DISTINCT FROM predecessor
       OR (length(definition)-length(replace(definition,old_filter,'')))<>length(old_filter)
       OR EXISTS(SELECT 1 FROM storage_v2_posting_conversion_contract c
                 WHERE c.signature=target_signature) THEN
        RAISE EXCEPTION 'derived candidate scope predecessor differs';
    END IF;
    SELECT * INTO STRICT before_seal FROM storage_v2_lexical_conversion_contract_seal WHERE singleton;

    -- Keep the materialized fingerprint scan. A semi-join lets PostgreSQL
    -- hash the occurrence set once and never duplicates matching blocks.
    EXECUTE replace(definition,old_filter,new_filter);
    SELECT sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')) INTO STRICT installed
      FROM pg_proc p WHERE p.oid=target_signature::REGPROCEDURE AND to_jsonb(p)-'prosrc'=metadata;
    UPDATE storage_v2_lexical_conversion_function_contract c SET definition_sha256=installed
      WHERE c.signature=target_signature AND c.definition_sha256=predecessor;
    IF NOT FOUND THEN RAISE EXCEPTION 'derived candidate scope dependency differs'; END IF;

    IF NOT EXISTS(SELECT 1 FROM pg_trigger
                  WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
                    AND tgname='storage_v2_lexical_conversion_seal_immutable'
                    AND tgenabled='O' AND tgtype=27
                    AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'derived candidate scope requires the active lexical seal guard';
    END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
    UPDATE storage_v2_lexical_conversion_contract_seal s
       SET contract_sha256=sha256(convert_to(jsonb_build_object(
           'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature)
                          FROM storage_v2_lexical_conversion_function_contract c),
           'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid)
                          FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
     WHERE s.singleton AND to_jsonb(s)=to_jsonb(before_seal);
    IF NOT FOUND THEN RAISE EXCEPTION 'derived candidate scope seal differs'; END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
END $candidate_scope$;
SELECT storage_v2_require_lexical_conversion_operator();
COMMIT;
