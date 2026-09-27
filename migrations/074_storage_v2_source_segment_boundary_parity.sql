-- A chunk can have a lexeme at its boundary that the whole-document vector
-- cannot have. Rank and prove the sealed source slice, not the document vector.
-- Reconstruct the current chunk weighting without a runtime dependency on
-- legacy chunks, including after their eventual removal.
CREATE FUNCTION storage_v2_segment_rank_vector(
    p_search_text TEXT, p_text_start BIGINT, p_text_length BIGINT,
    p_chunk_type TEXT
) RETURNS TSVECTOR
LANGUAGE sql IMMUTABLE STRICT
SET search_path = pg_catalog, public
AS $$
    SELECT setweight(to_tsvector('simple',
               substring(p_search_text FROM p_text_start::INTEGER
                                       FOR p_text_length::INTEGER)), 'A')
        || setweight(to_tsvector('simple', p_chunk_type), 'B')
$$;

ALTER FUNCTION storage_v2_segment_rank_vector(TEXT, BIGINT, BIGINT, TEXT)
    OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_segment_rank_vector(TEXT, BIGINT, BIGINT, TEXT)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_segment_rank_vector(TEXT, BIGINT, BIGINT, TEXT)
    TO mainrag;

-- Materialize the exact chunk-weight vector once. Rebuilding it for every
-- matching segment makes medium source queries exceed the latency envelope.
ALTER TABLE storage_v2_lexical_segment ADD COLUMN rank_vector TSVECTOR;

DO $$
DECLARE
    v_sql TEXT := pg_get_functiondef(
        'storage_v2_put_lexical_segment(bigint,bigint,bigint,text,text,text)'::REGPROCEDURE
    );
    v_columns_old TEXT := E'        fts_vector\n    ) VALUES (';
    v_columns_new TEXT := E'        fts_vector, rank_vector\n    ) VALUES (';
    v_values_old TEXT := E'        p_chunk_type, v_vector\n    ) ON CONFLICT';
    v_values_new TEXT := E'        p_chunk_type, v_vector,\n        storage_v2_segment_rank_vector(v_search_text, v_position, char_length(p_text), p_chunk_type)\n    ) ON CONFLICT';
    v_identity_old TEXT := 'v_existing.chunk_type, v_existing.fts_vector)';
    v_identity_new TEXT := 'v_existing.chunk_type, v_existing.fts_vector, v_existing.rank_vector)';
    v_expected_old TEXT := 'p_chunk_type, v_vector) THEN';
    v_expected_new TEXT := 'p_chunk_type, v_vector, storage_v2_segment_rank_vector(v_search_text, v_position, char_length(p_text), p_chunk_type)) THEN';
BEGIN
    IF strpos(v_sql, v_columns_old) = 0 OR strpos(v_sql, v_values_old) = 0
       OR strpos(v_sql, v_identity_old) = 0 OR strpos(v_sql, v_expected_old) = 0 THEN
        RAISE EXCEPTION 'lexical segment writer changed before rank projection';
    END IF;
    v_sql := replace(v_sql, v_columns_old, v_columns_new);
    v_sql := replace(v_sql, v_values_old, v_values_new);
    v_sql := replace(v_sql, v_identity_old, v_identity_new);
    EXECUTE replace(v_sql, v_expected_old, v_expected_new);
END
$$;

DO $$
BEGIN
    ALTER TABLE storage_v2_lexical_segment
        DISABLE TRIGGER storage_v2_lexical_segment_immutable;
    UPDATE storage_v2_lexical_segment segment
       SET rank_vector = storage_v2_segment_rank_vector(
           document.search_text, segment.text_start, segment.text_length,
           segment.chunk_type)
      FROM occurrence occurrence_row
      JOIN artifact_version artifact
        ON artifact.id = occurrence_row.artifact_version_id
      JOIN storage_v2_search_view_document binding
        ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
      JOIN storage_v2_search_document document
        ON document.id = binding.document_id
       AND document.component_kind = 'node'
       AND document.node_id = artifact.content_root_node_id
     WHERE segment.occurrence_id = occurrence_row.id
       AND segment.source_id = occurrence_row.source_id
       AND segment.artifact_version_id = occurrence_row.artifact_version_id;
    ALTER TABLE storage_v2_lexical_segment
        ENABLE TRIGGER storage_v2_lexical_segment_immutable;
EXCEPTION WHEN OTHERS THEN
    ALTER TABLE storage_v2_lexical_segment
        ENABLE TRIGGER storage_v2_lexical_segment_immutable;
    RAISE;
END
$$;

ALTER TABLE storage_v2_lexical_segment
    ALTER COLUMN rank_vector SET NOT NULL;
CREATE INDEX idx_storage_v2_lexical_segment_rank_fts
    ON storage_v2_lexical_segment USING GIN (rank_vector);

