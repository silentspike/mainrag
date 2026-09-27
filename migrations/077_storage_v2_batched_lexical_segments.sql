-- Stage bounded groups of immutable source-backed lexical segments with one
-- document lookup and one indexed insert per group. The source authorization,
-- exact-text witness, vector, and collision checks match the single-row writer.
CREATE FUNCTION storage_v2_put_lexical_segments(
    p_occurrence_id BIGINT,
    p_artifact_version_id BIGINT,
    p_segment_orders BIGINT[],
    p_texts TEXT[],
    p_context_prefixes TEXT[],
    p_chunk_types TEXT[]
) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
DECLARE
    v_source_id BIGINT;
    v_search_text TEXT;
    v_count INTEGER;
    v_distinct INTEGER;
    v_valid BOOLEAN;
    v_inserted INTEGER;
BEGIN
    v_count := cardinality(p_segment_orders);
    IF v_count IS NULL OR v_count < 1 OR v_count > 256
       OR cardinality(p_texts) IS DISTINCT FROM v_count
       OR cardinality(p_context_prefixes) IS DISTINCT FROM v_count
       OR cardinality(p_chunk_types) IS DISTINCT FROM v_count THEN
        RAISE EXCEPTION 'bounded lexical segment group required';
    END IF;

    SELECT occurrence_row.source_id, document.search_text
      INTO v_source_id, v_search_text
      FROM occurrence occurrence_row
      JOIN artifact_version artifact
        ON artifact.id = occurrence_row.artifact_version_id
      JOIN storage_v2_search_view_document binding
        ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
      JOIN storage_v2_search_document document
        ON document.id = binding.document_id
       AND document.component_kind = 'node'
       AND document.node_id = artifact.content_root_node_id
     WHERE occurrence_row.id = p_occurrence_id
       AND occurrence_row.artifact_version_id = p_artifact_version_id;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'write') THEN
        RAISE EXCEPTION 'authorized source-backed lexical segment required'
            USING ERRCODE = '42501';
    END IF;

    SELECT count(DISTINCT segment_order),
           bool_and(COALESCE(segment_order >= 0 AND segment_text <> ''
                    AND context_prefix IS NOT NULL
                    AND chunk_type IS NOT NULL AND chunk_type <> ''
                    AND strpos(v_search_text, segment_text) > 0, FALSE))
      INTO v_distinct, v_valid
      FROM unnest(p_segment_orders, p_texts, p_context_prefixes, p_chunk_types)
           AS input(segment_order, segment_text, context_prefix, chunk_type);
    IF v_distinct <> v_count OR v_valid IS NOT TRUE THEN
        RAISE EXCEPTION 'valid source-backed lexical segment group required';
    END IF;

    INSERT INTO storage_v2_lexical_segment (
        occurrence_id, source_id, artifact_version_id, segment_order,
        text_start, text_length, text_sha256, context_prefix, chunk_type,
        fts_vector
    )
    SELECT p_occurrence_id, v_source_id, p_artifact_version_id,
           input.segment_order, strpos(v_search_text, input.segment_text),
           char_length(input.segment_text),
           sha256(convert_to(input.segment_text, 'UTF8')),
           input.context_prefix, input.chunk_type,
           setweight(to_tsvector('simple', input.segment_text), 'A')
           || setweight(to_tsvector('simple', input.context_prefix), 'B')
           || setweight(to_tsvector('simple', input.chunk_type), 'C')
      FROM unnest(p_segment_orders, p_texts, p_context_prefixes, p_chunk_types)
           AS input(segment_order, segment_text, context_prefix, chunk_type)
    ON CONFLICT (occurrence_id, segment_order) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;

    IF v_inserted <> v_count AND EXISTS (
        SELECT 1
          FROM unnest(p_segment_orders, p_texts, p_context_prefixes, p_chunk_types)
               AS input(segment_order, segment_text, context_prefix, chunk_type)
          LEFT JOIN storage_v2_lexical_segment stored
            ON stored.occurrence_id = p_occurrence_id
           AND stored.segment_order = input.segment_order
         WHERE (stored.source_id, stored.artifact_version_id,
                stored.text_start, stored.text_length, stored.text_sha256,
                stored.context_prefix, stored.chunk_type, stored.fts_vector)
               IS DISTINCT FROM
               (v_source_id, p_artifact_version_id,
                strpos(v_search_text, input.segment_text),
                char_length(input.segment_text),
                sha256(convert_to(input.segment_text, 'UTF8')),
                input.context_prefix, input.chunk_type,
                setweight(to_tsvector('simple', input.segment_text), 'A')
                || setweight(to_tsvector('simple', input.context_prefix), 'B')
                || setweight(to_tsvector('simple', input.chunk_type), 'C'))
    ) THEN
        RAISE EXCEPTION 'lexical segment identity collision';
    END IF;
    RETURN v_count;
