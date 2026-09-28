-- Migration 085: validate supplied character locators in one bounded source
-- window. The producer retains canonical first-match positions; the database
-- independently validates the exact text at every immutable position, digest,
-- weighted vector, authorization and collision identity. Old callers keep 083.
CREATE OR REPLACE FUNCTION storage_v2_put_lexical_segments_at(
    p_occurrence_id BIGINT, p_artifact_version_id BIGINT,
    p_segment_orders BIGINT[], p_texts TEXT[],
    p_context_prefixes TEXT[], p_chunk_types TEXT[], p_character_starts BIGINT[]
) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
DECLARE
    v_source_id BIGINT;
    v_search_text TEXT;
    v_window TEXT;
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
BEGIN
    v_count := cardinality(p_segment_orders);
    IF v_count IS NULL OR v_count < 1 OR v_count > 256
       OR cardinality(p_texts) IS DISTINCT FROM v_count
       OR cardinality(p_context_prefixes) IS DISTINCT FROM v_count
       OR cardinality(p_chunk_types) IS DISTINCT FROM v_count
       OR cardinality(p_character_starts) IS DISTINCT FROM v_count THEN
        RAISE EXCEPTION 'bounded lexical segment group required';
    END IF;
    SELECT occurrence_row.source_id, document.search_text
      INTO v_source_id, v_search_text
      FROM occurrence occurrence_row
      JOIN artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id
      JOIN storage_v2_search_view_document binding
        ON binding.view_id=occurrence_row.view_id AND binding.ordinal=0
      JOIN storage_v2_search_document document ON document.id=binding.document_id
       AND document.component_kind='node' AND document.node_id=artifact.content_root_node_id
     WHERE occurrence_row.id=p_occurrence_id
       AND occurrence_row.artifact_version_id=p_artifact_version_id;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'write') THEN
        RAISE EXCEPTION 'authorized source-backed lexical segment required'
            USING ERRCODE='42501';
    END IF;
    SELECT min(character_start),max(character_start+char_length(segment_text)-1)
      INTO v_window_start,v_window_end
      FROM unnest(p_character_starts,p_texts) AS bounds(character_start,segment_text);
    IF v_window_start IS NULL OR v_window_start<1 OR v_window_end>char_length(v_search_text)
       OR v_window_end-v_window_start+1>8388608 THEN
        RAISE EXCEPTION 'bounded source character window required';
    END IF;
    v_window := substring(v_search_text FROM v_window_start::INTEGER
                          FOR (v_window_end-v_window_start+1)::INTEGER);
    WITH prepared AS MATERIALIZED (
        SELECT input.*, character_start::INTEGER AS text_start,
               char_length(segment_text) AS text_length,
               sha256(convert_to(segment_text, 'UTF8')) AS text_sha256,
               setweight(to_tsvector('simple', segment_text), 'A')
               || setweight(to_tsvector('simple', context_prefix), 'B')
               || setweight(to_tsvector('simple', chunk_type), 'C') AS fts_vector
          FROM unnest(p_segment_orders, p_texts, p_context_prefixes, p_chunk_types, p_character_starts)
               WITH ORDINALITY AS input(segment_order, segment_text, context_prefix, chunk_type, character_start, ordinal)
    )
    SELECT count(DISTINCT segment_order),
           bool_and(COALESCE(segment_order >= 0 AND segment_text <> ''
                    AND context_prefix IS NOT NULL AND chunk_type IS NOT NULL
                    AND chunk_type <> '' AND text_start >= v_window_start
                    AND substring(v_window FROM (text_start-v_window_start+1)::INTEGER
                                  FOR text_length) = segment_text, FALSE)),
           array_agg(text_start ORDER BY ordinal), array_agg(text_length ORDER BY ordinal),
           array_agg(text_sha256 ORDER BY ordinal), array_agg(fts_vector ORDER BY ordinal)
      INTO v_distinct, v_valid, v_starts, v_lengths, v_hashes, v_vectors FROM prepared;
    IF v_distinct <> v_count OR v_valid IS NOT TRUE THEN
        RAISE EXCEPTION 'valid source-backed lexical segment group required';
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
          LEFT JOIN storage_v2_lexical_segment stored
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
$$;
ALTER FUNCTION storage_v2_put_lexical_segments_at(BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[],BIGINT[])
    OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_put_lexical_segments_at(BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[],BIGINT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_put_lexical_segments_at(BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[],BIGINT[]) TO mainrag;
