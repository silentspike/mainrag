-- Build packed inverted batches for one frozen generation from staged
-- projection proofs. A batch never implies reader readiness on its own.
BEGIN;
SET LOCAL search_path = pg_catalog, public, pg_temp;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT public.storage_v2_require_lexical_conversion_operator();

CREATE TABLE public.storage_v2_inverted_generation (
    generation_id BIGINT PRIMARY KEY,
    source_id BIGINT NOT NULL,
    generation_seq BIGINT NOT NULL,
    generation_item_count BIGINT NOT NULL CHECK (generation_item_count >= 0),
    generation_sealed_at TIMESTAMPTZ NOT NULL,
    metadata_revision BIGINT NOT NULL CHECK (metadata_revision >= 0),
    metadata_binding_count BIGINT NOT NULL CHECK (metadata_binding_count >= 0),
    metadata_view_count BIGINT NOT NULL CHECK (metadata_view_count >= 0),
    next_batch INTEGER NOT NULL DEFAULT 0 CHECK (next_batch >= 0),
    total_blocks BIGINT NOT NULL DEFAULT 0 CHECK (total_blocks >= next_batch),
    cursor_occurrence_id BIGINT,
    cursor_block_order BIGINT CHECK (cursor_block_order >= 0),
    published_at TIMESTAMPTZ,
    published_by UUID,
    CHECK ((next_batch = 0) = (total_blocks = 0)),
    CHECK ((next_batch = 0) = (cursor_occurrence_id IS NULL)),
    CHECK ((cursor_occurrence_id IS NULL) = (cursor_block_order IS NULL)),
    CHECK ((published_at IS NULL) = (published_by IS NULL))
);

CREATE TABLE public.storage_v2_inverted_batch (
    generation_id BIGINT NOT NULL REFERENCES public.storage_v2_inverted_generation ON DELETE RESTRICT,
    batch_no INTEGER NOT NULL CHECK (batch_no >= 0),
    requested_limit INTEGER NOT NULL CHECK (requested_limit BETWEEN 1 AND 4096),
    block_count INTEGER NOT NULL CHECK (block_count BETWEEN 1 AND requested_limit),
    last_occurrence_id BIGINT NOT NULL,
    last_block_order BIGINT NOT NULL CHECK (last_block_order >= 0),
    total_blocks BIGINT NOT NULL CHECK (total_blocks >= block_count),
    packed_bytes BIGINT NOT NULL CHECK (packed_bytes >= 0 AND packed_bytes % 6 = 0),
    PRIMARY KEY (generation_id, batch_no)
);

CREATE TABLE public.storage_v2_inverted_block (
    generation_id BIGINT NOT NULL,
    batch_no INTEGER NOT NULL,
    block_id INTEGER NOT NULL CHECK (block_id BETWEEN 0 AND 4095),
    occurrence_id BIGINT NOT NULL,
    block_order BIGINT NOT NULL CHECK (block_order >= 0),
    source_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    document_id BIGINT NOT NULL,
    layout_sha256 BYTEA NOT NULL CHECK (octet_length(layout_sha256) = 32),
    payload_sha256 BYTEA NOT NULL CHECK (octet_length(payload_sha256) = 32),
    PRIMARY KEY (generation_id, batch_no, block_id),
    UNIQUE (generation_id, occurrence_id, block_order),
    FOREIGN KEY (generation_id, batch_no) REFERENCES public.storage_v2_inverted_batch ON DELETE RESTRICT
);

CREATE TABLE public.storage_v2_inverted_prefix (
    generation_id BIGINT NOT NULL,
    batch_no INTEGER NOT NULL,
    prefix SMALLINT NOT NULL CHECK (prefix BETWEEN 0 AND 255),
    postings BYTEA NOT NULL CHECK (octet_length(postings) <= 6291456 AND octet_length(postings) % 6 = 0),
    PRIMARY KEY (generation_id, prefix, batch_no),
    FOREIGN KEY (generation_id, batch_no) REFERENCES public.storage_v2_inverted_batch ON DELETE RESTRICT
);

ALTER TABLE public.storage_v2_inverted_generation OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE public.storage_v2_inverted_batch OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE public.storage_v2_inverted_block OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE public.storage_v2_inverted_prefix OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON public.storage_v2_inverted_generation, public.storage_v2_inverted_batch,
    public.storage_v2_inverted_block, public.storage_v2_inverted_prefix FROM PUBLIC, mainrag;
