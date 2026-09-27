-- Preserve source-backed lexical chunk boundaries independently of legacy
-- chunks. A segment is attached to one immutable artifact and can be scored
-- after the legacy tables have been retired. The source text witness is an
-- exact slice of the verified search document, never an unchecked index hit.
CREATE TABLE storage_v2_lexical_segment (
    occurrence_id BIGINT NOT NULL REFERENCES occurrence(id) ON DELETE RESTRICT,
    source_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    segment_order BIGINT NOT NULL CHECK (segment_order >= 0),
    text_start BIGINT NOT NULL CHECK (text_start > 0),
    text_length BIGINT NOT NULL CHECK (text_length > 0),
    text_sha256 BYTEA NOT NULL CHECK (octet_length(text_sha256) = 32),
    context_prefix TEXT NOT NULL,
    chunk_type TEXT NOT NULL CHECK (chunk_type <> ''),
    fts_vector TSVECTOR NOT NULL,
    PRIMARY KEY (occurrence_id, segment_order),
    FOREIGN KEY (occurrence_id, source_id, artifact_version_id)
        REFERENCES occurrence(id, source_id, artifact_version_id) ON DELETE RESTRICT
);

CREATE INDEX idx_storage_v2_lexical_segment_fts
    ON storage_v2_lexical_segment USING GIN (fts_vector);
CREATE INDEX idx_storage_v2_lexical_segment_artifact
    ON storage_v2_lexical_segment (artifact_version_id, occurrence_id);

ALTER TABLE storage_v2_lexical_segment OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE storage_v2_lexical_segment ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_lexical_segment FORCE ROW LEVEL SECURITY;
CREATE POLICY storage_v2_lexical_segment_source ON storage_v2_lexical_segment
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));

CREATE TRIGGER storage_v2_lexical_segment_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_lexical_segment
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();

-- Called while staging the exact file version. The segment text must be
-- present in the immutable body-backed document bound to the occurrence.
CREATE FUNCTION storage_v2_put_lexical_segment(
    p_occurrence_id BIGINT,
    p_artifact_version_id BIGINT,
    p_segment_order BIGINT,
    p_text TEXT,
    p_context_prefix TEXT,
    p_chunk_type TEXT
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
DECLARE
    v_source_id BIGINT;
    v_search_text TEXT;
    v_position BIGINT;
    v_vector TSVECTOR;
    v_digest BYTEA;
    v_existing storage_v2_lexical_segment;
BEGIN
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
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'write')
       OR p_segment_order IS NULL OR p_segment_order < 0
       OR p_text IS NULL OR p_text = ''
       OR p_context_prefix IS NULL
       OR p_chunk_type IS NULL OR p_chunk_type = '' THEN
        RAISE EXCEPTION 'authorized source-backed lexical segment required'
            USING ERRCODE = '42501';
    END IF;
    v_position := strpos(v_search_text, p_text);
    IF v_position = 0 THEN
        RAISE EXCEPTION 'lexical segment is absent from immutable source text';
    END IF;
    v_digest := sha256(convert_to(p_text, 'UTF8'));
    v_vector := setweight(to_tsvector('simple', p_text), 'A')
        || setweight(to_tsvector('simple', p_context_prefix), 'B')
        || setweight(to_tsvector('simple', p_chunk_type), 'C');
    INSERT INTO storage_v2_lexical_segment (
        occurrence_id, source_id, artifact_version_id, segment_order,
        text_start, text_length, text_sha256, context_prefix, chunk_type,
        fts_vector
    ) VALUES (
        p_occurrence_id, v_source_id, p_artifact_version_id, p_segment_order,
        v_position, char_length(p_text), v_digest, p_context_prefix,
        p_chunk_type, v_vector
    ) ON CONFLICT (occurrence_id, segment_order) DO NOTHING;
    IF NOT FOUND THEN
        SELECT * INTO STRICT v_existing FROM storage_v2_lexical_segment
         WHERE occurrence_id = p_occurrence_id
           AND segment_order = p_segment_order;
        IF (v_existing.source_id, v_existing.artifact_version_id,
            v_existing.text_start, v_existing.text_length,
            v_existing.text_sha256, v_existing.context_prefix,
            v_existing.chunk_type, v_existing.fts_vector)
           IS DISTINCT FROM
           (v_source_id, p_artifact_version_id, v_position,
            char_length(p_text), v_digest, p_context_prefix,
            p_chunk_type, v_vector) THEN
            RAISE EXCEPTION 'lexical segment identity collision';
        END IF;
    END IF;
END
$$;

