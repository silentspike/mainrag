-- Reuse bounded weighted-vector projections without changing lexical payloads.
-- One exact predecessor writer routine changes. Readers and verifier routines,
-- canonical bytes, weighted vectors, authority and replay checks stay intact.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
DO $guard$
DECLARE signature TEXT:='storage_v2_put_lexical_segments_document_private(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[],bigint,bytea,integer,integer)';
BEGIN
 IF encode(sha256(convert_to(pg_get_functiondef(signature::REGPROCEDURE),'UTF8')),'hex')
      IS DISTINCT FROM '230b56c709b9375805c2f8f95ad7639b10cf1ab3ac8e48a871b2041cbe8c4110'
    OR (SELECT proowner FROM pg_proc WHERE oid=signature::REGPROCEDURE)
         <>'mainrag_v2_frontier_owner'::REGROLE THEN
  RAISE EXCEPTION 'shared lexical projection predecessor or authority differs';
 END IF;
END $guard$;

CREATE OR REPLACE FUNCTION storage_v2_put_lexical_segments_document_private(
 p_occurrence_id BIGINT,p_artifact_version_id BIGINT,p_segment_orders BIGINT[],p_texts TEXT[],
 p_context_prefixes TEXT[],p_chunk_types TEXT[],p_character_starts BIGINT[],p_byte_starts BIGINT[],
 p_source_id BIGINT,p_source_bytes BYTEA,p_source_character_count INTEGER,p_source_byte_count INTEGER
) RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $function$
DECLARE
    v_source_id BIGINT := p_source_id;
    v_source_bytes BYTEA := p_source_bytes;
    v_byte_window_start BIGINT;
    v_byte_window_end BIGINT;
    v_window_start BIGINT;
    v_window_end BIGINT;
    v_count INTEGER;
    v_distinct INTEGER;
    v_valid BOOLEAN;
    v_inserted INTEGER;
    v_starts INTEGER[];
    v_lengths INTEGER[];
    v_hashes BYTEA[];
    v_vectors TSVECTOR[];
    v_masks BIT(128)[];
    v_block_masks BIT(128)[];
    v_block_fingerprints INTEGER[];
    v_shared BOOLEAN;
    v_vector_bytes BIGINT;
    v_first_terms TEXT[];
    v_first_ordinals SMALLINT[];
    v_existing_derived public.storage_v2_derived_lexical_block;
    v_byte_lengths INTEGER[];
    v_low INTEGER;
    v_high INTEGER;
    v_existing_block public.storage_v2_compact_lexical_block;