END
$$;

ALTER FUNCTION storage_v2_put_lexical_segments(
    BIGINT, BIGINT, BIGINT[], TEXT[], TEXT[], TEXT[]
) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_put_lexical_segments(
    BIGINT, BIGINT, BIGINT[], TEXT[], TEXT[], TEXT[]
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_put_lexical_segments(
    BIGINT, BIGINT, BIGINT[], TEXT[], TEXT[], TEXT[]
) TO mainrag;

CREATE OR REPLACE FUNCTION storage_v2_copy_legacy_lexical_segments(
    p_occurrence_id BIGINT, p_artifact_version_id BIGINT
) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
DECLARE
    v_source_id BIGINT;
    v_path TEXT;
    v_body TEXT;
    v_hash TEXT;
    v_file_id BIGINT;
    v_count BIGINT;
    v_staged BIGINT := 0;
    v_orders BIGINT[] := ARRAY[]::BIGINT[];
    v_texts TEXT[] := ARRAY[]::TEXT[];
    v_prefixes TEXT[] := ARRAY[]::TEXT[];
    v_types TEXT[] := ARRAY[]::TEXT[];
    v_chunk RECORD;
BEGIN
    SELECT occurrence_row.source_id, occurrence_row.source_path,
           document.search_text, artifact.expected_content_hash
      INTO v_source_id, v_path, v_body, v_hash
      FROM occurrence occurrence_row
      JOIN artifact_version artifact
        ON artifact.id = occurrence_row.artifact_version_id
      JOIN storage_v2_search_view_document binding
        ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
      JOIN storage_v2_search_document document
        ON document.id = binding.document_id
       AND document.component_kind = 'node'
       AND document.node_id = artifact.content_root_node_id
     WHERE occurrence_row.id = p_occurrence_id
       AND occurrence_row.artifact_version_id = p_artifact_version_id;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'write') THEN
        RAISE EXCEPTION 'authorized source-backed lexical copy required'
            USING ERRCODE = '42501';
    END IF;
    SELECT file.id INTO v_file_id FROM files file
     WHERE file.source_id = v_source_id AND file.path = v_path
       AND file.hash = decode(v_hash, 'hex')
       AND file.content_text = v_body;
    IF NOT FOUND THEN
        RETURN 0;
    END IF;
    SELECT count(*) INTO v_count FROM chunks WHERE file_id = v_file_id;
    IF v_count = 0 OR EXISTS (
        SELECT 1 FROM chunks chunk
         WHERE chunk.file_id = v_file_id
           AND (chunk.content_text IS NULL OR chunk.content_text = ''
                OR strpos(v_body, chunk.content_text) = 0)
    ) THEN
        RETURN 0;
    END IF;
    FOR v_chunk IN
        SELECT id, content_text, COALESCE(context_prefix, '') AS context_prefix,
               chunk_type
          FROM chunks WHERE file_id = v_file_id ORDER BY id
    LOOP
        v_orders := array_append(v_orders, v_chunk.id);
        v_texts := array_append(v_texts, v_chunk.content_text);
        v_prefixes := array_append(v_prefixes, v_chunk.context_prefix);
        v_types := array_append(v_types, v_chunk.chunk_type);
        IF cardinality(v_orders) = 256 THEN
            v_staged := v_staged + storage_v2_put_lexical_segments(
                p_occurrence_id, p_artifact_version_id,
                v_orders, v_texts, v_prefixes, v_types);
            v_orders := ARRAY[]::BIGINT[];
            v_texts := ARRAY[]::TEXT[];
            v_prefixes := ARRAY[]::TEXT[];
            v_types := ARRAY[]::TEXT[];
        END IF;
    END LOOP;
    IF cardinality(v_orders) > 0 THEN
        v_staged := v_staged + storage_v2_put_lexical_segments(
            p_occurrence_id, p_artifact_version_id,
            v_orders, v_texts, v_prefixes, v_types);
    END IF;
    IF v_staged <> v_count THEN
        RAISE EXCEPTION 'legacy lexical segment count changed during copy';
    END IF;
    RETURN v_staged;
END
$$;

ALTER FUNCTION storage_v2_copy_legacy_lexical_segments(BIGINT, BIGINT)
    OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_copy_legacy_lexical_segments(BIGINT, BIGINT)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_copy_legacy_lexical_segments(BIGINT, BIGINT)
    TO mainrag;