GRANT SELECT ON public.storage_v2_inverted_generation, public.storage_v2_inverted_block,
    public.storage_v2_inverted_prefix TO mainrag_v2_lexical_rank_owner;
GRANT SELECT ON public.storage_v2_inverted_generation TO mainrag_v2_metadata_reader;

CREATE TRIGGER storage_v2_inverted_batch_immutable BEFORE UPDATE OR DELETE
    ON public.storage_v2_inverted_batch FOR EACH ROW EXECUTE FUNCTION public.storage_v2_reject_retrieval_mutation();
CREATE TRIGGER storage_v2_inverted_block_immutable BEFORE UPDATE OR DELETE
    ON public.storage_v2_inverted_block FOR EACH ROW EXECUTE FUNCTION public.storage_v2_reject_retrieval_mutation();
CREATE TRIGGER storage_v2_inverted_prefix_immutable BEFORE UPDATE OR DELETE
    ON public.storage_v2_inverted_prefix FOR EACH ROW EXECUTE FUNCTION public.storage_v2_reject_retrieval_mutation();

CREATE FUNCTION public.storage_v2_guard_inverted_generation_mutation()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $guard$
BEGIN
    IF TG_OP <> 'UPDATE' OR OLD.published_at IS NOT NULL THEN
        RAISE EXCEPTION 'inverted generation seal is immutable' USING ERRCODE = '42501';
    END IF;
    IF (OLD.generation_id, OLD.source_id, OLD.generation_seq, OLD.generation_item_count,
        OLD.generation_sealed_at, OLD.metadata_revision, OLD.metadata_binding_count, OLD.metadata_view_count)
       IS DISTINCT FROM
       (NEW.generation_id, NEW.source_id, NEW.generation_seq, NEW.generation_item_count,
        NEW.generation_sealed_at, NEW.metadata_revision, NEW.metadata_binding_count, NEW.metadata_view_count) THEN
        RAISE EXCEPTION 'inverted generation snapshot is immutable' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$guard$;
ALTER FUNCTION public.storage_v2_guard_inverted_generation_mutation() OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION public.storage_v2_guard_inverted_generation_mutation() FROM PUBLIC, mainrag;
CREATE TRIGGER storage_v2_inverted_generation_immutable BEFORE UPDATE OR DELETE
    ON public.storage_v2_inverted_generation FOR EACH ROW
    EXECUTE FUNCTION public.storage_v2_guard_inverted_generation_mutation();

CREATE FUNCTION public.storage_v2_append_inverted_batch(
    p_generation_id BIGINT, p_expected_batch INTEGER, p_limit INTEGER
) RETURNS TABLE (disposition TEXT, batch_no INTEGER, block_count INTEGER, total_blocks BIGINT)
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
SET plan_cache_mode = force_custom_plan
SET jit = off AS $append$
DECLARE
    snapshot RECORD;
    build public.storage_v2_inverted_generation;
    receipt public.storage_v2_inverted_batch;
    selected_blocks public.storage_v2_inverted_block[];
    selected_payloads BYTEA[];
    missing BIGINT;
    input_bytes BIGINT;
    selected_count INTEGER;
    last_block public.storage_v2_inverted_block;
