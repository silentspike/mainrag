-- Stage missing packed projections from a frozen historical generation.
BEGIN;
SET LOCAL search_path = pg_catalog, public, pg_temp;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT public.storage_v2_require_lexical_conversion_operator();

CREATE FUNCTION public.storage_v2_backfill_inverted_projections(
    p_generation_id BIGINT,
    p_after_occurrence_id BIGINT,
    p_after_block_order BIGINT,
    p_max_blocks INTEGER,
    p_max_body_bytes BIGINT
) RETURNS TABLE (
    staged_blocks BIGINT,
    decoded_occurrences BIGINT,
    logical_body_bytes BIGINT,
    logical_segment_bytes BIGINT,
    next_occurrence_id BIGINT,
    next_block_order BIGINT,
    has_more BOOLEAN
)
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
SET plan_cache_mode = force_custom_plan
SET jit = off AS $backfill$
DECLARE
    selected_source BIGINT;
    metadata_ready BOOLEAN;
    missing_documents BOOLEAN;
    available_blocks BIGINT;
    first_identity_valid BOOLEAN;
    eligible_blocks public.storage_v2_derived_lexical_block[];
    document_input RECORD;
    chosen public.storage_v2_derived_lexical_block;
    vectors TSVECTOR[];
    vector_start INTEGER;
    segment_count INTEGER;
