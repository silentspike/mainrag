-- Stage reusable first-hit payloads for explicitly enabled sources. The
-- compressed inverted reader and publication protocol are separate changes.
BEGIN;
SET LOCAL search_path = pg_catalog, public, pg_temp;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT public.storage_v2_require_lexical_conversion_operator();

CREATE TABLE public.storage_v2_inverted_source (
    source_id BIGINT PRIMARY KEY,
    enabled_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    enabled_by UUID NOT NULL
);

CREATE TABLE public.storage_v2_inverted_projection (
    source_id BIGINT NOT NULL,
    document_id BIGINT NOT NULL,
    layout_sha256 BYTEA NOT NULL CHECK (octet_length(layout_sha256) = 32),
    representative_occurrence_id BIGINT NOT NULL,
    representative_block_order BIGINT NOT NULL CHECK (representative_block_order >= 0),
    payload BYTEA NOT NULL CHECK (octet_length(payload) <= 327680 AND octet_length(payload) % 5 = 0),
    payload_sha256 BYTEA NOT NULL CHECK (octet_length(payload_sha256) = 32),
    PRIMARY KEY (source_id, document_id, layout_sha256)
);

CREATE TABLE public.storage_v2_inverted_binding (
    occurrence_id BIGINT NOT NULL,
    block_order BIGINT NOT NULL CHECK (block_order >= 0),
    source_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    document_id BIGINT NOT NULL,
    layout_sha256 BYTEA NOT NULL CHECK (octet_length(layout_sha256) = 32),
    payload_sha256 BYTEA NOT NULL CHECK (octet_length(payload_sha256) = 32),
    PRIMARY KEY (occurrence_id, block_order),
    FOREIGN KEY (source_id, document_id, layout_sha256)
        REFERENCES public.storage_v2_inverted_projection (source_id, document_id, layout_sha256)
        ON DELETE RESTRICT
);

-- The canonical relations already have sealed catalog and immutability
-- contracts. The writer proves their exact identities before publishing a
-- binding instead of adding new RI actions to those catalog-pinned tables.
ALTER TABLE public.storage_v2_inverted_source OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE public.storage_v2_inverted_projection OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE public.storage_v2_inverted_binding OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON public.storage_v2_inverted_source, public.storage_v2_inverted_projection,
    public.storage_v2_inverted_binding FROM PUBLIC, mainrag;

CREATE TRIGGER storage_v2_inverted_projection_immutable BEFORE UPDATE OR DELETE
    ON public.storage_v2_inverted_projection FOR EACH ROW
    EXECUTE FUNCTION public.storage_v2_reject_retrieval_mutation();
CREATE TRIGGER storage_v2_inverted_binding_immutable BEFORE UPDATE OR DELETE
    ON public.storage_v2_inverted_binding FOR EACH ROW
    EXECUTE FUNCTION public.storage_v2_reject_retrieval_mutation();

CREATE FUNCTION public.storage_v2_set_inverted_source_enabled(p_source_id BIGINT, p_enabled BOOLEAN)
RETURNS BOOLEAN LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $operator$
BEGIN
    IF inet_client_addr() IS NOT NULL
       OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname = session_user)
       OR current_setting('role') NOT IN ('none', session_user)
       OR public.storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'inverted projection requires local administrative operator' USING ERRCODE = '42501';
    END IF;
    IF p_enabled IS NULL OR NOT EXISTS (SELECT 1 FROM public.logical_source WHERE id = p_source_id) THEN
        RAISE EXCEPTION 'inverted projection requires a valid source and enabled state' USING ERRCODE = '22023';
    END IF;
    IF public.storage_v2_can_access_source(p_source_id, 'write') IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'inverted projection requires source write authority' USING ERRCODE = '42501';
    END IF;

    IF p_enabled THEN
        INSERT INTO public.storage_v2_inverted_source (source_id, enabled_by)
        VALUES (p_source_id, current_setting('app.user_id')::UUID)
        ON CONFLICT (source_id) DO NOTHING;
    ELSE
        DELETE FROM public.storage_v2_inverted_source WHERE source_id = p_source_id;
    END IF;
    RETURN p_enabled;