BEGIN
    v_count := cardinality(p_segment_orders);
    IF v_count IS NULL OR v_count < 1 OR v_count > 256
       OR cardinality(p_texts) IS DISTINCT FROM v_count
       OR cardinality(p_context_prefixes) IS DISTINCT FROM v_count
       OR cardinality(p_chunk_types) IS DISTINCT FROM v_count
       OR cardinality(p_character_starts) IS DISTINCT FROM v_count
       OR cardinality(p_byte_starts) IS DISTINCT FROM v_count THEN
        RAISE EXCEPTION 'bounded lexical segment group required';
    END IF;
    -- Only finish can invoke this private helper. Bytes and anchors are
    -- independently obtained from its bound canonical document in this xact.
    IF NOT storage_v2_can_access_source(v_source_id, 'write') THEN
        RAISE EXCEPTION 'authorized source-backed lexical segment required'
            USING ERRCODE='42501';
    END IF;
    SELECT min(character_start), max(character_start + char_length(segment_text) - 1),
           min(byte_start), max(byte_start + octet_length(segment_text) - 1),
           bool_and(COALESCE(character_start BETWEEN 1 AND 2147483647
                    AND byte_start BETWEEN 1 AND 2147483647
                    AND segment_text <> '', FALSE))
      INTO v_window_start, v_window_end, v_byte_window_start, v_byte_window_end, v_valid
      FROM unnest(p_character_starts, p_byte_starts, p_texts)
           AS bounds(character_start, byte_start, segment_text);
    IF v_valid IS NOT TRUE OR v_window_start IS NULL
       OR v_window_end > p_source_character_count
       OR v_window_end - v_window_start + 1 > 8388608
       OR v_byte_window_end > p_source_byte_count
       OR v_byte_window_end - v_byte_window_start + 1 > 33554432 THEN
        RAISE EXCEPTION 'bounded source character and byte window required';
    END IF;
    -- All distinct document anchors were independently decoded once by
    -- finish; no caller can supply or modify this trusted-owned mapping.
    WITH prepared AS MATERIALIZED (
        SELECT input.*, input.character_start::INTEGER AS text_start,
               char_length(segment_text) AS text_length,
               byte_start::INTEGER AS text_byte_start,
               octet_length(segment_text) AS text_byte_length,
               mapped.character_start AS verified_character_start,
               sha256(convert_to(segment_text, 'UTF8')) AS text_sha256,
               setweight(to_tsvector('simple', segment_text), 'A')
               || setweight(to_tsvector('simple', context_prefix), 'B')
               || setweight(to_tsvector('simple', chunk_type), 'C') AS fts_vector
          FROM unnest(p_segment_orders, p_texts, p_context_prefixes, p_chunk_types,
                      p_character_starts, p_byte_starts)
               WITH ORDINALITY AS input(segment_order, segment_text, context_prefix,
                   chunk_type, character_start, byte_start, ordinal)
          JOIN pg_temp.mainrag_v2_lexical_anchors mapped USING (byte_start)
    )
    SELECT count(DISTINCT segment_order),
           bool_and(COALESCE(segment_order >= 0 AND segment_text <> ''
                    AND context_prefix IS NOT NULL AND chunk_type IS NOT NULL
                    AND chunk_type <> '' AND text_start = verified_character_start
                    AND substring(v_source_bytes FROM text_byte_start
                                  FOR text_byte_length) = convert_to(segment_text, 'UTF8'), FALSE)),
           array_agg(text_start ORDER BY ordinal), array_agg(text_length ORDER BY ordinal),
           array_agg(text_sha256 ORDER BY ordinal), array_agg(fts_vector ORDER BY ordinal)
      INTO v_distinct, v_valid, v_starts, v_lengths, v_hashes, v_vectors FROM prepared;
    IF v_distinct <> v_count OR v_valid IS NOT TRUE THEN
        RAISE EXCEPTION 'valid source-backed lexical segment group required';
    END IF;
    -- Reuse all preceding independent byte/character/text validations.
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'mainrag.lexical-segment:'||p_occurrence_id::TEXT,0));
    IF p_source_byte_count>=262144
       AND array_lower(p_segment_orders,1)=1 AND p_segment_orders[1]%64=0
       AND p_segment_orders=ARRAY(SELECT generate_series(p_segment_orders[1],p_segment_orders[1]+v_count-1))
       AND NOT EXISTS(SELECT 1 FROM public.storage_v2_lexical_segment WHERE occurrence_id=p_occurrence_id)
       AND NOT EXISTS(SELECT 1 FROM public.storage_v2_compact_lexical_block WHERE occurrence_id=p_occurrence_id) THEN
        SELECT array_agg(octet_length(value) ORDER BY ordinal) INTO v_byte_lengths
          FROM unnest(p_texts) WITH ORDINALITY item(value,ordinal);
        -- The fast projection shares lexeme expansion across the bounded
        -- group. Larger inputs retain the original vector algorithms.
        SELECT coalesce(sum(octet_length(t)+octet_length(p)+octet_length(k)),0)<=8388608
          INTO v_shared FROM unnest(p_texts,p_context_prefixes,p_chunk_types) item(t,p,k);
        SELECT coalesce(sum(pg_column_size(value)),0) INTO v_vector_bytes
          FROM unnest(v_vectors) vector(value);
        -- Relational term membership has fixed aggregation overhead. Small
        -- weighted vectors retain the preceding per-vector routine; at least
        -- 2 KiB per vector admits sharing, still within the 8 MiB scratch cap.
        -- This is a resource/density admission, not a throughput guarantee.
        v_shared:=v_shared AND v_vector_bytes<=8388608
                           AND v_vector_bytes>=v_count::BIGINT*2048;
        IF NOT v_shared THEN
            SELECT array_agg(public.storage_v2_lexical_segment_mask(
                       public.storage_v2_posting_fingerprints(tsvector_to_array(value))) ORDER BY ordinal)
              INTO v_masks FROM unnest(v_vectors) WITH ORDINALITY vector(value,ordinal);
        END IF;
        FOR v_low IN SELECT generate_series(1,v_count,64) LOOP
            v_high:=LEAST(v_low+63,v_count);
            IF v_shared THEN
                -- Aggregate each complete lexeme once within this block. The
                -- same membership supplies masks, fingerprints and first-hit
                -- ordinals without joining the expanded lexemes back to hashes.
                WITH terms AS MATERIALIZED (
                    SELECT term,array_agg(ordinal ORDER BY ordinal) AS ordinals,
                           min(ordinal)::SMALLINT AS first_ordinal,count(*) AS segments
                      FROM unnest(v_vectors[v_low:v_high]) WITH ORDINALITY vector(value,ordinal)
                      CROSS JOIN LATERAL unnest(tsvector_to_array(value)) lexeme(term)
                     GROUP BY term
                ), fingerprints AS MATERIALIZED (
                    SELECT terms.*,public.storage_v2_posting_fingerprint(term) AS fingerprint
                      FROM terms
                ), segment_fingerprints AS MATERIALIZED (
                    SELECT ordinal,array_agg(DISTINCT fingerprint ORDER BY fingerprint) AS fingerprints
                      FROM fingerprints CROSS JOIN LATERAL unnest(ordinals) segment(ordinal)
                     GROUP BY ordinal
                ), selected AS MATERIALIZED (
                    SELECT term,first_ordinal,segments FROM terms WHERE octet_length(term)<=8
                     ORDER BY octet_length(term),segments DESC,term COLLATE "C" LIMIT 16
                )
                SELECT ARRAY(SELECT public.storage_v2_lexical_segment_mask(
                               coalesce(segment.fingerprints,ARRAY[]::INTEGER[]))
                             FROM generate_series(1,v_high-v_low+1) ordinal
                             LEFT JOIN segment_fingerprints segment USING(ordinal) ORDER BY ordinal),
                       ARRAY(SELECT DISTINCT fingerprint FROM fingerprints ORDER BY fingerprint),
                       coalesce(array_agg(term ORDER BY octet_length(term),segments DESC,term COLLATE "C"),ARRAY[]::TEXT[]),
                       coalesce(array_agg(first_ordinal ORDER BY octet_length(term),segments DESC,term COLLATE "C"),ARRAY[]::SMALLINT[])
                  INTO v_block_masks,v_block_fingerprints,v_first_terms,v_first_ordinals FROM selected;
            ELSE
                v_block_masks:=v_masks[v_low:v_high];
                v_block_fingerprints:=public.storage_v2_lexical_block_fingerprints(v_vectors[v_low:v_high]);
                SELECT cache.terms,cache.ordinals INTO v_first_terms,v_first_ordinals
                  FROM public.storage_v2_lexical_first_term_cache(v_vectors[v_low:v_high]) cache;
            END IF;
            INSERT INTO public.storage_v2_derived_lexical_block(
                occurrence_id,source_id,artifact_version_id,block_order,segment_orders,
                text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,
                fingerprints,text_byte_starts,text_byte_lengths,segment_masks,first_terms,first_term_ordinals
            ) VALUES(p_occurrence_id,v_source_id,p_artifact_version_id,p_segment_orders[v_low]/64,
                p_segment_orders[v_low:v_high],v_starts[v_low:v_high],v_lengths[v_low:v_high],
                v_hashes[v_low:v_high],p_context_prefixes[v_low:v_high],p_chunk_types[v_low:v_high],
                v_block_fingerprints,
                p_byte_starts[v_low:v_high]::INTEGER[],v_byte_lengths[v_low:v_high],v_block_masks,v_first_terms,v_first_ordinals)
            ON CONFLICT(occurrence_id,block_order) DO NOTHING;
            SELECT * INTO STRICT v_existing_derived FROM public.storage_v2_derived_lexical_block
             WHERE occurrence_id=p_occurrence_id AND block_order=p_segment_orders[v_low]/64;
            IF (v_existing_derived.source_id,v_existing_derived.artifact_version_id,
                v_existing_derived.segment_orders,v_existing_derived.text_starts,
                v_existing_derived.text_lengths,v_existing_derived.text_hashes,
                v_existing_derived.context_prefixes,v_existing_derived.chunk_types,
                v_existing_derived.fingerprints,v_existing_derived.text_byte_starts,
                v_existing_derived.text_byte_lengths) IS DISTINCT FROM
               (v_source_id,p_artifact_version_id,p_segment_orders[v_low:v_high],
                v_starts[v_low:v_high],v_lengths[v_low:v_high],v_hashes[v_low:v_high],
                p_context_prefixes[v_low:v_high],p_chunk_types[v_low:v_high],
                v_block_fingerprints,
                p_byte_starts[v_low:v_high]::INTEGER[],v_byte_lengths[v_low:v_high])
               OR (v_existing_derived.segment_masks IS NOT NULL
                   AND v_existing_derived.segment_masks IS DISTINCT FROM v_block_masks)
               OR (v_existing_derived.first_terms IS NOT NULL
                   AND (v_existing_derived.first_terms,v_existing_derived.first_term_ordinals)
                       IS DISTINCT FROM (v_first_terms,v_first_ordinals)) THEN
                RAISE EXCEPTION 'lexical segment identity collision';
            END IF;
        END LOOP;
        RETURN v_count;
    END IF;
    -- All payload values were independently validated above.
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'mainrag.lexical-segment-source:'||v_source_id::TEXT,0));
    IF NOT EXISTS(SELECT 1 FROM public.storage_v2_derived_lexical_block
                    WHERE occurrence_id=p_occurrence_id
                      AND block_order BETWEEN p_segment_orders[1]/64 AND p_segment_orders[v_count]/64)
       AND array_lower(p_segment_orders,1)=1 AND p_segment_orders[1]%64=0
       AND p_segment_orders=ARRAY(SELECT generate_series(p_segment_orders[1],
                                     p_segment_orders[1]+v_count-1))
       AND NOT EXISTS (SELECT 1 FROM public.storage_v2_lexical_segment old
                        WHERE old.occurrence_id=p_occurrence_id
                          AND old.segment_order=ANY(p_segment_orders))
       AND NOT EXISTS (
           SELECT 1 FROM public.storage_v2_compact_lexical_block block
            WHERE block.occurrence_id=p_occurrence_id
              AND block.block_order BETWEEN p_segment_orders[1]/64
                                        AND p_segment_orders[v_count]/64
              AND cardinality(block.segment_orders)<>
                  LEAST(64,v_count-(block.block_order-p_segment_orders[1]/64)*64)
       ) THEN
        FOR v_low IN SELECT generate_series(1,v_count,64) LOOP
            v_high:=LEAST(v_low+63,v_count);
            INSERT INTO public.storage_v2_compact_lexical_block(
                occurrence_id,source_id,artifact_version_id,block_order,segment_orders,
                text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,fts_vectors
            ) VALUES(p_occurrence_id,v_source_id,p_artifact_version_id,p_segment_orders[v_low]/64,
                p_segment_orders[v_low:v_high],v_starts[v_low:v_high],v_lengths[v_low:v_high],
                v_hashes[v_low:v_high],p_context_prefixes[v_low:v_high],
                p_chunk_types[v_low:v_high],v_vectors[v_low:v_high])
            ON CONFLICT(occurrence_id,block_order) DO NOTHING;
            SELECT * INTO STRICT v_existing_block FROM public.storage_v2_compact_lexical_block
             WHERE occurrence_id=p_occurrence_id AND block_order=p_segment_orders[v_low]/64;
            IF (v_existing_block.source_id,v_existing_block.artifact_version_id,
                v_existing_block.segment_orders,v_existing_block.text_starts,
                v_existing_block.text_lengths,v_existing_block.text_hashes,
                v_existing_block.context_prefixes,v_existing_block.chunk_types,
                v_existing_block.fts_vectors) IS DISTINCT FROM
               (v_source_id,p_artifact_version_id,p_segment_orders[v_low:v_high],
                v_starts[v_low:v_high],v_lengths[v_low:v_high],v_hashes[v_low:v_high],
                p_context_prefixes[v_low:v_high],p_chunk_types[v_low:v_high],v_vectors[v_low:v_high]) THEN
                RAISE EXCEPTION 'lexical segment identity collision';
            END IF;
        END LOOP;
        RETURN v_count;
    END IF;
    INSERT INTO storage_v2_lexical_segment (
        occurrence_id, source_id, artifact_version_id, segment_order,
        text_start, text_length, text_sha256, context_prefix, chunk_type, fts_vector
    )
    SELECT p_occurrence_id, v_source_id, p_artifact_version_id, input.*
      FROM unnest(p_segment_orders, v_starts, v_lengths, v_hashes,
                  p_context_prefixes, p_chunk_types, v_vectors) AS input
    ON CONFLICT (occurrence_id, segment_order) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    IF v_inserted <> v_count AND EXISTS (
        SELECT 1
          FROM unnest(p_segment_orders, v_starts, v_lengths, v_hashes,
                      p_context_prefixes, p_chunk_types, v_vectors)
               AS input(segment_order, text_start, text_length, text_sha256,
                        context_prefix, chunk_type, fts_vector)
          LEFT JOIN public.storage_v2_lexical_segment_all stored
            ON stored.occurrence_id=p_occurrence_id AND stored.segment_order=input.segment_order
         WHERE (stored.source_id, stored.artifact_version_id, stored.text_start,
                stored.text_length, stored.text_sha256, stored.context_prefix,
                stored.chunk_type, stored.fts_vector) IS DISTINCT FROM
               (v_source_id, p_artifact_version_id, input.text_start, input.text_length,
                input.text_sha256, input.context_prefix, input.chunk_type, input.fts_vector)
    ) THEN
        RAISE EXCEPTION 'lexical segment identity collision';
    END IF;
    RETURN v_count;
END
$function$;

COMMIT;
