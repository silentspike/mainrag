-- The installed chunk vector includes body (A), context prefix (B), and type
-- (C). Migration 074 used the historical schema.sql definition for its extra
-- rank vector and therefore omitted legitimate context matches. The sealed
-- lexical segment already stores the exact installed chunk projection.
DO $$
DECLARE
    v_sql TEXT := pg_get_functiondef(
        'storage_v2_put_lexical_segment(bigint,bigint,bigint,text,text,text)'::REGPROCEDURE
    );
    v_columns_old TEXT := E'        fts_vector, rank_vector\n    ) VALUES (';
    v_columns_new TEXT := E'        fts_vector\n    ) VALUES (';
    v_values_old TEXT := E'        p_chunk_type, v_vector,\n        storage_v2_segment_rank_vector(v_search_text, v_position, char_length(p_text), p_chunk_type)\n    ) ON CONFLICT';
    v_values_new TEXT := E'        p_chunk_type, v_vector\n    ) ON CONFLICT';
    v_identity_old TEXT := 'v_existing.chunk_type, v_existing.fts_vector, v_existing.rank_vector)';
    v_identity_new TEXT := 'v_existing.chunk_type, v_existing.fts_vector)';
    v_expected_old TEXT := 'p_chunk_type, v_vector, storage_v2_segment_rank_vector(v_search_text, v_position, char_length(p_text), p_chunk_type)) THEN';
    v_expected_new TEXT := 'p_chunk_type, v_vector) THEN';
BEGIN
    IF strpos(v_sql, v_columns_old) = 0 OR strpos(v_sql, v_values_old) = 0
       OR strpos(v_sql, v_identity_old) = 0 OR strpos(v_sql, v_expected_old) = 0 THEN
        RAISE EXCEPTION 'rank projection writer differs before installed parity repair';
    END IF;
    v_sql := replace(v_sql, v_columns_old, v_columns_new);
    v_sql := replace(v_sql, v_values_old, v_values_new);
    v_sql := replace(v_sql, v_identity_old, v_identity_new);
    EXECUTE replace(v_sql, v_expected_old, v_expected_new);
END
$$;

DO $$
DECLARE
    v_sql TEXT := pg_get_functiondef(
        'storage_v2_verify_lexical_segments(bigint)'::REGPROCEDURE
    );
    v_old TEXT := E'                         OR lexical.rank_vector IS DISTINCT FROM\n                            storage_v2_segment_rank_vector(visible.search_text, lexical.text_start, lexical.text_length, lexical.chunk_type)\n';
BEGIN
    IF strpos(v_sql, v_old) = 0 THEN
        RAISE EXCEPTION 'rank projection verifier differs before installed parity repair';
    END IF;
    EXECUTE replace(v_sql, v_old, '');
END
$$;

DO $$
DECLARE
    v_sql TEXT := pg_get_functiondef(
        'storage_v2_source_segment_ranks(bigint[],text)'::REGPROCEDURE
    );
BEGIN
    IF strpos(v_sql, 'ts_rank_cd(segment.rank_vector, query.value)') = 0
       OR strpos(v_sql, 'WHERE segment.rank_vector @@ query.value') = 0 THEN
        RAISE EXCEPTION 'set ranking differs before installed parity repair';
    END IF;
    EXECUTE replace(v_sql, 'segment.rank_vector', 'segment.fts_vector');
END
$$;

-- Existing source-backed body proof remains valid for chunk-boundary terms.
-- A context-only legacy hit needs a separate, explicit copy-provenance proof;
-- never relabel that metadata hit as a body-text match.
CREATE FUNCTION storage_v2_source_legacy_segment_matches(
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
          JOIN files file
            ON file.source_id = occurrence_row.source_id
           AND file.path = occurrence_row.source_path
           AND file.hash = decode(artifact.expected_content_hash, 'hex')
           AND file.content_text = document.search_text
          JOIN chunks chunk ON chunk.file_id = file.id
          JOIN storage_v2_lexical_segment segment
            ON segment.occurrence_id = occurrence_row.id
           AND segment.source_id = occurrence_row.source_id
           AND segment.artifact_version_id = occurrence_row.artifact_version_id
           AND segment.segment_order = chunk.id
         WHERE occurrence_row.id = p_occurrence_id
           AND storage_v2_can_access_source(occurrence_row.source_id, 'read')
           AND chunk.content_text IS NOT NULL
           AND segment.text_sha256 = sha256(convert_to(chunk.content_text, 'UTF8'))
           AND segment.text_sha256 = sha256(convert_to(substring(
               document.search_text FROM segment.text_start::INTEGER
                                    FOR segment.text_length::INTEGER), 'UTF8'))
           AND segment.context_prefix = COALESCE(chunk.context_prefix, '')
           AND segment.chunk_type = chunk.chunk_type
           AND segment.fts_vector = chunk.fts_vector
           AND segment.fts_vector @@ websearch_to_tsquery('simple', p_query)
    )
$$;

ALTER FUNCTION storage_v2_source_legacy_segment_matches(BIGINT, TEXT)
    OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_source_legacy_segment_matches(BIGINT, TEXT)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_source_legacy_segment_matches(BIGINT, TEXT)
    TO mainrag;

DO $$
DECLARE
    v_sql TEXT := pg_get_functiondef(
        'storage_v2_candidate_query_evidence(bigint,bigint,text,text,bigint[],bigint[])'::REGPROCEDURE
    );
    v_match_old TEXT := 'EXISTS (SELECT 1 FROM storage_v2_source_segment_rank(id, p_query)) AS segment_matches,';
    v_match_new TEXT := E'EXISTS (SELECT 1 FROM storage_v2_source_segment_rank(id, p_query)) AS segment_matches,\n               storage_v2_source_legacy_segment_matches(id, p_query) AS legacy_segment_matches,';
    v_json_old TEXT := '''segment_matches'', segment_matches';
    v_json_new TEXT := '''segment_matches'', segment_matches, ''legacy_segment_matches'', legacy_segment_matches';
    v_check_old TEXT := E'                       AND hit -> ''segment_matches'' = ''true''::JSONB\n                   )))';
    v_check_new TEXT := E'                       AND hit -> ''segment_matches'' = ''true''::JSONB\n                   ) OR (\n                       hit -> ''legacy_segment_matches'' = ''true''::JSONB\n                       AND hit -> ''segment_matches'' = ''true''::JSONB\n                   )))';
BEGIN
    IF strpos(v_sql, v_match_old) = 0 OR strpos(v_sql, v_json_old) = 0
       OR strpos(v_sql, v_check_old) = 0
       OR strpos(v_sql, '''mainrag.storage-v2.query-coverage.v3''') = 0 THEN
        RAISE EXCEPTION 'query proof differs before installed parity repair';
    END IF;
    v_sql := replace(v_sql, v_match_old, v_match_new);
    v_sql := replace(v_sql, v_json_old, v_json_new);
    v_sql := replace(v_sql, v_check_old, v_check_new);
    EXECUTE replace(v_sql, '''mainrag.storage-v2.query-coverage.v3''',
                    '''mainrag.storage-v2.query-coverage.v4''');
END
$$;

-- The extra column and index from 074 are not search or proof inputs after
-- the repair. Dropping the column removes the duplicate logical projection;
-- physical table-space reclamation is a separate maintenance concern.
ALTER TABLE storage_v2_lexical_segment DROP COLUMN rank_vector;
DROP FUNCTION storage_v2_segment_rank_vector(TEXT, BIGINT, BIGINT, TEXT);