END
$operator$;

CREATE FUNCTION public.storage_v2_inverted_source_enabled(p_source_id BIGINT)
RETURNS BOOLEAN LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp AS $source$
    SELECT EXISTS (SELECT 1 FROM public.storage_v2_inverted_source WHERE source_id = p_source_id)
$source$;

CREATE FUNCTION public.storage_v2_pack_inverted_projection(p_vectors TSVECTOR[], p_fingerprints INTEGER[])
RETURNS BYTEA LANGUAGE plpgsql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $codec$
DECLARE
    reconstructed_fingerprints INTEGER[];
    packed BYTEA;
BEGIN
    IF p_vectors IS NULL OR p_fingerprints IS NULL OR array_ndims(p_vectors) IS DISTINCT FROM 1
       OR cardinality(p_vectors) NOT BETWEEN 1 AND 64 OR array_position(p_vectors, NULL) IS NOT NULL THEN
        RAISE EXCEPTION 'inverted projection requires a complete bounded vector block';
    END IF;

    -- Content, context and kind are already combined in the verified
    -- weighted vectors. Group complete lexemes before grouping hash buckets:
    -- repeat occurrences of a lexeme do not make its bucket ambiguous.
    WITH first_positions AS MATERIALIZED (
        SELECT word.lexeme COLLATE "C" AS lexeme, min(segment.ordinal)::INTEGER - 1 AS first_ordinal
          FROM unnest(p_vectors) WITH ORDINALITY AS segment(vector, ordinal)
         CROSS JOIN LATERAL unnest(tsvector_to_array(segment.vector)) AS word(lexeme)
         GROUP BY word.lexeme COLLATE "C"
    ), hashes AS MATERIALIZED (
        SELECT encode(substring(public.digest(lexeme, 'sha256') FROM 1 FOR 4), 'hex') COLLATE "C" AS key,
               first_ordinal
          FROM first_positions
    ), buckets AS (
        SELECT left(key, 4) AS prefix,
               CASE WHEN count(*) = 1 THEN right(min(key), 4) || lpad(to_hex(min(first_ordinal)), 2, '0')
                    ELSE '0000ff' END AS suffix_and_ordinal
          FROM hashes
         GROUP BY left(key, 4)
    )
    SELECT coalesce(array_agg(('x' || prefix)::BIT(16)::INTEGER ORDER BY prefix), ARRAY[]::INTEGER[]),
           decode(coalesce(string_agg(prefix || suffix_and_ordinal, '' ORDER BY prefix), ''), 'hex')
      INTO reconstructed_fingerprints, packed
      FROM buckets;

    IF reconstructed_fingerprints IS DISTINCT FROM p_fingerprints THEN
        RAISE EXCEPTION 'inverted projection fingerprint coverage differs';
    END IF;
    RETURN packed;
END
$codec$;

CREATE FUNCTION public.storage_v2_stage_inverted_projection(
    p_block public.storage_v2_derived_lexical_block, p_vectors TSVECTOR[]
) RETURNS BOOLEAN LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
SET row_security = on AS $stage$
DECLARE
    stored_block public.storage_v2_derived_lexical_block;
    representative public.storage_v2_derived_lexical_block;
    canonical RECORD;
    layout_digest BYTEA;
    staged_payload BYTEA;
    projection public.storage_v2_inverted_projection;
    bound public.storage_v2_inverted_binding;
