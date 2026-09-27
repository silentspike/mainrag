-- Preserve the current PostgreSQL FTS rank for source-backed candidate items
-- whose legacy chunk text is an extracted view rather than a contiguous slice
-- of the immutable storage-v2 document.  The projection stores the rank input
-- and chunk identity; search never needs to read legacy chunks after staging.

CREATE TABLE IF NOT EXISTS storage_v2_legacy_lexical_segment (
    occurrence_id BIGINT NOT NULL REFERENCES occurrence(id) ON DELETE RESTRICT,
    source_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    legacy_chunk_id BIGINT NOT NULL CHECK (legacy_chunk_id > 0),
    legacy_file_hash BYTEA NOT NULL CHECK (octet_length(legacy_file_hash) = 32),
    fts_vector TSVECTOR NOT NULL,
    PRIMARY KEY (occurrence_id, legacy_chunk_id),
    FOREIGN KEY (occurrence_id, source_id, artifact_version_id)
        REFERENCES occurrence(id, source_id, artifact_version_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_storage_v2_legacy_lexical_segment_fts
    ON storage_v2_legacy_lexical_segment USING GIN (fts_vector);
CREATE INDEX IF NOT EXISTS idx_storage_v2_legacy_lexical_segment_source
    ON storage_v2_legacy_lexical_segment (source_id, occurrence_id);

ALTER TABLE storage_v2_legacy_lexical_segment OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE storage_v2_legacy_lexical_segment ENABLE ROW LEVEL SECURITY;
-- The security-definer rank/presence functions are owned by this table owner
-- and still apply an explicit authorized_source filter before returning rows.
-- Leave FORCE off so the immutable projection does not pay a per-row ACL call.
ALTER TABLE storage_v2_legacy_lexical_segment NO FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS storage_v2_legacy_lexical_segment_source
    ON storage_v2_legacy_lexical_segment;
CREATE POLICY storage_v2_legacy_lexical_segment_source
    ON storage_v2_legacy_lexical_segment
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));

DROP TRIGGER IF EXISTS storage_v2_legacy_lexical_segment_immutable
    ON storage_v2_legacy_lexical_segment;
CREATE TRIGGER storage_v2_legacy_lexical_segment_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_legacy_lexical_segment
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
REVOKE ALL ON storage_v2_legacy_lexical_segment FROM PUBLIC;
GRANT SELECT ON storage_v2_legacy_lexical_segment TO mainrag;