CREATE OR REPLACE FUNCTION storage_v2_source_segment_ranks(
    p_occurrence_ids BIGINT[], p_query TEXT
) RETURNS TABLE(occurrence_id BIGINT, score REAL, segment_order BIGINT)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    ), query AS (
        SELECT websearch_to_tsquery('simple', p_query) AS value
    )
    SELECT DISTINCT ON (segment.occurrence_id)
           segment.occurrence_id,
           ts_rank_cd(segment.rank_vector, query.value)::REAL AS score,
           segment.segment_order
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN authorized_source ON authorized_source.id = occurrence_row.source_id
      JOIN artifact_version artifact
        ON artifact.id = occurrence_row.artifact_version_id
      JOIN storage_v2_search_view_document binding
        ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
      JOIN storage_v2_search_document document
        ON document.id = binding.document_id
       AND document.component_kind = 'node'
       AND document.node_id = artifact.content_root_node_id
      JOIN storage_v2_lexical_segment segment
        ON segment.occurrence_id = occurrence_row.id
       AND segment.source_id = occurrence_row.source_id
       AND segment.artifact_version_id = occurrence_row.artifact_version_id
      CROSS JOIN query
     WHERE segment.rank_vector @@ query.value
     ORDER BY segment.occurrence_id, score DESC, segment.segment_order
$$;

CREATE OR REPLACE FUNCTION storage_v2_source_segment_rank(
    p_occurrence_id BIGINT, p_term TEXT
) RETURNS TABLE(score REAL, segment_order BIGINT)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
    SELECT ranked.score, ranked.segment_order
      FROM storage_v2_source_segment_ranks(ARRAY[p_occurrence_id], p_term) ranked
     WHERE ranked.occurrence_id = p_occurrence_id
$$;

DO $$
DECLARE
    v_sql TEXT := pg_get_functiondef(
        'storage_v2_verify_lexical_segments(bigint)'::REGPROCEDURE
    );
    v_old TEXT := 'OR lexical.fts_vector IS DISTINCT FROM';
    v_new TEXT := E'OR lexical.rank_vector IS DISTINCT FROM\n                            storage_v2_segment_rank_vector(visible.search_text, lexical.text_start, lexical.text_length, lexical.chunk_type)\n                         OR lexical.fts_vector IS DISTINCT FROM';
BEGIN
    IF strpos(v_sql, v_old) = 0 THEN
        RAISE EXCEPTION 'lexical segment verification changed before rank projection';
    END IF;
    EXECUTE replace(v_sql, v_old, v_new);
END
$$;

-- The evidence fallback must independently observe the exact source slice.
-- A whole-document FTS miss cannot disprove a matching sealed segment.
CREATE FUNCTION storage_v2_source_segment_body_matches(
    p_occurrence_id BIGINT, p_query TEXT
) RETURNS BOOLEAN
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
    SELECT EXISTS (
        SELECT 1
          FROM occurrence occurrence_row
          JOIN artifact_version artifact
            ON artifact.id = occurrence_row.artifact_version_id
          JOIN storage_v2_search_view_document binding
            ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
          JOIN storage_v2_search_document document
            ON document.id = binding.document_id
           AND document.component_kind = 'node'
           AND document.node_id = artifact.content_root_node_id
          JOIN storage_v2_lexical_segment segment
            ON segment.occurrence_id = occurrence_row.id
           AND segment.source_id = occurrence_row.source_id
           AND segment.artifact_version_id = occurrence_row.artifact_version_id
         WHERE occurrence_row.id = p_occurrence_id
           AND storage_v2_can_access_source(occurrence_row.source_id, 'read')
           AND segment.fts_vector @@ websearch_to_tsquery('simple', p_query)
           AND segment.text_sha256 = sha256(convert_to(substring(
               document.search_text FROM segment.text_start::INTEGER
                                    FOR segment.text_length::INTEGER), 'UTF8'))
           AND to_tsvector('simple', substring(
               document.search_text FROM segment.text_start::INTEGER
                                    FOR segment.text_length::INTEGER))
               @@ websearch_to_tsquery('simple', p_query)
    )
$$;

ALTER FUNCTION storage_v2_source_segment_body_matches(BIGINT, TEXT)
    OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_source_segment_body_matches(BIGINT, TEXT)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_body_matches(BIGINT, TEXT)
    TO mainrag;

DO $$
DECLARE
    v_sql TEXT;
    v_old TEXT := 'to_tsvector(''simple'', search_text) @@ websearch_to_tsquery(''simple'', p_query) AS fts_body_matches';
    v_new TEXT := 'storage_v2_source_segment_body_matches(id, p_query) AS fts_body_matches';
BEGIN
    v_sql := pg_get_functiondef(
        'storage_v2_candidate_query_evidence(bigint,bigint,text,text,bigint[],bigint[])'::REGPROCEDURE
    );
    IF strpos(v_sql, v_old) = 0
       OR strpos(v_sql, '''mainrag.storage-v2.query-coverage.v3''') = 0 THEN
        RAISE EXCEPTION 'storage-v2 query evidence definition changed before source slice proof';
    END IF;
    EXECUTE replace(v_sql, v_old, v_new);
END
$$;