BEGIN
    -- Serialize source opt-out with any in-flight staging for that source.
    PERFORM 1 FROM public.storage_v2_inverted_source WHERE source_id = p_block.source_id FOR KEY SHARE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    IF public.storage_v2_can_access_source(p_block.source_id, 'write') IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'inverted projection requires source write authority' USING ERRCODE = '42501';
    END IF;

    SELECT block.* INTO STRICT stored_block FROM public.storage_v2_derived_lexical_block block
     WHERE block.occurrence_id = p_block.occurrence_id AND block.block_order = p_block.block_order;
    IF stored_block IS DISTINCT FROM p_block THEN
        RAISE EXCEPTION 'inverted projection stored block identity differs';
    END IF;
    IF cardinality(p_vectors) IS DISTINCT FROM cardinality(stored_block.segment_orders) THEN
        RAISE EXCEPTION 'inverted projection vector count differs from the checked block';
    END IF;

    SELECT document.id, document.profile_id, document.materialization_sha256
      INTO STRICT canonical
      FROM public.occurrence item
      JOIN public.artifact_version artifact ON artifact.id = item.artifact_version_id
       AND artifact.source_id = item.source_id
      JOIN public.storage_v2_search_view_document binding ON binding.view_id = item.view_id AND binding.ordinal = 0
      JOIN public.storage_v2_search_document document ON document.id = binding.document_id
       AND document.component_kind = 'node' AND document.node_id = artifact.content_root_node_id
     WHERE item.id = stored_block.occurrence_id AND item.source_id = stored_block.source_id
       AND item.artifact_version_id = stored_block.artifact_version_id;

    -- Optional derived caches are independent of the layout. The complete
    -- vector membership depends on the canonical text, source positions,
    -- stored prefixes and types, tokenizer, and the writer's A/B/C weights.
    layout_digest := sha256(convert_to(jsonb_build_object(
        'format', 'mainrag.inverted-first.v1',
        'encoding', getdatabaseencoding(),
        'configuration', 'pg_catalog.simple',
        'weights', 'ABC',
        'profile', canonical.profile_id,
        'materialization', encode(canonical.materialization_sha256, 'hex'),
        'segment_orders', stored_block.segment_orders,
        'text_starts', stored_block.text_starts,
        'text_lengths', stored_block.text_lengths,
        'text_hashes', ARRAY(SELECT encode(segment.text_hash, 'hex')
                               FROM unnest(stored_block.text_hashes) WITH ORDINALITY AS segment(text_hash, ordinal)
                              ORDER BY segment.ordinal),
        'byte_starts', stored_block.text_byte_starts,
        'byte_lengths', stored_block.text_byte_lengths,
        'context_prefixes', stored_block.context_prefixes,
        'chunk_types', stored_block.chunk_types
    )::TEXT, 'UTF8'));

    SELECT candidate.* INTO projection FROM public.storage_v2_inverted_projection candidate
     WHERE candidate.source_id = stored_block.source_id AND candidate.document_id = canonical.id
       AND candidate.layout_sha256 = layout_digest;
    IF NOT FOUND THEN
        staged_payload := public.storage_v2_pack_inverted_projection(p_vectors, stored_block.fingerprints);
        INSERT INTO public.storage_v2_inverted_projection (
            source_id, document_id, layout_sha256, representative_occurrence_id,
            representative_block_order, payload, payload_sha256
        ) VALUES (
            stored_block.source_id, canonical.id, layout_digest, stored_block.occurrence_id,
            stored_block.block_order, staged_payload, sha256(staged_payload)
        ) ON CONFLICT (source_id, document_id, layout_sha256) DO NOTHING;
        SELECT candidate.* INTO STRICT projection FROM public.storage_v2_inverted_projection candidate
         WHERE candidate.source_id = stored_block.source_id AND candidate.document_id = canonical.id
           AND candidate.layout_sha256 = layout_digest;
        IF projection.payload IS DISTINCT FROM staged_payload THEN
            RAISE EXCEPTION 'inverted projection payload proof differs';
        END IF;
    END IF;

    IF projection.payload_sha256 IS DISTINCT FROM sha256(projection.payload)
       OR octet_length(projection.payload) IS DISTINCT FROM 5 * cardinality(stored_block.fingerprints) THEN
        RAISE EXCEPTION 'inverted projection payload proof differs';
    END IF;

    IF (projection.representative_occurrence_id, projection.representative_block_order)
       IS DISTINCT FROM (stored_block.occurrence_id, stored_block.block_order) THEN
        SELECT block.* INTO STRICT representative FROM public.storage_v2_derived_lexical_block block
          JOIN public.occurrence item ON item.id = block.occurrence_id AND item.source_id = block.source_id
           AND item.artifact_version_id = block.artifact_version_id
          JOIN public.storage_v2_search_view_document binding ON binding.view_id = item.view_id AND binding.ordinal = 0
          JOIN public.storage_v2_inverted_binding prior ON prior.occurrence_id = block.occurrence_id
           AND prior.block_order = block.block_order AND prior.source_id = block.source_id
           AND prior.artifact_version_id = block.artifact_version_id AND prior.document_id = binding.document_id
           AND prior.layout_sha256 = layout_digest AND prior.payload_sha256 = projection.payload_sha256
         WHERE block.occurrence_id = projection.representative_occurrence_id
           AND block.block_order = projection.representative_block_order
           AND block.source_id = stored_block.source_id AND binding.document_id = canonical.id;
        -- The digest locates a candidate for reuse. The original immutable
        -- layout and fingerprint array are the exact identity check.
        IF (representative.segment_orders, representative.text_starts, representative.text_lengths,
            representative.text_hashes, representative.text_byte_starts, representative.text_byte_lengths,
            representative.context_prefixes, representative.chunk_types, representative.fingerprints)
           IS DISTINCT FROM
           (stored_block.segment_orders, stored_block.text_starts, stored_block.text_lengths,
            stored_block.text_hashes, stored_block.text_byte_starts, stored_block.text_byte_lengths,
            stored_block.context_prefixes, stored_block.chunk_types, stored_block.fingerprints) THEN
            RAISE EXCEPTION 'inverted projection layout identity differs';
        END IF;
    END IF;

    INSERT INTO public.storage_v2_inverted_binding (
        occurrence_id, block_order, source_id, artifact_version_id, document_id, layout_sha256, payload_sha256
    ) VALUES (
        stored_block.occurrence_id, stored_block.block_order, stored_block.source_id,
        stored_block.artifact_version_id, canonical.id, layout_digest, projection.payload_sha256
    ) ON CONFLICT (occurrence_id, block_order) DO NOTHING;
    SELECT binding.* INTO STRICT bound FROM public.storage_v2_inverted_binding binding
     WHERE binding.occurrence_id = stored_block.occurrence_id AND binding.block_order = stored_block.block_order;
    IF (bound.source_id, bound.artifact_version_id, bound.document_id, bound.layout_sha256, bound.payload_sha256)
       IS DISTINCT FROM (stored_block.source_id, stored_block.artifact_version_id, canonical.id,
                         layout_digest, projection.payload_sha256) THEN
        RAISE EXCEPTION 'inverted projection binding identity differs';
    END IF;
    RETURN TRUE;
END
$stage$;

ALTER FUNCTION public.storage_v2_set_inverted_source_enabled(BIGINT, BOOLEAN) OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION public.storage_v2_inverted_source_enabled(BIGINT) OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION public.storage_v2_pack_inverted_projection(TSVECTOR[], INTEGER[]) OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION public.storage_v2_stage_inverted_projection(public.storage_v2_derived_lexical_block, TSVECTOR[])
    OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION public.storage_v2_set_inverted_source_enabled(BIGINT, BOOLEAN),
    public.storage_v2_inverted_source_enabled(BIGINT),
    public.storage_v2_pack_inverted_projection(TSVECTOR[], INTEGER[]),
    public.storage_v2_stage_inverted_projection(public.storage_v2_derived_lexical_block, TSVECTOR[])
    FROM PUBLIC, mainrag;

SELECT public.storage_v2_require_lexical_conversion_operator();
COMMIT;