-- Called while the legacy source snapshot is still available.  The exact
-- file identity binds the projection to the immutable occurrence; chunk text
-- itself is deliberately not copied because the legacy extractor may expose
-- a normalized/extracted view instead of a contiguous document substring.
CREATE OR REPLACE FUNCTION storage_v2_materialize_legacy_chunk_ranks(
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
    v_file_hash BYTEA;
    v_count BIGINT;
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
        RAISE EXCEPTION 'authorized source-backed legacy rank projection required'
            USING ERRCODE = '42501';
    END IF;

    SELECT file.id, file.hash INTO v_file_id, v_file_hash
      FROM files file
     WHERE file.source_id = v_source_id
       AND file.path = v_path
       AND file.hash = decode(v_hash, 'hex')
       AND file.content_text = v_body;
    IF NOT FOUND THEN
        -- Fragmented source items deliberately carry a fragment hash, while
        -- the legacy file row carries the whole-file hash.  Bind those items
        -- by the immutable source path and keep the observed file hash in the
        -- projection; search still requires the candidate body to match the
        -- query before using this compatibility rank.
        SELECT file.id, file.hash INTO v_file_id, v_file_hash
          FROM files file
         WHERE file.source_id = v_source_id
           AND file.path = v_path;
        IF NOT FOUND THEN
            RETURN 0;
        END IF;
    END IF;

    INSERT INTO storage_v2_legacy_lexical_segment(
        occurrence_id, source_id, artifact_version_id,
        legacy_chunk_id, legacy_file_hash, fts_vector
    )
    SELECT p_occurrence_id, v_source_id, p_artifact_version_id,
           chunk.id, v_file_hash, chunk.fts_vector
      FROM chunks chunk
     WHERE chunk.file_id = v_file_id
    ON CONFLICT (occurrence_id, legacy_chunk_id) DO NOTHING;

    SELECT count(*) INTO v_count
      FROM storage_v2_legacy_lexical_segment projection
     WHERE projection.occurrence_id = p_occurrence_id;
    RETURN v_count;
END
$$;

DO $$
DECLARE
    v_owner NAME;
BEGIN
    SELECT pg_get_userbyid(proowner) INTO STRICT v_owner
      FROM pg_proc
     WHERE oid = 'storage_v2_copy_legacy_lexical_segments(bigint,bigint)'::REGPROCEDURE;
    EXECUTE format(
        'ALTER FUNCTION storage_v2_materialize_legacy_chunk_ranks(BIGINT,BIGINT) OWNER TO %I',
        v_owner
    );
END
$$;
REVOKE ALL ON FUNCTION storage_v2_materialize_legacy_chunk_ranks(BIGINT, BIGINT)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_materialize_legacy_chunk_ranks(BIGINT, BIGINT)
    TO mainrag;

-- Existing builders already call this copy function.  Materialize the rank
-- projection before its contiguous-substring check so extracted legacy chunks
-- remain available even when the immutable lexical fallback is generated.
DO $$
DECLARE
    v_sql TEXT := pg_get_functiondef(
        'storage_v2_copy_legacy_lexical_segments(bigint,bigint)'::REGPROCEDURE
    );
    v_old TEXT := E'    SELECT count(*) INTO v_count FROM chunks WHERE file_id = v_file_id;';
    v_new TEXT := E'    PERFORM storage_v2_materialize_legacy_chunk_ranks(\n'
        || E'        p_occurrence_id, p_artifact_version_id\n'
        || E'    );\n'
        || v_old;
BEGIN
    IF strpos(v_sql, v_old) = 0
       OR strpos(v_sql, 'storage_v2_materialize_legacy_chunk_ranks') <> 0 THEN
        RAISE EXCEPTION 'legacy lexical copy definition changed before rank projection';
    END IF;
    EXECUTE replace(v_sql, v_old, v_new);
END
$$;

-- Backfill an already sealed candidate without rebuilding its immutable body
-- or membership.  This is an operator-scoped, idempotent materialization step.
CREATE OR REPLACE FUNCTION storage_v2_backfill_legacy_chunk_ranks(
    p_source_id BIGINT, p_generation_id BIGINT
) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_occurrence RECORD;
    v_total BIGINT := 0;
    v_count BIGINT;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'legacy rank backfill requires administrator authority'
            USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM source_generation generation
         WHERE generation.id = p_generation_id
           AND generation.source_id = p_source_id
           AND generation.status IN ('verified', 'release_candidate')
    ) THEN
        RAISE EXCEPTION 'verified source generation required';
    END IF;

    FOR v_occurrence IN
        SELECT DISTINCT occurrence_row.id AS occurrence_id,
               occurrence_row.artifact_version_id
          FROM occurrence occurrence_row
          JOIN artifact_version artifact
            ON artifact.id = occurrence_row.artifact_version_id
          JOIN generation_item_version membership
            ON membership.source_id = p_source_id
           AND membership.source_item_id = artifact.item_id
           AND membership.artifact_version_id = artifact.id
          JOIN source_generation generation
            ON generation.id = p_generation_id
           AND generation.source_id = membership.source_id
         WHERE occurrence_row.source_id = p_source_id
           AND membership.valid_from_seq <= generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > generation.generation_seq)
    LOOP
        v_count := storage_v2_materialize_legacy_chunk_ranks(
            v_occurrence.occurrence_id, v_occurrence.artifact_version_id
        );
        v_total := v_total + v_count;
    END LOOP;
    RETURN v_total;
END
$$;

DO $$
DECLARE
    v_owner NAME;
BEGIN
    SELECT pg_get_userbyid(proowner) INTO STRICT v_owner
      FROM pg_proc
     WHERE oid = 'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE;
    EXECUTE format(
        'ALTER FUNCTION storage_v2_backfill_legacy_chunk_ranks(BIGINT,BIGINT) OWNER TO %I',
        v_owner
    );
END
$$;
REVOKE ALL ON FUNCTION storage_v2_backfill_legacy_chunk_ranks(BIGINT, BIGINT)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_backfill_legacy_chunk_ranks(BIGINT, BIGINT)
    TO mainrag;

