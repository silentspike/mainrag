-- Verify every retained segment without repeatedly scanning long UTF-8 prefixes.
-- Prove ASCII over the complete document before using character offsets as byte
-- offsets. Other documents retain independent character/byte checks in smaller
-- overlapping windows; hashes, weighted vectors, authority and counts are unchanged.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
DO $guard$
BEGIN
    IF encode(sha256(convert_to(pg_get_functiondef(
            'storage_v2_verify_lexical_segments(bigint)'::REGPROCEDURE), 'UTF8')), 'hex')
            <> '788bd356bb01a70bc340a01a7ea5bb7ffa46266c579e4efd53ae3d1f5f70970b'
       OR (SELECT proowner FROM pg_proc WHERE oid =
            'storage_v2_verify_lexical_segments(bigint)'::REGPROCEDURE)
            <> 'mainrag_v2_frontier_owner'::REGROLE THEN
        RAISE EXCEPTION 'byte-sliced lexical verifier predecessor or authority differs';
    END IF;
END $guard$;

CREATE OR REPLACE FUNCTION storage_v2_verify_lexical_segments(p_generation_id BIGINT)
RETURNS JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
SET work_mem = '8MB'
SET enable_nestloop = on
AS $$
DECLARE
    v_source_id BIGINT;
    v_item RECORD;
    v_group RECORD;
    v_bytes BYTEA;
    v_byte_count BIGINT;
    v_character_count BIGINT;
    v_ascii BOOLEAN;
    v_byte_start BIGINT;
    v_byte_length INTEGER;
    v_window TEXT;
    v_window_start BIGINT;
    v_window_length INTEGER;
    v_overlap BIGINT;
    v_chunk_size BIGINT;
    v_stride BIGINT;
    v_occurrences BIGINT := 0;
    v_segments BIGINT := 0;
    v_item_segments BIGINT;
    v_missing BIGINT := 0;
    v_invalid BIGINT := 0;
    v_group_invalid BIGINT;