ALTER FUNCTION storage_v2_put_lexical_segment(
    BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT
) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON storage_v2_lexical_segment FROM PUBLIC, mainrag;
GRANT SELECT ON storage_v2_lexical_segment TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_put_lexical_segment(
    BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_put_lexical_segment(
    BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT
) TO mainrag;

-- During backfill the current chunk projection is reusable only if it belongs
-- to the same file bytes and every chunk is an exact substring of the sealed
-- document. The copied rank key is a value, not a dependency on the legacy
-- chunk row: no foreign key or live read of chunks remains after this call.
CREATE FUNCTION storage_v2_copy_legacy_lexical_segments(
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
        PERFORM storage_v2_put_lexical_segment(
            p_occurrence_id, p_artifact_version_id, v_chunk.id,
            v_chunk.content_text, v_chunk.context_prefix, v_chunk.chunk_type
        );
    END LOOP;
    RETURN v_count;
END
$$;

ALTER FUNCTION storage_v2_copy_legacy_lexical_segments(BIGINT, BIGINT)
    OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_copy_legacy_lexical_segments(BIGINT, BIGINT)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_copy_legacy_lexical_segments(BIGINT, BIGINT)
    TO mainrag;

-- A lexical segment is eligible only when the bound immutable body also
-- matches PostgreSQL's own simple lexer. Context-only legacy hits cannot be
-- admitted as body-backed candidate hits.
CREATE FUNCTION storage_v2_source_segment_rank(
    p_occurrence_id BIGINT, p_term TEXT
) RETURNS TABLE(score REAL, segment_order BIGINT)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
    SELECT ts_rank_cd(segment.fts_vector,
               websearch_to_tsquery('simple', p_term))::REAL AS score,
           segment.segment_order
      FROM storage_v2_lexical_segment segment
      JOIN occurrence occurrence_row ON occurrence_row.id = segment.occurrence_id
      JOIN artifact_version artifact ON artifact.id = occurrence_row.artifact_version_id
      JOIN storage_v2_search_view_document binding
        ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
      JOIN storage_v2_search_document document
        ON document.id = binding.document_id
       AND document.component_kind = 'node'
       AND document.node_id = artifact.content_root_node_id
     WHERE segment.occurrence_id = p_occurrence_id
       AND segment.source_id = occurrence_row.source_id
       AND segment.artifact_version_id = occurrence_row.artifact_version_id
       AND storage_v2_can_access_source(occurrence_row.source_id, 'read')
       AND document.fts_simple @@ websearch_to_tsquery('simple', p_term)
       AND segment.fts_vector @@ websearch_to_tsquery('simple', p_term)
     ORDER BY score DESC, segment.segment_order
     LIMIT 1
$$;

ALTER FUNCTION storage_v2_source_segment_rank(BIGINT, TEXT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_source_segment_rank(BIGINT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_rank(BIGINT, TEXT) TO mainrag;

CREATE FUNCTION storage_v2_verify_lexical_segments(p_generation_id BIGINT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
DECLARE
    v_source_id BIGINT;
    v_result JSONB;
BEGIN
    SELECT source_id INTO v_source_id FROM source_generation
     WHERE id = p_generation_id AND status IN ('verified', 'release_candidate');
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'read') THEN
        RAISE EXCEPTION 'verified authorized generation required for lexical verification'
            USING ERRCODE = '42501';
    END IF;
    WITH visible AS (
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
    ), checked AS (
        SELECT visible.document_id, visible.search_text,
               segment.segment_count, segment.invalid_count
          FROM visible
          LEFT JOIN LATERAL (
              SELECT count(*) AS segment_count,
                     count(*) FILTER (WHERE
                         sha256(convert_to(substring(visible.search_text
                             FROM lexical.text_start::INTEGER
                             FOR lexical.text_length::INTEGER), 'UTF8'))
                             <> lexical.text_sha256
                         OR lexical.fts_vector IS DISTINCT FROM
                            (setweight(to_tsvector('simple', substring(
                                visible.search_text FROM lexical.text_start::INTEGER
                                FOR lexical.text_length::INTEGER)), 'A')
                             || setweight(to_tsvector('simple', lexical.context_prefix), 'B')
                             || setweight(to_tsvector('simple', lexical.chunk_type), 'C'))
                     ) AS invalid_count
                FROM storage_v2_lexical_segment lexical
               WHERE lexical.occurrence_id = visible.id
          ) segment ON TRUE
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

ALTER FUNCTION storage_v2_verify_lexical_segments(BIGINT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_verify_lexical_segments(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_verify_lexical_segments(BIGINT) TO mainrag;

DO $$
DECLARE
    v_sql TEXT := pg_get_functiondef(
        'storage_v2_qualify_release_candidate(uuid,bigint,bigint,text,text,text,text,text,jsonb)'::REGPROCEDURE
    );
    v_old TEXT := '''restart_resume'', ''search_quality''';
BEGIN
    IF strpos(v_sql, v_old) = 0 THEN
        RAISE EXCEPTION 'release-candidate qualification definition changed before lexical gate installation';
    END IF;
    v_sql := replace(v_sql, v_old,
        '''restart_resume'', ''search_quality'', ''lexical_segment_integrity''');
    EXECUTE v_sql;
END
$$;

-- Keep the established AST, tenant, generation, score-component and limit
-- contracts. For one positive term, use the verified body-backed chunk score
-- and original chunk tie key when the segment projection is present. Older
-- generations without that projection retain their previous retrieval path.
DO $$
DECLARE
    v_function REGPROCEDURE;
    v_sql TEXT;
    v_ast_old TEXT := E'WHERE storage_v2_search_ast_matches(\n             p_ast, matched_terms, matched_phrases, matched_exact\n         )';
    v_ast_new TEXT := E'WHERE storage_v2_search_ast_matches(\n             p_ast, matched_terms, matched_phrases, matched_exact\n         ) OR (p_ast ->> ''type'' = ''term'' AND EXISTS (\n             SELECT 1 FROM storage_v2_source_segment_rank(matched.id, p_ast ->> ''value'')\n         ))';
    v_score_old TEXT := 'lexical_score + graph_score + semantic_score + rerank_score AS final_score';
    v_score_new TEXT := E'COALESCE(1000000.0 + lexical_rank.score, lexical_score)\n                 + graph_score + semantic_score + rerank_score AS final_score,\n               lexical_rank.segment_order AS lexical_sort_key';
    v_from_old TEXT := E'FROM staged\n    ),\n    ordered AS (';
    v_from_new TEXT := E'FROM staged\n          LEFT JOIN LATERAL storage_v2_source_segment_rank(\n              staged.id, p_ast ->> ''value''\n          ) lexical_rank ON p_ast ->> ''type'' = ''term''\n    ),\n    ordered AS (';
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE,
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::REGPROCEDURE
    ] LOOP
        v_sql := pg_get_functiondef(v_function);
        IF strpos(v_sql, v_ast_old) = 0
           OR strpos(v_sql, v_score_old) = 0
           OR strpos(v_sql, v_from_old) = 0
           OR strpos(v_sql, 'ORDER BY final_score DESC, external_hit_id, id') = 0 THEN
            RAISE EXCEPTION 'storage-v2 retrieval definition changed before lexical segment installation';
        END IF;
        v_sql := replace(v_sql, v_ast_old, v_ast_new);
        v_sql := replace(v_sql, v_score_old, v_score_new);
        v_sql := replace(v_sql, v_from_old, v_from_new);
        v_sql := replace(v_sql,
            'ORDER BY final_score DESC, external_hit_id, id',
            'ORDER BY final_score DESC, lexical_sort_key NULLS LAST, external_hit_id, id');
        EXECUTE v_sql;
    END LOOP;
END
$$;

-- Extend the independent proof for PostgreSQL lexemes such as punctuation-
-- joined numbers. The earlier regular-expression path remains required when
-- it applies. The fallback requires both the complete immutable body FTS and
-- an independently materialized, source-backed lexical segment.
DO $$
DECLARE
    v_sql TEXT;
    v_frequency_old TEXT := 'storage_v2_literal_term_count(search_text, p_query) AS reference_frequency,';
    v_frequency_new TEXT := E'storage_v2_literal_term_count(search_text, p_query) AS reference_frequency,\n               to_tsvector(''simple'', search_text) @@ websearch_to_tsquery(''simple'', p_query) AS fts_body_matches,\n               EXISTS (SELECT 1 FROM storage_v2_source_segment_rank(id, p_query)) AS segment_matches,';
    v_check_old TEXT := E'OR (hit ->> ''reference_frequency'')::BIGINT <= 0\n                   OR hit -> ''reference_frequency'' <> hit -> ''posting_frequency'')';
    v_check_new TEXT := E'OR NOT ((\n                       (hit ->> ''reference_frequency'')::BIGINT > 0\n                       AND hit -> ''reference_frequency'' = hit -> ''posting_frequency''\n                   ) OR (\n                       hit -> ''fts_body_matches'' = ''true''::JSONB\n                       AND hit -> ''segment_matches'' = ''true''::JSONB\n                   )))';
BEGIN
    v_sql := pg_get_functiondef(
        'storage_v2_candidate_query_evidence(bigint,bigint,text,text,bigint[],bigint[])'::REGPROCEDURE
    );
    IF strpos(v_sql, v_frequency_old) = 0
       OR strpos(v_sql, v_check_old) = 0
       OR strpos(v_sql, '''mainrag.storage-v2.query-coverage.v1''') = 0
       OR strpos(v_sql, '''reference_frequency'', reference_frequency, ''posting_frequency'', posting_frequency') = 0 THEN
        RAISE EXCEPTION 'storage-v2 query evidence definition changed before lexical proof installation';
    END IF;
    v_sql := replace(v_sql, '''mainrag.storage-v2.query-coverage.v1''',
        '''mainrag.storage-v2.query-coverage.v2''');
    v_sql := replace(v_sql, v_frequency_old, v_frequency_new);
    v_sql := replace(v_sql,
        '''reference_frequency'', reference_frequency, ''posting_frequency'', posting_frequency',
        '''reference_frequency'', reference_frequency, ''posting_frequency'', posting_frequency, ''fts_body_matches'', fts_body_matches, ''segment_matches'', segment_matches');
    v_sql := replace(v_sql, v_check_old, v_check_new);
    EXECUTE v_sql;
END
$$;