-- Prefer the materialized current FTS rank when it exists.  The 1,000,000
-- tier is an internal marker consumed by the final-score expression below;
-- generated storage-v2 segments keep their existing non-legacy behavior.
CREATE OR REPLACE FUNCTION storage_v2_source_segment_ranks(
    p_occurrence_ids BIGINT[], p_query TEXT
) RETURNS TABLE(occurrence_id BIGINT, score REAL, segment_order BIGINT)
LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
BEGIN
    -- Keep the two provenance paths separate.  A single UNION plan makes the
    -- forced-RLS lexical scan repeat for every legacy projection row.
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    ), query AS (
        SELECT websearch_to_tsquery('simple', p_query) AS value
    )
    SELECT DISTINCT ON (projection.occurrence_id)
           projection.occurrence_id,
           (1000000.0 + ts_rank_cd(projection.fts_vector, query.value, 0))::REAL,
           projection.legacy_chunk_id
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN authorized_source
        ON authorized_source.id = occurrence_row.source_id
      JOIN artifact_version artifact
        ON artifact.id = occurrence_row.artifact_version_id
      JOIN storage_v2_search_view_document binding
        ON binding.view_id = occurrence_row.view_id AND binding.ordinal = 0
      JOIN storage_v2_search_document document
        ON document.id = binding.document_id
       AND document.component_kind = 'node'
       AND document.node_id = artifact.content_root_node_id
      JOIN storage_v2_legacy_lexical_segment projection
        ON projection.occurrence_id = occurrence_row.id
       AND projection.source_id = occurrence_row.source_id
       AND projection.artifact_version_id = occurrence_row.artifact_version_id
      CROSS JOIN query
     WHERE document.fts_simple @@ query.value
       AND projection.fts_vector @@ query.value
     ORDER BY projection.occurrence_id, 2 DESC, projection.legacy_chunk_id;

    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    ), query AS (
        SELECT websearch_to_tsquery('simple', p_query) AS value
    ), generated AS MATERIALIZED (
        SELECT marker.occurrence_id
          FROM requested
          JOIN storage_v2_lexical_segment marker
            ON marker.occurrence_id = requested.id
           AND marker.segment_order = 0
    )
    SELECT DISTINCT ON (segment.occurrence_id)
           segment.occurrence_id,
           (CASE WHEN generated.occurrence_id IS NULL
                   THEN 100000.0 + ts_rank_cd(segment.fts_vector, query.value)
                   ELSE LEAST(ts_rank_cd(segment.fts_vector, query.value), 99999.0)
            END)::REAL,
           segment.segment_order
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN authorized_source
        ON authorized_source.id = occurrence_row.source_id
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
      LEFT JOIN generated
        ON generated.occurrence_id = segment.occurrence_id
      CROSS JOIN query
     WHERE segment.fts_vector @@ query.value
       AND NOT EXISTS (
           SELECT 1
             FROM storage_v2_legacy_lexical_segment projection
            WHERE projection.occurrence_id = segment.occurrence_id
       )
     ORDER BY segment.occurrence_id, 2 DESC, segment.segment_order;
END
$$;

-- The same split keeps presence checks bounded to occurrences that have no
-- materialized legacy projection; it avoids scanning all sealed segments
-- under forced RLS for every occurrence on every query.
CREATE OR REPLACE FUNCTION storage_v2_source_segment_presence(
    p_occurrence_ids BIGINT[]
) RETURNS TABLE(occurrence_id BIGINT)
LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $$
BEGIN
    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    )
    SELECT DISTINCT projection.occurrence_id
      FROM requested
      JOIN storage_v2_legacy_lexical_segment projection
        ON projection.occurrence_id = requested.id
      JOIN occurrence occurrence_row
        ON occurrence_row.id = projection.occurrence_id
      JOIN authorized_source
        ON authorized_source.id = occurrence_row.source_id;

    RETURN QUERY
    WITH requested AS MATERIALIZED (
        SELECT DISTINCT id FROM unnest(p_occurrence_ids) AS input(id)
    ), authorized_source AS MATERIALIZED (
        SELECT source.id FROM sources source
         WHERE storage_v2_can_access_source(source.id, 'read')
    )
    SELECT DISTINCT occurrence_row.id
      FROM requested
      JOIN occurrence occurrence_row ON occurrence_row.id = requested.id
      JOIN authorized_source
        ON authorized_source.id = occurrence_row.source_id
     WHERE NOT EXISTS (
               SELECT 1
                 FROM storage_v2_legacy_lexical_segment projection
                WHERE projection.occurrence_id = occurrence_row.id
           )
       AND EXISTS (
               SELECT 1
                 FROM storage_v2_lexical_segment segment
                WHERE segment.occurrence_id = occurrence_row.id
                  AND segment.source_id = occurrence_row.source_id
                  AND segment.artifact_version_id = occurrence_row.artifact_version_id
           );
END
$$;

ALTER FUNCTION storage_v2_source_segment_ranks(BIGINT[], TEXT)
    OWNER TO mainrag_v2_frontier_owner;
ALTER FUNCTION storage_v2_source_segment_presence(BIGINT[])
    OWNER TO mainrag_v2_frontier_owner;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_ranks(BIGINT[], TEXT)
    TO mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_source_segment_presence(BIGINT[])
    TO mainrag;

DO $$
DECLARE
    v_function REGPROCEDURE;
    v_sql TEXT;
    v_old TEXT := E'lexical_score + graph_score + semantic_score + rerank_score AS final_score';
    v_new TEXT := E'CASE WHEN staged.segment_score >= 1000000.0\n'
        || E'                    THEN staged.segment_score\n'
        || E'                    ELSE lexical_score\n'
        || E'               END + graph_score + semantic_score + rerank_score AS final_score';
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE,
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::REGPROCEDURE
    ] LOOP
        v_sql := pg_get_functiondef(v_function);
        IF strpos(v_sql, v_old) = 0 THEN
            RAISE EXCEPTION 'storage-v2 final rank definition changed before legacy projection';
        END IF;
        EXECUTE replace(v_sql, v_old, v_new);
    END LOOP;
END
$$;