BEGIN
    PERFORM public.storage_v2_require_lexical_conversion_operator();
    IF p_generation_id IS NULL OR p_expected_batch IS NULL OR p_expected_batch < 0
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 4096 THEN
        RAISE EXCEPTION 'inverted batch requires a generation, expected batch and bounded block limit';
    END IF;

    SELECT header.* INTO snapshot FROM public.storage_v2_reader_metadata_header header
      JOIN public.storage_v2_reader_metadata_epoch epoch
        ON epoch.source_id = header.source_id AND epoch.revision = header.revision
      JOIN public.source_generation generation
        ON (generation.id, generation.source_id, generation.generation_seq, generation.item_count, generation.sealed_at)
         = (header.generation_id, header.source_id, header.generation_seq, header.item_count, header.sealed_at)
     WHERE header.generation_id = p_generation_id AND header.complete AND NOT header.missing_documents
       AND generation.status IN ('sealed', 'verified', 'release_candidate', 'active', 'superseded');
    IF NOT FOUND THEN RAISE EXCEPTION 'inverted publication requires complete frozen reader metadata'; END IF;
    IF public.storage_v2_can_access_source(snapshot.source_id, 'write') IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'inverted publication requires source write authority' USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM public.storage_v2_inverted_source source WHERE source.source_id = snapshot.source_id FOR KEY SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'inverted publication requires enabled source'; END IF;

    SELECT state.* INTO build FROM public.storage_v2_inverted_generation state
     WHERE state.generation_id = p_generation_id FOR UPDATE;
    IF NOT FOUND THEN
        IF p_expected_batch <> 0 THEN RAISE EXCEPTION 'inverted batch cursor differs'; END IF;
        IF public.storage_v2_reader_metadata_ready(ARRAY[p_generation_id]) IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'inverted publication requires complete frozen reader metadata';
        END IF;
        INSERT INTO public.storage_v2_inverted_generation (
            generation_id, source_id, generation_seq, generation_item_count, generation_sealed_at,
            metadata_revision, metadata_binding_count, metadata_view_count
        ) VALUES (
            p_generation_id, snapshot.source_id, snapshot.generation_seq, snapshot.item_count, snapshot.sealed_at,
            snapshot.revision, snapshot.binding_count, snapshot.view_count
        ) ON CONFLICT (generation_id) DO NOTHING;
        SELECT state.* INTO STRICT build FROM public.storage_v2_inverted_generation state
         WHERE state.generation_id = p_generation_id FOR UPDATE;
    END IF;
    IF (build.source_id, build.generation_seq, build.generation_item_count, build.generation_sealed_at,
        build.metadata_revision, build.metadata_binding_count, build.metadata_view_count)
       IS DISTINCT FROM
       (snapshot.source_id, snapshot.generation_seq, snapshot.item_count, snapshot.sealed_at,
        snapshot.revision, snapshot.binding_count, snapshot.view_count) THEN
        RAISE EXCEPTION 'inverted publication reader metadata snapshot differs';
    END IF;

    IF p_expected_batch < build.next_batch THEN
        SELECT batch.* INTO STRICT receipt FROM public.storage_v2_inverted_batch batch
         WHERE batch.generation_id = p_generation_id AND batch.batch_no = p_expected_batch;
        IF receipt.requested_limit <> p_limit THEN RAISE EXCEPTION 'inverted batch replay limit differs'; END IF;
        RETURN QUERY SELECT 'replayed'::TEXT, receipt.batch_no, receipt.block_count, receipt.total_blocks;
        RETURN;
    END IF;
    IF p_expected_batch <> build.next_batch OR build.published_at IS NOT NULL THEN
        RAISE EXCEPTION 'inverted batch cursor differs';
    END IF;

    -- Use the generation's indexed occurrence roster. The lateral block
    -- lookup starts at the saved primary-key cursor even inside an occurrence.
    -- Missing staged bindings remain part of this roster and abort the batch.
    WITH roster AS MATERIALIZED (
        SELECT metadata.occurrence_id, original.block_order, original.artifact_version_id,
               metadata.document_id, original.fingerprint_count
          FROM public.storage_v2_reader_metadata_binding metadata
         CROSS JOIN LATERAL (
             SELECT block.block_order, block.artifact_version_id, cardinality(block.fingerprints) AS fingerprint_count
               FROM public.storage_v2_derived_lexical_block block
              WHERE block.occurrence_id = metadata.occurrence_id AND block.source_id = build.source_id
                AND block.block_order > CASE WHEN metadata.occurrence_id = build.cursor_occurrence_id
                                             THEN build.cursor_block_order ELSE -1 END
              ORDER BY block.block_order OFFSET 0
         ) original
         WHERE metadata.generation_id = p_generation_id AND metadata.component_ordinal = 0
           AND (build.cursor_occurrence_id IS NULL OR metadata.occurrence_id >= build.cursor_occurrence_id)
         ORDER BY metadata.occurrence_id, original.block_order
         LIMIT p_limit
    ), staged AS MATERIALIZED (
        SELECT roster.*, binding.layout_sha256, binding.payload_sha256, projection.payload,
               row_number() OVER (ORDER BY roster.occurrence_id, roster.block_order) AS ordinal,
               sum(coalesce(octet_length(projection.payload), 0))
                   OVER (ORDER BY roster.occurrence_id, roster.block_order ROWS UNBOUNDED PRECEDING) AS bytes_so_far
          FROM roster
          LEFT JOIN public.storage_v2_inverted_binding binding
            ON binding.occurrence_id = roster.occurrence_id AND binding.block_order = roster.block_order
           AND binding.source_id = build.source_id AND binding.artifact_version_id = roster.artifact_version_id
           AND binding.document_id = roster.document_id
          LEFT JOIN public.storage_v2_inverted_projection projection
            ON projection.source_id = binding.source_id AND projection.document_id = binding.document_id
           AND projection.layout_sha256 = binding.layout_sha256 AND projection.payload_sha256 = binding.payload_sha256
    )
    SELECT array_agg(ROW(p_generation_id, p_expected_batch, (staged.ordinal - 1)::INTEGER, staged.occurrence_id,
                        staged.block_order, build.source_id, staged.artifact_version_id, staged.document_id,
                        staged.layout_sha256, staged.payload_sha256)::public.storage_v2_inverted_block ORDER BY staged.ordinal),
           array_agg(staged.payload ORDER BY staged.ordinal),
           count(*) FILTER (WHERE staged.payload IS NULL
                              OR sha256(staged.payload) IS DISTINCT FROM staged.payload_sha256
                              OR octet_length(staged.payload) IS DISTINCT FROM 5 * staged.fingerprint_count),
           coalesce(sum(octet_length(staged.payload)::BIGINT), 0)
      INTO selected_blocks, selected_payloads, missing, input_bytes
      FROM staged WHERE staged.bytes_so_far <= 16777216;

    IF missing <> 0 THEN RAISE EXCEPTION 'inverted batch requires staged coverage for every selected derived block'; END IF;
    selected_count := coalesce(cardinality(selected_blocks), 0);
    IF selected_count = 0 THEN
        RETURN QUERY SELECT 'exhausted'::TEXT, p_expected_batch, 0, build.total_blocks;
        RETURN;
    END IF;
    last_block := selected_blocks[selected_count];
    INSERT INTO public.storage_v2_inverted_batch (
        generation_id, batch_no, requested_limit, block_count, last_occurrence_id, last_block_order, total_blocks, packed_bytes
    ) VALUES (
        p_generation_id, p_expected_batch, p_limit, selected_count, last_block.occurrence_id, last_block.block_order,
        build.total_blocks + selected_count, input_bytes / 5 * 6
    ) RETURNING * INTO receipt;

    INSERT INTO public.storage_v2_inverted_block SELECT row.* FROM unnest(selected_blocks) row;
    INSERT INTO public.storage_v2_inverted_prefix (generation_id, batch_no, prefix, postings)
    WITH input AS MATERIALIZED (
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
    )
    SELECT p_generation_id, p_expected_batch, prefix.number::SMALLINT, coalesce(packed.postings, '\x'::BYTEA)
      FROM generate_series(0, 255) AS prefix(number)
      LEFT JOIN packed ON packed.prefix = prefix.number;

    UPDATE public.storage_v2_inverted_generation state
       SET next_batch = state.next_batch + 1, total_blocks = receipt.total_blocks,
           cursor_occurrence_id = receipt.last_occurrence_id, cursor_block_order = receipt.last_block_order
     WHERE state.generation_id = p_generation_id;
    RETURN QUERY SELECT 'appended'::TEXT, receipt.batch_no, receipt.block_count, receipt.total_blocks;