BEGIN
    PERFORM public.storage_v2_require_lexical_conversion_operator();
    IF p_generation_id IS NULL OR p_generation_id <= 0
       OR p_after_occurrence_id IS NULL OR p_after_occurrence_id < 0
       OR p_after_block_order IS NULL
       OR (p_after_occurrence_id = 0 AND p_after_block_order <> -1)
       OR (p_after_occurrence_id > 0 AND p_after_block_order < 0)
       OR p_max_blocks IS NULL OR p_max_blocks NOT BETWEEN 1 AND 128
       OR p_max_body_bytes IS NULL OR p_max_body_bytes NOT BETWEEN 1 AND 134217728 THEN
        RAISE EXCEPTION 'inverted backfill requires a generation, ordered cursor and bounded work';
    END IF;

    -- Hold the same checked generation and metadata revision through the
    -- complete batch. NOWAIT keeps a competing lifecycle writer in control.
    SELECT generation.source_id INTO selected_source
      FROM public.source_generation generation
      JOIN public.storage_v2_reader_metadata_header header
        ON (header.generation_id,header.source_id,header.generation_seq,header.item_count,header.sealed_at)
         = (generation.id,generation.source_id,generation.generation_seq,generation.item_count,generation.sealed_at)
      JOIN public.storage_v2_reader_metadata_epoch epoch
        ON epoch.source_id = header.source_id AND epoch.revision = header.revision
     WHERE generation.id = p_generation_id AND header.complete AND NOT header.missing_documents
       AND generation.status IN ('sealed','verified','release_candidate','active','superseded')
       FOR SHARE OF generation,epoch,header NOWAIT;
    IF NOT FOUND THEN RAISE EXCEPTION 'inverted backfill requires complete frozen reader metadata'; END IF;
    IF public.storage_v2_can_access_source(selected_source,'write') IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'inverted backfill requires source write authority' USING ERRCODE = '42501';
    END IF;
    SELECT status.ready,status.missing_documents INTO STRICT metadata_ready,missing_documents
      FROM public.storage_v2_reader_metadata_status(ARRAY[p_generation_id]) status;
    IF metadata_ready IS DISTINCT FROM TRUE OR missing_documents IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION 'inverted backfill requires complete frozen reader metadata';
    END IF;

    -- The ordinary stage writer takes KEY SHARE on this row. Holding UPDATE
    -- prevents an ingest from staging the selected source between the missing
    -- binding check and the decode. Other sources can continue their writes.
    PERFORM 1 FROM public.storage_v2_inverted_source enabled
     WHERE enabled.source_id = selected_source FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RAISE EXCEPTION 'inverted backfill requires enabled source'; END IF;

    WITH roster AS MATERIALIZED (
        SELECT metadata.occurrence_id,metadata.document_id,original.block_order
          FROM public.storage_v2_reader_metadata_binding metadata
          CROSS JOIN LATERAL (
              SELECT block.block_order
                FROM public.storage_v2_derived_lexical_block block
               WHERE block.occurrence_id = metadata.occurrence_id
                 AND block.source_id = selected_source
                 AND block.block_order > CASE WHEN metadata.occurrence_id = p_after_occurrence_id
                                              THEN p_after_block_order ELSE -1 END
                 AND NOT EXISTS (
                     SELECT 1 FROM public.storage_v2_inverted_binding staged
                      WHERE staged.occurrence_id = block.occurrence_id AND staged.block_order = block.block_order
                 )
               ORDER BY block.block_order OFFSET 0
          ) original
         WHERE metadata.generation_id = p_generation_id AND metadata.component_ordinal = 0
           AND metadata.occurrence_id >= p_after_occurrence_id
         ORDER BY metadata.occurrence_id,original.block_order
         LIMIT p_max_blocks + 1
    ), measured AS MATERIALIZED (
        SELECT roster.occurrence_id,roster.block_order,block AS block_row,
               octet_length(document.search_text) AS body_bytes,
               cost.segment_bytes,
               COALESCE(document.id IS NOT NULL AND cost.valid, FALSE) AS identity_valid
          FROM roster
          JOIN public.storage_v2_derived_lexical_block block
            ON block.occurrence_id = roster.occurrence_id AND block.block_order = roster.block_order
           AND block.source_id = selected_source
          LEFT JOIN public.occurrence item ON item.id = block.occurrence_id
           AND item.source_id = block.source_id AND item.artifact_version_id = block.artifact_version_id
          LEFT JOIN public.artifact_version artifact ON artifact.id = item.artifact_version_id
           AND artifact.source_id = item.source_id
          LEFT JOIN public.storage_v2_search_view_document binding ON binding.view_id = item.view_id
           AND binding.ordinal = 0 AND binding.document_id = roster.document_id
          LEFT JOIN public.storage_v2_search_document document ON document.id = binding.document_id
           AND document.component_kind = 'node' AND document.node_id = artifact.content_root_node_id
          CROSS JOIN LATERAL (
              SELECT sum(part.byte_length::BIGINT + octet_length(part.prefix) + octet_length(part.kind)) AS segment_bytes,
                     count(*) = cardinality(block.segment_orders) AND count(*) BETWEEN 1 AND 64
                     AND bool_and(COALESCE(part.byte_start >= 1 AND part.byte_length >= 1
                         AND part.byte_start::BIGINT + part.byte_length - 1 <= octet_length(document.search_text)
                         AND part.prefix IS NOT NULL AND part.kind IS NOT NULL,FALSE)) AS valid
                FROM unnest(block.text_byte_starts,block.text_byte_lengths,block.context_prefixes,block.chunk_types)
                     AS part(byte_start,byte_length,prefix,kind)
          ) cost
          FOR KEY SHARE OF block NOWAIT
    ), ordered AS (
        SELECT measured.*,
               row_number() OVER (ORDER BY measured.occurrence_id,measured.block_order) AS position,
               row_number() OVER (PARTITION BY measured.occurrence_id ORDER BY measured.block_order) = 1 AS first_block
          FROM measured
    ), costs AS (
        SELECT ordered.*,
               CASE WHEN ordered.first_block THEN coalesce(ordered.body_bytes,0) ELSE 0 END AS charged_body,
               sum(CASE WHEN ordered.first_block THEN coalesce(ordered.body_bytes,0) ELSE 0 END) OVER progress AS body_so_far,
               sum(coalesce(ordered.segment_bytes,0)) OVER progress AS segment_so_far,
               bool_and(ordered.identity_valid) OVER progress AS complete_prefix
          FROM ordered
          WINDOW progress AS (ORDER BY ordered.position ROWS UNBOUNDED PRECEDING)
    ), admitted AS (
        SELECT costs.*,
               costs.position <= p_max_blocks AND costs.complete_prefix
               AND costs.body_so_far <= p_max_body_bytes AND costs.segment_so_far <= p_max_body_bytes AS allowed
          FROM costs
    )
    SELECT count(*), bool_or(admitted.identity_valid) FILTER (WHERE admitted.position = 1),
           array_agg(admitted.block_row ORDER BY admitted.position) FILTER (WHERE admitted.allowed),
           coalesce(sum(admitted.charged_body) FILTER (WHERE admitted.allowed),0),
           coalesce(sum(admitted.segment_bytes) FILTER (WHERE admitted.allowed),0)
      INTO available_blocks,first_identity_valid,eligible_blocks,logical_body_bytes,logical_segment_bytes
      FROM admitted;

    staged_blocks := 0;
    decoded_occurrences := 0;
    next_occurrence_id := p_after_occurrence_id;
    next_block_order := p_after_block_order;
    has_more := FALSE;
    IF available_blocks = 0 THEN
        RETURN NEXT;
        RETURN;
    END IF;
    IF first_identity_valid IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'inverted backfill canonical block identity differs at the cursor';
    END IF;
    IF eligible_blocks IS NULL THEN
        RAISE EXCEPTION 'inverted backfill next block exceeds the canonical or segment byte budget';
    END IF;

    -- The selected complete prefix has already been budgeted. Decode the
    -- canonical body once per occurrence and align its vectors by block and
    -- by stored array order before invoking the original staging procedure.
    FOR document_input IN
        SELECT block.occurrence_id,
               array_agg(part.byte_start ORDER BY block.block_order,part.ordinal) AS byte_starts,
               array_agg(part.byte_length ORDER BY block.block_order,part.ordinal) AS byte_lengths,
               array_agg(part.prefix ORDER BY block.block_order,part.ordinal) AS prefixes,
               array_agg(part.kind ORDER BY block.block_order,part.ordinal) AS kinds
          FROM unnest(eligible_blocks) block
          CROSS JOIN LATERAL unnest(block.text_byte_starts,block.text_byte_lengths,block.context_prefixes,block.chunk_types)
               WITH ORDINALITY AS part(byte_start,byte_length,prefix,kind,ordinal)
         GROUP BY block.occurrence_id
         ORDER BY block.occurrence_id
    LOOP
        vectors := public.storage_v2_derived_lexical_vectors(document_input.occurrence_id,
            document_input.byte_starts,document_input.byte_lengths,document_input.prefixes,document_input.kinds);
        IF cardinality(vectors) IS DISTINCT FROM cardinality(document_input.byte_starts) THEN
            RAISE EXCEPTION 'inverted backfill derived vector count differs';
        END IF;
        decoded_occurrences := decoded_occurrences + 1;
        vector_start := 1;
        FOR chosen IN SELECT block.* FROM unnest(eligible_blocks) block
                       WHERE block.occurrence_id = document_input.occurrence_id ORDER BY block.block_order
        LOOP
            segment_count := cardinality(chosen.segment_orders);
            IF public.storage_v2_stage_inverted_projection(
                chosen,vectors[vector_start:vector_start+segment_count-1]
            ) IS DISTINCT FROM TRUE THEN
                RAISE EXCEPTION 'inverted backfill staging authorization changed';
            END IF;
            vector_start := vector_start + segment_count;
            staged_blocks := staged_blocks + 1;
            next_occurrence_id := chosen.occurrence_id;
            next_block_order := chosen.block_order;
        END LOOP;
    END LOOP;
    has_more := available_blocks > staged_blocks;
    RETURN NEXT;
