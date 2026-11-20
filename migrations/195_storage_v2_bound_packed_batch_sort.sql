-- Keep only the small packed entries in the batch aggregate's sort input.
BEGIN;
SET LOCAL search_path = pg_catalog, public, pg_temp;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT public.storage_v2_require_lexical_conversion_operator();
LOCK TABLE public.storage_v2_lexical_conversion_function_contract,
           public.storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

DO $batch_sort$
DECLARE
    target_signature CONSTANT TEXT := 'storage_v2_append_inverted_batch(bigint,integer,integer)';
    predecessor CONSTANT BYTEA := decode('040200f3905255c94fc568f6d0d0123cf76bbad7ddc37603394be7b08bbf68bf','hex');
    old_input CONSTANT TEXT := $old$    WITH input AS MATERIALIZED (
        SELECT (source.ordinal - 1)::SMALLINT AS block_id, source.payload
          FROM unnest(selected_payloads) WITH ORDINALITY AS source(payload, ordinal)
    ), packed AS (
        SELECT get_byte(input.payload, slot.byte_offset) AS prefix,
               string_agg(
                   substring(input.payload FROM slot.byte_offset + 2 FOR 1) || int2send(input.block_id)
                       || substring(input.payload FROM slot.byte_offset + 3 FOR 3),
                   '\x'::BYTEA ORDER BY get_byte(input.payload, slot.byte_offset + 1), input.block_id
               ) AS postings
          FROM input
         CROSS JOIN LATERAL generate_series(0, octet_length(input.payload) - 5, 5) AS slot(byte_offset)
         GROUP BY get_byte(input.payload, slot.byte_offset)
    )$old$;
    new_input CONSTANT TEXT := $new$    WITH input AS MATERIALIZED (
        SELECT (source.ordinal - 1)::SMALLINT AS block_id,
               substring(source.payload FROM 1 FOR octet_length(source.payload)) AS payload
          FROM unnest(selected_payloads) WITH ORDINALITY AS source(payload, ordinal)
    ), entries AS MATERIALIZED (
        SELECT get_byte(input.payload, slot.byte_offset)::SMALLINT AS prefix,
               get_byte(input.payload, slot.byte_offset + 1)::SMALLINT AS low_key,
               input.block_id,
               substring(input.payload FROM slot.byte_offset + 2 FOR 1) || int2send(input.block_id)
                   || substring(input.payload FROM slot.byte_offset + 3 FOR 3) AS posting
          FROM input
         CROSS JOIN LATERAL generate_series(0, octet_length(input.payload) - 5, 5) AS slot(byte_offset)
    ), packed AS (
        SELECT entry.prefix,
               string_agg(entry.posting, '\x'::BYTEA ORDER BY entry.low_key, entry.block_id) AS postings
          FROM entries entry
         GROUP BY entry.prefix
    )$new$;
    definition TEXT;
    installed BYTEA;
    metadata JSONB;
    before_seal public.storage_v2_lexical_conversion_contract_seal%ROWTYPE;
BEGIN
    SELECT pg_get_functiondef(p.oid), to_jsonb(p)-'prosrc'
      INTO STRICT definition, metadata FROM pg_proc p WHERE p.oid=target_signature::REGPROCEDURE;
    IF sha256(convert_to(definition,'UTF8')) IS DISTINCT FROM predecessor
       OR (length(definition)-length(replace(definition,old_input,'')))<>length(old_input)
       OR EXISTS(SELECT 1 FROM public.storage_v2_posting_conversion_contract c WHERE c.signature=target_signature) THEN
        RAISE EXCEPTION 'inverted batch sort predecessor differs';
    END IF;
    SELECT * INTO STRICT before_seal FROM public.storage_v2_lexical_conversion_contract_seal WHERE singleton;

    -- Detoast each payload once and materialize only the six-byte posting
    -- plus numeric sort keys. Sorting the full payload per slot can make the
    -- sort grow with payload size squared.
    EXECUTE replace(definition,old_input,new_input);
    SELECT sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')) INTO STRICT installed
      FROM pg_proc p WHERE p.oid=target_signature::REGPROCEDURE AND to_jsonb(p)-'prosrc'=metadata;
    UPDATE public.storage_v2_lexical_conversion_function_contract c SET definition_sha256=installed
      WHERE c.signature=target_signature AND c.definition_sha256=predecessor;
    IF NOT FOUND THEN RAISE EXCEPTION 'inverted batch sort dependency differs'; END IF;

    IF NOT EXISTS(SELECT 1 FROM pg_trigger
                  WHERE tgrelid='public.storage_v2_lexical_conversion_contract_seal'::REGCLASS
                    AND tgname='storage_v2_lexical_conversion_seal_immutable'
                    AND tgenabled='O' AND tgtype=27
                    AND tgfoid='public.storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'inverted batch sort requires the active lexical seal guard';
    END IF;
    ALTER TABLE public.storage_v2_lexical_conversion_contract_seal
        DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
    UPDATE public.storage_v2_lexical_conversion_contract_seal s
       SET contract_sha256=sha256(convert_to(jsonb_build_object(
           'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature)
                          FROM public.storage_v2_lexical_conversion_function_contract c),
           'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid)
                          FROM public.storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
     WHERE s.singleton AND to_jsonb(s)=to_jsonb(before_seal);
    IF NOT FOUND THEN RAISE EXCEPTION 'inverted batch sort seal differs'; END IF;
    ALTER TABLE public.storage_v2_lexical_conversion_contract_seal
        ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
END $batch_sort$;
SELECT public.storage_v2_require_lexical_conversion_operator();
COMMIT;