END
$append$;

CREATE FUNCTION public.storage_v2_publish_inverted_generation(p_generation_id BIGINT, p_expected_batches INTEGER)
RETURNS BOOLEAN LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
SET jit = off AS $publish$
DECLARE
    build public.storage_v2_inverted_generation;
    original_blocks BIGINT;
    covered_blocks BIGINT;
BEGIN
    PERFORM public.storage_v2_require_lexical_conversion_operator();
    SELECT state.* INTO build FROM public.storage_v2_inverted_generation state
     WHERE state.generation_id = p_generation_id FOR UPDATE;
    IF NOT FOUND OR p_expected_batches IS DISTINCT FROM build.next_batch THEN
        RAISE EXCEPTION 'inverted publication batch count differs';
    END IF;
    IF public.storage_v2_can_access_source(build.source_id, 'write') IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'inverted publication requires source write authority' USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM public.storage_v2_inverted_source source WHERE source.source_id = build.source_id FOR KEY SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'inverted publication requires enabled source'; END IF;

    IF build.published_at IS NOT NULL THEN RETURN TRUE; END IF;

    -- Match the existing generation-before-source metadata lock order. Bound
    -- lock acquisition and freeze both the metadata and the derived rows for
    -- the one coverage audit, including the valid zero-block case. Later
    -- retained conversions still require per-request block coverage at read.
    PERFORM 1 FROM public.source_generation generation WHERE generation.id = p_generation_id FOR SHARE NOWAIT;
    PERFORM 1 FROM public.storage_v2_reader_metadata_epoch epoch WHERE epoch.source_id = build.source_id FOR SHARE NOWAIT;
    PERFORM 1 FROM public.storage_v2_reader_metadata_header header
     WHERE header.generation_id = p_generation_id FOR SHARE NOWAIT;
    LOCK TABLE public.storage_v2_derived_lexical_block IN SHARE MODE NOWAIT;
    IF public.storage_v2_reader_metadata_ready(ARRAY[p_generation_id]) IS DISTINCT FROM TRUE
       OR NOT EXISTS (
           SELECT 1 FROM public.storage_v2_reader_metadata_header header
             JOIN public.storage_v2_reader_metadata_epoch epoch
               ON epoch.source_id = header.source_id AND epoch.revision = header.revision
            WHERE header.generation_id = p_generation_id AND header.complete AND NOT header.missing_documents
              AND (header.source_id, header.generation_seq, header.item_count, header.sealed_at,
                   header.revision, header.binding_count, header.view_count)
                = (build.source_id, build.generation_seq, build.generation_item_count, build.generation_sealed_at,
                   build.metadata_revision, build.metadata_binding_count, build.metadata_view_count)
       ) THEN
        RAISE EXCEPTION 'inverted publication reader metadata snapshot differs';
    END IF;

    SELECT count(*), count(packed.block_id) INTO original_blocks, covered_blocks
      FROM public.storage_v2_reader_metadata_binding metadata
      JOIN public.storage_v2_derived_lexical_block original ON original.occurrence_id = metadata.occurrence_id
       AND original.source_id = build.source_id
      LEFT JOIN public.storage_v2_inverted_block packed ON packed.generation_id = metadata.generation_id
       AND packed.occurrence_id = original.occurrence_id AND packed.block_order = original.block_order
       AND packed.source_id = original.source_id AND packed.artifact_version_id = original.artifact_version_id
       AND packed.document_id = metadata.document_id
     WHERE metadata.generation_id = p_generation_id AND metadata.component_ordinal = 0;

    IF covered_blocks <> original_blocks OR original_blocks <> build.total_blocks THEN
        RAISE EXCEPTION 'inverted publication derived coverage is incomplete';
    END IF;
    UPDATE public.storage_v2_inverted_generation state
       SET published_at = transaction_timestamp(), published_by = current_setting('app.user_id')::UUID
     WHERE state.generation_id = p_generation_id;
    RETURN TRUE;