BEGIN
    SELECT source_id INTO v_source_id FROM source_generation
     WHERE id = p_generation_id AND status IN ('verified', 'release_candidate');
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'read') THEN
        RAISE EXCEPTION 'verified authorized generation required for lexical verification'
            USING ERRCODE = '42501';
    END IF;

    -- Length metadata has no text/vector payload and includes all representations.
    SELECT GREATEST(4096::BIGINT, COALESCE(max(length), 0)) INTO v_overlap
      FROM (
        SELECT text_length AS length FROM storage_v2_lexical_segment
         WHERE source_id = v_source_id
        UNION ALL
        SELECT length FROM storage_v2_compact_lexical_block block
         CROSS JOIN LATERAL unnest(block.text_lengths) item(length)
         WHERE block.source_id = v_source_id
        UNION ALL
        SELECT length FROM storage_v2_derived_lexical_block block
         CROSS JOIN LATERAL unnest(block.text_lengths) item(length)
         WHERE block.source_id = v_source_id
      ) lengths;
    v_chunk_size := GREATEST(65536::BIGINT, v_overlap + 4096);
    v_stride := v_chunk_size - v_overlap;
    IF v_stride <= 0 OR v_chunk_size > 2147483647 THEN
        RAISE EXCEPTION 'lexical verification window bounds are invalid';
    END IF;

    FOR v_item IN
        SELECT occurrence_row.id, document.id AS document_id
          FROM source_generation generation
          JOIN generation_item_version membership
            ON membership.source_id = generation.source_id
           AND membership.valid_from_seq <= generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > generation.generation_seq)
          JOIN occurrence occurrence_row
            ON occurrence_row.source_id = generation.source_id
           AND occurrence_row.artifact_version_id = membership.artifact_version_id
          LEFT JOIN storage_v2_search_view_document binding
            ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
          LEFT JOIN storage_v2_search_document document ON document.id = binding.document_id
         WHERE generation.id = p_generation_id
    LOOP
        v_occurrences := v_occurrences + 1;
        IF v_item.document_id IS NULL THEN
            v_missing := v_missing + 1;
            CONTINUE;
        END IF;
        SELECT convert_to(search_text, 'UTF8') INTO v_bytes
          FROM storage_v2_search_document WHERE id = v_item.document_id;
        v_byte_count := octet_length(v_bytes);
        -- UTF-8 encodes each non-ASCII character in at least two bytes. Equality
        -- over the complete canonical document proves every character offset is
        -- the same byte offset; an ASCII prefix alone cannot grant this path.
        v_character_count := char_length(convert_from(v_bytes, 'UTF8'));
        v_ascii := v_character_count = v_byte_count;
        v_byte_start := 1;
        v_window_start := 1 - v_stride;
        v_window := '';
        v_item_segments := 0;

        FOR v_group IN
            WITH segments AS (
                SELECT segment_order, text_start, text_length, text_sha256,
                       context_prefix, chunk_type, fts_vector,
                       NULL::INTEGER AS byte_start, NULL::INTEGER AS byte_length
                  FROM storage_v2_lexical_segment WHERE occurrence_id = v_item.id
                UNION ALL
                SELECT item.*, NULL::INTEGER, NULL::INTEGER
                  FROM storage_v2_compact_lexical_block block
                  CROSS JOIN LATERAL unnest(block.segment_orders, block.text_starts,
                      block.text_lengths, block.text_hashes, block.context_prefixes,
                      block.chunk_types, block.fts_vectors)
                      item(segment_order, text_start, text_length, text_sha256,
                           context_prefix, chunk_type, fts_vector)
                 WHERE block.occurrence_id = v_item.id
                UNION ALL
                SELECT item.segment_order, item.text_start, item.text_length,
                       item.text_sha256, item.context_prefix, item.chunk_type,
                       NULL::TSVECTOR, item.byte_start, item.byte_length
                  FROM storage_v2_derived_lexical_block block
                  CROSS JOIN LATERAL unnest(block.segment_orders, block.text_starts,
                      block.text_lengths, block.text_hashes, block.context_prefixes,
                      block.chunk_types, block.text_byte_starts, block.text_byte_lengths)
                      item(segment_order, text_start, text_length, text_sha256,
                           context_prefix, chunk_type, byte_start, byte_length)
                 WHERE block.occurrence_id = v_item.id
            ), ordered AS (
                SELECT *, ((text_start - 1) / v_stride) * v_stride + 1 AS chunk_start,
                       (row_number() OVER (ORDER BY text_start, segment_order) - 1) / 256 AS batch
                  FROM segments
            )
            SELECT chunk_start, batch, count(*) AS segment_count,
                   array_agg(text_start) AS starts, array_agg(text_length) AS lengths,
                   array_agg(text_sha256) AS hashes, array_agg(context_prefix) AS prefixes,
                   array_agg(chunk_type) AS kinds, array_agg(fts_vector) AS vectors,
                   array_agg(byte_start) AS byte_starts, array_agg(byte_length) AS byte_lengths
              FROM ordered GROUP BY chunk_start, batch ORDER BY chunk_start, batch
        LOOP
            -- Advance by whole characters, never substring from a global character
            -- offset. Trim a maximum of three trailing UTF-8 continuation bytes.
            WHILE NOT v_ascii AND v_window_start < v_group.chunk_start LOOP
                IF v_window_start > 0 THEN
                    IF v_window_length < v_stride THEN
                        RAISE EXCEPTION 'lexical segment projection is incomplete or differs from immutable source';
                    END IF;
                    v_byte_start := v_byte_start + octet_length(
                        substring(v_window FROM 1 FOR v_stride::INTEGER));
                END IF;
                v_window_start := v_window_start + v_stride;
                v_byte_length := LEAST(2147483647::BIGINT, 4 * v_chunk_size,
                                      v_byte_count - v_byte_start + 1)::INTEGER;
                WHILE v_byte_length > 0
                  AND v_byte_start + v_byte_length <= v_byte_count
                  AND get_byte(v_bytes, (v_byte_start + v_byte_length - 1)::INTEGER)
                      BETWEEN 128 AND 191 LOOP
                    v_byte_length := v_byte_length - 1;
                END LOOP;
                v_window := substring(convert_from(substring(v_bytes
                    FROM v_byte_start::INTEGER FOR v_byte_length), 'UTF8')
                    FROM 1 FOR v_chunk_size::INTEGER);
                v_window_length := char_length(v_window);
            END LOOP;

            WITH pieces AS MATERIALIZED (
                SELECT item.*, CASE WHEN v_ascii THEN
                       convert_from(substring(v_bytes FROM item.start::INTEGER
                                              FOR item.length::INTEGER), 'UTF8')
                       ELSE substring(v_window
                           FROM (item.start - v_window_start + 1)::INTEGER
                           FOR item.length::INTEGER) END AS text
                  FROM unnest(v_group.starts, v_group.lengths, v_group.hashes,
                       v_group.prefixes, v_group.kinds, v_group.vectors,
                       v_group.byte_starts, v_group.byte_lengths)
                       item(start, length, hash, prefix, kind, vector, byte_start, byte_length)
            ), distinct_inputs AS MATERIALIZED (
                -- Repeated log/conversation segments still receive independent
                -- digest and stored-vector checks. Only identical calculation
                -- inputs share one FTS reconstruction in this bounded group.
                SELECT DISTINCT text COLLATE "C" AS text,
                       prefix COLLATE "C" AS prefix, kind COLLATE "C" AS kind
                  FROM pieces WHERE byte_start IS NULL
            ), expected_vectors AS MATERIALIZED (
                SELECT text, prefix, kind,
                       setweight(to_tsvector('simple', text COLLATE "default"), 'A')
                       || setweight(to_tsvector('simple', prefix COLLATE "default"), 'B')
                       || setweight(to_tsvector('simple', kind COLLATE "default"), 'C') AS expected
                  FROM distinct_inputs
            )
            SELECT count(*) FILTER (WHERE
                pieces.start < 1 OR pieces.length < 0
                OR pieces.start - 1 + pieces.length > v_character_count
                OR char_length(pieces.text) <> pieces.length
                OR sha256(convert_to(pieces.text, 'UTF8')) IS DISTINCT FROM pieces.hash
                OR (pieces.byte_start IS NULL AND pieces.vector IS DISTINCT FROM expected_vectors.expected)
                OR (pieces.byte_start IS NOT NULL AND substring(v_bytes
                    FROM pieces.byte_start FOR pieces.byte_length)
                    IS DISTINCT FROM convert_to(pieces.text, 'UTF8'))
            ) INTO v_group_invalid FROM pieces LEFT JOIN expected_vectors
              ON pieces.byte_start IS NULL
             AND expected_vectors.text = pieces.text COLLATE "C"
             AND expected_vectors.prefix = pieces.prefix COLLATE "C"
             AND expected_vectors.kind = pieces.kind COLLATE "C";
            v_item_segments := v_item_segments + v_group.segment_count;
            v_invalid := v_invalid + v_group_invalid;
        END LOOP;
        v_segments := v_segments + v_item_segments;
        IF v_byte_count > 0 AND v_item_segments = 0 THEN
            v_missing := v_missing + 1;
        END IF;
        v_bytes := NULL;
    END LOOP;
    IF v_missing <> 0 OR v_invalid <> 0 THEN
        RAISE EXCEPTION 'lexical segment projection is incomplete or differs from immutable source';
    END IF;
    RETURN jsonb_build_object(
        'schema_version', 'mainrag.storage-v2.lexical-segment-verification.v1',
        'generation_id', p_generation_id, 'occurrence_count', v_occurrences,
        'segment_count', v_segments, 'missing_count', v_missing, 'invalid_count', v_invalid);
END $$;
ALTER FUNCTION storage_v2_verify_lexical_segments(BIGINT) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_verify_lexical_segments(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_verify_lexical_segments(BIGINT) TO mainrag;
COMMIT;
