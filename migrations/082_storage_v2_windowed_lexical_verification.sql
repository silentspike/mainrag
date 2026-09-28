-- Migration 082: avoid decompressing a complete search document once per segment.
--
-- The verifier still reconstructs every segment from the immutable search
-- document and recomputes both its digest and weighted simple FTS vector.  A
-- direct substring(document.search_text, ...) call made PostgreSQL detoast and
-- decompress the complete document for every segment.  Materialize bounded,
-- overlapping document windows once per occurrence instead.  The overlap is
-- derived from the largest stored segment, so every segment is wholly covered
-- by one window without changing the checked byte range.

CREATE OR REPLACE FUNCTION storage_v2_verify_lexical_segments(p_generation_id BIGINT)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
SET work_mem = '256MB'
AS $$
DECLARE
    v_source_id BIGINT;
    v_result JSONB;
    v_max_segment_length BIGINT;
    v_chunk_size BIGINT;
    v_overlap BIGINT;
    v_stride BIGINT;
BEGIN
    SELECT source_id INTO v_source_id FROM source_generation
     WHERE id = p_generation_id AND status IN ('verified', 'release_candidate');
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'read') THEN
        RAISE EXCEPTION 'verified authorized generation required for lexical verification'
            USING ERRCODE = '42501';
    END IF;

    SELECT COALESCE(max(text_length), 0)
      INTO v_max_segment_length
      FROM storage_v2_lexical_segment
     WHERE source_id = v_source_id;
    v_overlap := GREATEST(4096::BIGINT, v_max_segment_length);
    v_chunk_size := GREATEST(65536::BIGINT, v_overlap + 4096);
    v_stride := v_chunk_size - v_overlap;
    IF v_stride <= 0 OR v_chunk_size > 2147483647 THEN
        RAISE EXCEPTION 'lexical verification window bounds are invalid';
    END IF;

    WITH visible AS MATERIALIZED (
        SELECT occurrence_row.id, document.id AS document_id,
               document.search_text
          FROM source_generation generation
          JOIN generation_item_version membership
            ON membership.source_id = generation.source_id
           AND membership.valid_from_seq <= generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > generation.generation_seq)
          JOIN artifact_version artifact
            ON artifact.id = membership.artifact_version_id
          JOIN occurrence occurrence_row
            ON occurrence_row.source_id = generation.source_id
           AND occurrence_row.artifact_version_id = artifact.id
          LEFT JOIN storage_v2_search_view_document binding
            ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
          LEFT JOIN storage_v2_search_document document
            ON document.id = binding.document_id
         WHERE generation.id = p_generation_id
    ), segment_base AS MATERIALIZED (
        SELECT visible.id AS occurrence_id,
               visible.document_id,
               lexical.text_start,
               lexical.text_length,
               lexical.text_sha256,
               lexical.context_prefix,
               lexical.chunk_type,
               lexical.fts_vector,
               ((lexical.text_start - 1) / v_stride) * v_stride + 1
                   AS chunk_start
          FROM visible
          JOIN storage_v2_lexical_segment lexical
            ON lexical.occurrence_id = visible.id
    ), chunk_bounds AS MATERIALIZED (
        SELECT DISTINCT occurrence_id, chunk_start
          FROM segment_base
    ), document_chunks AS MATERIALIZED (
        SELECT chunk_bounds.occurrence_id,
               chunk_bounds.chunk_start,
               substring(visible.search_text
                         FROM chunk_bounds.chunk_start::INTEGER
                         FOR v_chunk_size::INTEGER) AS chunk_text
          FROM chunk_bounds
          JOIN visible ON visible.id = chunk_bounds.occurrence_id
    ), segment_values AS MATERIALIZED (
        SELECT segment_base.occurrence_id,
               segment_base.document_id,
               segment_base.text_sha256,
               segment_base.context_prefix,
               segment_base.chunk_type,
               segment_base.fts_vector,
               substring(document_chunks.chunk_text
                         FROM (segment_base.text_start
                               - document_chunks.chunk_start + 1)::INTEGER
                         FOR segment_base.text_length::INTEGER) AS segment_text
          FROM segment_base
          JOIN document_chunks
            ON document_chunks.occurrence_id = segment_base.occurrence_id
           AND document_chunks.chunk_start = segment_base.chunk_start
    ), segment_checks AS (
        SELECT occurrence_id,
               count(*) AS segment_count,
               count(*) FILTER (WHERE
                   sha256(convert_to(segment_text, 'UTF8')) <> text_sha256
                   OR fts_vector IS DISTINCT FROM
                      (setweight(to_tsvector('simple', segment_text), 'A')
                       || setweight(to_tsvector('simple', context_prefix), 'B')
                       || setweight(to_tsvector('simple', chunk_type), 'C'))
               ) AS invalid_count
          FROM segment_values
         GROUP BY occurrence_id
    ), checked AS (
        SELECT visible.document_id,
               visible.search_text,
               COALESCE(segment_checks.segment_count, 0) AS segment_count,
               COALESCE(segment_checks.invalid_count, 0) AS invalid_count
          FROM visible
          LEFT JOIN segment_checks
            ON segment_checks.occurrence_id = visible.id
    )
    SELECT jsonb_build_object(
        'schema_version', 'mainrag.storage-v2.lexical-segment-verification.v1',
        'generation_id', p_generation_id,
        'occurrence_count', count(*),
        'segment_count', COALESCE(sum(segment_count), 0),
        'missing_count', count(*) FILTER (
            WHERE document_id IS NULL
               OR (search_text <> '' AND segment_count = 0)
        ),
        'invalid_count', COALESCE(sum(invalid_count), 0)
    ) INTO v_result FROM checked;
    IF (v_result ->> 'missing_count')::BIGINT <> 0
       OR (v_result ->> 'invalid_count')::BIGINT <> 0 THEN
        RAISE EXCEPTION 'lexical segment projection is incomplete or differs from immutable source';
    END IF;
    RETURN v_result;
END
$$;

ALTER FUNCTION storage_v2_verify_lexical_segments(BIGINT)
    OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_verify_lexical_segments(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_verify_lexical_segments(BIGINT) TO mainrag;
