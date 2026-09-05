-- Migration 046: read-only, source-bound evidence for literal query coverage.
-- Recompute support from immutable UTF-8 body text, not from the posting index
-- that produced the hit. This does not qualify or activate a generation.

CREATE OR REPLACE FUNCTION storage_v2_literal_term_count(p_text TEXT, p_term TEXT)
RETURNS BIGINT LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog, public
AS $$
    SELECT count(*) FROM regexp_split_to_table(lower(p_text), '[^[:alnum:]_]+') token
     WHERE token = lower(p_term)
$$;

CREATE OR REPLACE FUNCTION storage_v2_candidate_query_evidence(
    p_source_id BIGINT, p_generation_id BIGINT, p_commit_sha TEXT,
    p_query TEXT, p_candidate_ids BIGINT[], p_current_ids BIGINT[]
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
    v_result JSONB;
BEGIN
    IF NOT storage_v2_is_admin() OR NOT storage_v2_can_access_source(p_source_id, 'read') THEN
        RAISE EXCEPTION 'candidate query evidence requires administrator source authority'
            USING ERRCODE = '42501';
    END IF;
    IF p_commit_sha IS NULL OR p_commit_sha !~ '^[0-9a-f]{40}$'
       OR p_query IS NULL OR octet_length(p_query) NOT BETWEEN 1 AND 128
       OR p_query !~ '^[[:alnum:]_]+$'
       OR p_candidate_ids IS NULL OR p_current_ids IS NULL
       OR cardinality(p_candidate_ids) > 10 OR cardinality(p_current_ids) > 10
       OR EXISTS (SELECT 1 FROM unnest(p_candidate_ids || p_current_ids) id WHERE id IS NULL OR id <= 0)
       OR cardinality(p_candidate_ids) <> (SELECT count(DISTINCT id) FROM unnest(p_candidate_ids) id)
       OR cardinality(p_current_ids) <> (SELECT count(DISTINCT id) FROM unnest(p_current_ids) id) THEN
        RAISE EXCEPTION 'bounded literal query and unique positive hit identities required';
    END IF;
    SELECT * INTO v_generation FROM source_generation
     WHERE id = p_generation_id AND source_id = p_source_id
       AND status IN ('verified', 'release_candidate')
       AND witness ->> 'commit_sha' = p_commit_sha;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM storage_v2_ingest_run run
        JOIN logical_source source ON source.id = run.source_id
         WHERE run.generation_id = p_generation_id AND run.source_id = p_source_id
           AND run.status = 'sealed'
           AND run.expected_active_generation_id IS NOT DISTINCT FROM source.active_generation_id
    ) THEN
        RAISE EXCEPTION 'verified candidate identity and unchanged pointer required';
    END IF;

    WITH candidate AS MATERIALIZED (
        SELECT occurrence_row.id, occurrence_row.source_path, document.id AS document_id,
               document.search_text, body.digest AS body_digest,
               'storage-v2:' || encode(storage_v2_hash_parts(
                    'mainrag.external-hit.v1', ARRAY[
                        int8send(occurrence_row.source_id), convert_to(item.item_key, 'UTF8'),
                        convert_to(artifact.expected_content_hash, 'UTF8'), view_row.view_digest,
                        convert_to(occurrence_row.role, 'UTF8'), int8send(occurrence_row.ordinal),
                        convert_to(occurrence_row.locator::TEXT, 'UTF8')
                    ]
               ), 'hex') AS external_hit_id
          FROM occurrence occurrence_row
          JOIN artifact_version artifact ON artifact.id = occurrence_row.artifact_version_id
          JOIN source_item item ON item.id = artifact.item_id
          JOIN generation_item_version membership
            ON membership.source_id = p_source_id AND membership.source_item_id = item.id
           AND membership.artifact_version_id = artifact.id
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > v_generation.generation_seq)
          JOIN retrieval_view view_row ON view_row.id = occurrence_row.view_id
          JOIN storage_v2_search_view_document binding ON binding.view_id = view_row.id AND binding.ordinal = 0
          JOIN storage_v2_search_document document ON document.id = binding.document_id
           AND document.component_kind = 'node' AND document.node_id = artifact.content_root_node_id
           AND document.profile_id = 'mainrag.lexical-simple.v1'
          JOIN content_node node ON node.id = document.node_id
          JOIN content_body body ON body.id = node.body_id AND body.digest_algorithm = 'sha256-v1'
         WHERE occurrence_row.source_id = p_source_id AND occurrence_row.id = ANY(p_candidate_ids)
           AND NOT EXISTS (SELECT 1 FROM storage_v2_search_view_document other
                            WHERE other.view_id = view_row.id AND other.ordinal <> 0)
    ),
    current_hits AS MATERIALIZED (
        SELECT chunk.id, file.path AS source_path,
               chunk.fts_vector @@ websearch_to_tsquery('simple', p_query) AS indexed_match
          FROM chunks chunk JOIN files file ON file.id = chunk.file_id
         WHERE file.source_id = p_source_id AND chunk.id = ANY(p_current_ids)
    ),
    paths AS (
        SELECT source_path FROM candidate UNION SELECT source_path FROM current_hits
    ),
    legacy_support AS MATERIALIZED (
        SELECT paths.source_path, count(chunk.id) AS chunk_count,
               count(chunk.id) FILTER (WHERE chunk.fts_vector @@ websearch_to_tsquery('simple', p_query)) AS indexed_matches,
               count(chunk.id) FILTER (WHERE storage_v2_literal_term_count(chunk.content_text, p_query) > 0) AS literal_matches
          FROM paths LEFT JOIN files file ON file.source_id = p_source_id AND file.path = paths.source_path
          LEFT JOIN chunks chunk ON chunk.file_id = file.id GROUP BY paths.source_path
    ),
    reference AS MATERIALIZED (
        SELECT candidate.*,
               sha256(convert_to(search_text, 'UTF8')) = body_digest AS body_text_matches,
               storage_v2_literal_term_count(search_text, p_query) AS reference_frequency,
               COALESCE((SELECT term_frequency FROM storage_v2_search_posting posting
                          WHERE posting.document_id = candidate.document_id
                            AND posting.term_sha256 = digest(lower(p_query), 'sha256')
                            AND posting.term = lower(p_query)), 0) AS posting_frequency
          FROM candidate
    )
    SELECT jsonb_build_object(
        'schema_version', 'mainrag.storage-v2.query-coverage.v1',
        'source_id', p_source_id, 'generation_id', p_generation_id,
        'generation_seq', v_generation.generation_seq, 'commit_sha', p_commit_sha,
        'query_sha256', encode(sha256(convert_to(p_query, 'UTF8')), 'hex'),
        'candidate', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'occurrence_id', id, 'external_hit_id', external_hit_id,
            'path_sha256', encode(sha256(convert_to(source_path, 'UTF8')), 'hex'),
            'body_sha256', encode(body_digest, 'hex'), 'body_text_matches', body_text_matches,
            'reference_frequency', reference_frequency, 'posting_frequency', posting_frequency
        ) ORDER BY id) FROM reference), '[]'::JSONB),
        'current', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'chunk_id', id, 'path_sha256', encode(sha256(convert_to(source_path, 'UTF8')), 'hex'),
            'indexed_match', indexed_match
        ) ORDER BY id) FROM current_hits), '[]'::JSONB),
        'legacy_paths', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'path_sha256', encode(sha256(convert_to(source_path, 'UTF8')), 'hex'),
            'chunk_count', chunk_count, 'indexed_matches', indexed_matches, 'literal_matches', literal_matches
        ) ORDER BY source_path) FROM legacy_support), '[]'::JSONB)
    ) INTO v_result;

    IF jsonb_array_length(v_result -> 'candidate') <> cardinality(p_candidate_ids)
       OR jsonb_array_length(v_result -> 'current') <> cardinality(p_current_ids) THEN
        RAISE EXCEPTION 'query hit identity is outside the supported named-generation source scope'
            USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_result -> 'candidate') hit
                WHERE hit -> 'body_text_matches' <> 'true'::JSONB
                   OR (hit ->> 'reference_frequency')::BIGINT <= 0
                   OR hit -> 'reference_frequency' <> hit -> 'posting_frequency')
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(v_result -> 'current') hit
                   WHERE hit -> 'indexed_match' IS DISTINCT FROM 'true'::JSONB) THEN
        RAISE EXCEPTION 'query hit lacks consistent immutable body or lexical support';
    END IF;
    RETURN v_result;
END
$$;

-- Use the existing retrieval owner. Deployments and disposable test schemas
-- may have different table owners; changing to an unrelated grantee breaks
-- row_security=off without granting any legitimate source authority.
DO $$
DECLARE
    v_owner NAME;
BEGIN
    SELECT pg_get_userbyid(proowner) INTO STRICT v_owner FROM pg_proc
     WHERE oid = 'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE;
    EXECUTE format('ALTER FUNCTION storage_v2_literal_term_count(TEXT, TEXT) OWNER TO %I', v_owner);
    EXECUTE format('ALTER FUNCTION storage_v2_candidate_query_evidence(BIGINT, BIGINT, TEXT, TEXT, BIGINT[], BIGINT[]) OWNER TO %I', v_owner);
END
$$;
REVOKE ALL ON FUNCTION storage_v2_literal_term_count(TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_candidate_query_evidence(BIGINT, BIGINT, TEXT, TEXT, BIGINT[], BIGINT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_literal_term_count(TEXT, TEXT) TO mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_candidate_query_evidence(BIGINT, BIGINT, TEXT, TEXT, BIGINT[], BIGINT[]) TO mainrag;