END
$publish$;

CREATE FUNCTION public.storage_v2_inverted_generation_ready(p_generation_id BIGINT)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
SET row_security = on AS $ready$
    SELECT EXISTS (
        SELECT 1 FROM public.storage_v2_inverted_generation state
          JOIN public.storage_v2_reader_metadata_header header ON header.generation_id = state.generation_id
          JOIN public.storage_v2_reader_metadata_epoch epoch
            ON epoch.source_id = header.source_id AND epoch.revision = header.revision
          JOIN public.source_generation generation
            ON (generation.id, generation.source_id, generation.generation_seq, generation.item_count, generation.sealed_at)
             = (header.generation_id, header.source_id, header.generation_seq, header.item_count, header.sealed_at)
         WHERE state.generation_id = p_generation_id AND state.published_at IS NOT NULL
           AND header.complete AND NOT header.missing_documents
           AND generation.status IN ('sealed', 'verified', 'release_candidate', 'active', 'superseded')
           AND (header.source_id, header.generation_seq, header.item_count, header.sealed_at,
                header.revision, header.binding_count, header.view_count)
             = (state.source_id, state.generation_seq, state.generation_item_count, state.generation_sealed_at,
                state.metadata_revision, state.metadata_binding_count, state.metadata_view_count)
           AND public.storage_v2_can_access_source(state.source_id, 'read')
    )
$ready$;

ALTER FUNCTION public.storage_v2_append_inverted_batch(BIGINT, INTEGER, INTEGER) OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION public.storage_v2_publish_inverted_generation(BIGINT, INTEGER) OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION public.storage_v2_inverted_generation_ready(BIGINT) OWNER TO mainrag_v2_metadata_reader;
REVOKE ALL ON FUNCTION public.storage_v2_append_inverted_batch(BIGINT, INTEGER, INTEGER),
    public.storage_v2_publish_inverted_generation(BIGINT, INTEGER),
    public.storage_v2_inverted_generation_ready(BIGINT) FROM PUBLIC, mainrag;
GRANT EXECUTE ON FUNCTION public.storage_v2_inverted_generation_ready(BIGINT) TO mainrag_v2_lexical_rank_owner;

SELECT public.storage_v2_require_lexical_conversion_operator();
COMMIT;