END
$backfill$;

REVOKE ALL ON FUNCTION public.storage_v2_backfill_inverted_projections(BIGINT,BIGINT,BIGINT,INTEGER,BIGINT)
    FROM PUBLIC,mainrag;

LOCK TABLE storage_v2_lexical_conversion_function_contract,
           storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

DO $pin_backfill$
DECLARE
    target CONSTANT TEXT := 'storage_v2_backfill_inverted_projections(bigint,bigint,bigint,integer,bigint)';
    before_seal storage_v2_lexical_conversion_contract_seal%ROWTYPE;
BEGIN
    SELECT * INTO STRICT before_seal FROM storage_v2_lexical_conversion_contract_seal WHERE singleton;
    INSERT INTO storage_v2_lexical_conversion_function_contract(signature,definition_sha256,owner_oid,acl_sha256)
    SELECT target,sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),p.proowner,
           sha256(convert_to(coalesce(p.proacl::TEXT,''),'UTF8'))
      FROM pg_proc p WHERE p.oid = target::REGPROCEDURE;

    IF NOT EXISTS(SELECT 1 FROM pg_trigger
                  WHERE tgrelid = 'storage_v2_lexical_conversion_contract_seal'::REGCLASS
                    AND tgname = 'storage_v2_lexical_conversion_seal_immutable'
                    AND tgenabled = 'O' AND tgtype = 27
                    AND tgfoid = 'storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
        RAISE EXCEPTION 'inverted backfill requires the active lexical seal guard';
    END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
    UPDATE storage_v2_lexical_conversion_contract_seal s
       SET function_count = before_seal.function_count + 1,
           contract_sha256 = sha256(convert_to(jsonb_build_object(
               'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature)
                              FROM storage_v2_lexical_conversion_function_contract c),
               'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid)
                              FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
     WHERE s.singleton AND to_jsonb(s) = to_jsonb(before_seal);
    IF NOT FOUND THEN RAISE EXCEPTION 'inverted backfill seal differs'; END IF;
    ALTER TABLE storage_v2_lexical_conversion_contract_seal
        ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
END
$pin_backfill$;

SELECT public.storage_v2_require_lexical_conversion_operator();
COMMIT;
