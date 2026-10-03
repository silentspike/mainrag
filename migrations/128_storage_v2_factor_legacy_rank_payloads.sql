-- Store repeated immutable legacy rank vectors once per source and payload.
-- Retain every occurrence/chunk association and the existing reader row shape.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '20min';
SET LOCAL maintenance_work_mem = '128MB';
SET LOCAL work_mem = '64MB';

DO $$
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
        RAISE EXCEPTION 'rank payload factorization requires the database administrator';
    END IF;
    IF to_regclass('public.storage_v2_legacy_rank_binding') IS NOT NULL THEN
        RAISE EXCEPTION 'factorization already installed; verify the installed layout instead of replaying data';
    END IF;
    IF (SELECT relkind FROM pg_class WHERE oid='public.storage_v2_legacy_lexical_segment'::REGCLASS)<>'r'
       OR (SELECT relowner FROM pg_class WHERE oid='public.storage_v2_legacy_lexical_segment'::REGCLASS)
            <>'mainrag_v2_frontier_owner'::REGROLE
       OR encode(sha256(convert_to(pg_get_functiondef(
            'storage_v2_materialize_legacy_chunk_ranks(bigint,bigint)'::REGPROCEDURE),'UTF8')),'hex')
            <>'85d8d1d64a3ac790d29d61ae49a2df8d66a41e5cd89e50ea238cc651e46ef8c5' THEN
        RAISE EXCEPTION 'unexpected preceding legacy rank layout or materializer';
    END IF;
    IF encode(sha256(convert_to(pg_get_functiondef(
            'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::REGPROCEDURE),'UTF8')),'hex')
            <>'a64cf3e85ef5f9b823ce1ed83c8354906f575d0f00344aa9510970723ba6b75c' THEN
        RAISE EXCEPTION 'unexpected preceding legacy rank reader';
    END IF;
END $$;

LOCK TABLE storage_v2_legacy_lexical_segment IN ACCESS EXCLUSIVE MODE;
ALTER TABLE storage_v2_legacy_lexical_segment RENAME TO storage_v2_legacy_rank_original;
ALTER INDEX idx_storage_v2_legacy_lexical_segment_fts RENAME TO idx_storage_v2_legacy_rank_original_fts;
ALTER INDEX idx_storage_v2_legacy_lexical_segment_source RENAME TO idx_storage_v2_legacy_rank_original_source;

CREATE TABLE storage_v2_legacy_rank_payload (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    payload_sha256 BYTEA NOT NULL CHECK (octet_length(payload_sha256)=32),
    fts_vector TSVECTOR NOT NULL,
    CONSTRAINT legacy_rank_payload_checksum CHECK (payload_sha256=sha256(tsvectorsend(fts_vector))),
    UNIQUE (source_id,payload_sha256),
    UNIQUE (id,source_id)
);
CREATE TABLE storage_v2_legacy_rank_binding (
    occurrence_id BIGINT NOT NULL REFERENCES occurrence(id) ON DELETE RESTRICT,
    source_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    legacy_chunk_id BIGINT NOT NULL CHECK (legacy_chunk_id>0),
    legacy_file_hash BYTEA NOT NULL CHECK (octet_length(legacy_file_hash)=32),
    payload_id BIGINT NOT NULL,
    PRIMARY KEY (occurrence_id,legacy_chunk_id),
    FOREIGN KEY (occurrence_id,source_id,artifact_version_id)
        REFERENCES occurrence(id,source_id,artifact_version_id) ON DELETE RESTRICT,
    FOREIGN KEY (payload_id,source_id)
        REFERENCES storage_v2_legacy_rank_payload(id,source_id) ON DELETE RESTRICT
);

-- Sort identifiers and digests, not millions of expanded rank vectors. Read
-- one representative vector per source/digest after the bounded-width sort.
WITH representatives AS MATERIALIZED (
    SELECT DISTINCT ON (source_id,payload_sha256)
           source_id,payload_sha256,occurrence_id,legacy_chunk_id
      FROM (SELECT source_id,sha256(tsvectorsend(fts_vector)) payload_sha256,
                   occurrence_id,legacy_chunk_id FROM storage_v2_legacy_rank_original) hashed
     ORDER BY source_id,payload_sha256,occurrence_id,legacy_chunk_id
)
INSERT INTO storage_v2_legacy_rank_payload(source_id,payload_sha256,fts_vector)
SELECT representative.source_id,representative.payload_sha256,original.fts_vector
  FROM representatives representative JOIN storage_v2_legacy_rank_original original
    USING (occurrence_id,legacy_chunk_id);

INSERT INTO storage_v2_legacy_rank_binding(
    occurrence_id,source_id,artifact_version_id,legacy_chunk_id,legacy_file_hash,payload_id)
SELECT original.occurrence_id,original.source_id,original.artifact_version_id,
       original.legacy_chunk_id,original.legacy_file_hash,payload.id
  FROM storage_v2_legacy_rank_original original JOIN storage_v2_legacy_rank_payload payload
    ON payload.source_id=original.source_id
   AND payload.payload_sha256=sha256(tsvectorsend(original.fts_vector));

CREATE INDEX idx_storage_v2_legacy_lexical_segment_fts
    ON storage_v2_legacy_rank_payload USING GIN (source_id,fts_vector);
CREATE INDEX idx_storage_v2_legacy_lexical_segment_source
    ON storage_v2_legacy_rank_binding(source_id,occurrence_id);

ALTER TABLE storage_v2_legacy_rank_payload OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE storage_v2_legacy_rank_binding OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE storage_v2_legacy_rank_payload ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_legacy_rank_binding ENABLE ROW LEVEL SECURITY;
CREATE POLICY legacy_rank_payload_source ON storage_v2_legacy_rank_payload
    USING (storage_v2_can_access_source(source_id,'read'))
    WITH CHECK (storage_v2_can_access_source(source_id,'write'));
CREATE POLICY legacy_rank_binding_source ON storage_v2_legacy_rank_binding
    USING (storage_v2_can_access_source(source_id,'read'))
    WITH CHECK (storage_v2_can_access_source(source_id,'write'));
CREATE TRIGGER legacy_rank_payload_immutable BEFORE UPDATE OR DELETE
    ON storage_v2_legacy_rank_payload FOR EACH ROW
    EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
CREATE TRIGGER legacy_rank_binding_immutable BEFORE UPDATE OR DELETE
    ON storage_v2_legacy_rank_binding FOR EACH ROW
    EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
REVOKE ALL ON storage_v2_legacy_rank_payload,storage_v2_legacy_rank_binding FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_legacy_rank_payload,storage_v2_legacy_rank_binding TO mainrag;

CREATE VIEW storage_v2_legacy_lexical_segment WITH (security_invoker=true) AS
SELECT binding.occurrence_id,payload.source_id,binding.artifact_version_id,
       binding.legacy_chunk_id,binding.legacy_file_hash,payload.fts_vector
  FROM storage_v2_legacy_rank_binding binding JOIN storage_v2_legacy_rank_payload payload
    ON payload.id=binding.payload_id AND payload.source_id=binding.source_id;
ALTER VIEW storage_v2_legacy_lexical_segment OWNER TO mainrag_v2_frontier_owner;
GRANT SELECT ON storage_v2_legacy_lexical_segment TO mainrag;

-- Delegate only a relation snapshot fence. The limited materializer owner
-- receives no write privilege on legacy files/chunks to obtain this lock.
CREATE FUNCTION storage_v2_lock_legacy_rank_snapshot(
    p_source_id BIGINT,p_file_id BIGINT,p_file_hash BYTEA
) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id,'write') THEN
        RAISE EXCEPTION 'authorized legacy rank snapshot required' USING ERRCODE='42501';
    END IF;
    LOCK TABLE files,chunks IN SHARE MODE;
    IF NOT EXISTS (SELECT 1 FROM files WHERE id=p_file_id AND source_id=p_source_id
        AND hash=p_file_hash) THEN
        RAISE EXCEPTION 'legacy rank source identity changed';
    END IF;
END $$;
REVOKE ALL ON FUNCTION storage_v2_lock_legacy_rank_snapshot(BIGINT,BIGINT,BYTEA) FROM PUBLIC,mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_lock_legacy_rank_snapshot(BIGINT,BIGINT,BYTEA)
    TO mainrag_v2_frontier_owner;

-- Equality covers the actual vectors, weights, chunk IDs and source witnesses.
-- A digest collision, omitted association or extra association aborts the DDL.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM storage_v2_legacy_rank_original original
        FULL JOIN storage_v2_legacy_lexical_segment projected
          USING (occurrence_id,legacy_chunk_id)
        WHERE original.occurrence_id IS NULL OR projected.occurrence_id IS NULL
           OR ROW(original.source_id,original.artifact_version_id,original.legacy_file_hash,original.fts_vector)
                IS DISTINCT FROM ROW(projected.source_id,projected.artifact_version_id,
                                     projected.legacy_file_hash,projected.fts_vector)) THEN
        RAISE EXCEPTION 'factored rank projection is not the complete original relation';
    END IF;
END $$;

-- Preserve the original source/body/authorization checks and extracted-view
-- fallback. Only its write block changes; readers consume the identical view.
DO $$
DECLARE
    definition TEXT;
    first_position INTEGER;
    last_position INTEGER;
    replacement TEXT := $write$
    IF NOT pg_try_advisory_xact_lock(hashtextextended(
        'mainrag.storage-v2-ingest-source:'||v_source_id::TEXT,0)) THEN
        RAISE EXCEPTION 'another source writer is active';
    END IF;
    -- One relation fence, not a tuple lock per chunk. Keep both legacy lookup
    -- statements on the same snapshot while the source remains in coexistence.
    PERFORM storage_v2_lock_legacy_rank_snapshot(v_source_id,v_file_id,v_file_hash);
    INSERT INTO storage_v2_legacy_rank_payload(source_id,payload_sha256,fts_vector)
    SELECT DISTINCT v_source_id,sha256(tsvectorsend(chunk.fts_vector)),chunk.fts_vector
      FROM chunks chunk WHERE chunk.file_id=v_file_id
       AND NOT EXISTS (SELECT 1 FROM storage_v2_legacy_rank_binding binding
           WHERE binding.occurrence_id=p_occurrence_id AND binding.legacy_chunk_id=chunk.id)
    ON CONFLICT (source_id,payload_sha256) DO NOTHING;

    IF EXISTS (SELECT 1 FROM chunks chunk JOIN storage_v2_legacy_rank_payload payload
        ON payload.source_id=v_source_id
       AND payload.payload_sha256=sha256(tsvectorsend(chunk.fts_vector))
        WHERE chunk.file_id=v_file_id AND payload.fts_vector IS DISTINCT FROM chunk.fts_vector) THEN
        RAISE EXCEPTION 'legacy rank payload digest collision';
    END IF;
    INSERT INTO storage_v2_legacy_rank_binding(
        occurrence_id,source_id,artifact_version_id,legacy_chunk_id,legacy_file_hash,payload_id)
    SELECT p_occurrence_id,v_source_id,p_artifact_version_id,chunk.id,v_file_hash,payload.id
      FROM chunks chunk JOIN storage_v2_legacy_rank_payload payload
        ON payload.source_id=v_source_id
       AND payload.payload_sha256=sha256(tsvectorsend(chunk.fts_vector))
     WHERE chunk.file_id=v_file_id
    ON CONFLICT (occurrence_id,legacy_chunk_id) DO NOTHING;
    $write$;
BEGIN
    definition := pg_get_functiondef('storage_v2_materialize_legacy_chunk_ranks(bigint,bigint)'::REGPROCEDURE);
    first_position := strpos(definition,'    INSERT INTO storage_v2_legacy_lexical_segment(');
    last_position := strpos(definition,'    SELECT count(*) INTO v_count');
    IF first_position=0 OR last_position<=first_position THEN
        RAISE EXCEPTION 'preceding materializer write block is unavailable';
    END IF;
    EXECUTE substring(definition FROM 1 FOR first_position-1)||replacement
         ||substring(definition FROM last_position);
END $$;

-- Score each matching immutable payload once before expanding its bindings.
-- Restrict expansion to requested identities; the original authorization,
-- canonical-document, matching and rank-tie gates remain in the reader.
DO $$
DECLARE
    definition TEXT;
    first_position INTEGER;
    last_position INTEGER;
    replacement TEXT := $read$
    ), ranked_payload AS MATERIALIZED (
        SELECT payload.id,payload.source_id,
               ts_rank_cd(payload.fts_vector,v_query,0)::DOUBLE PRECISION AS legacy_score
          FROM storage_v2_legacy_rank_payload payload
         WHERE payload.fts_vector @@ v_query
           AND payload.source_id=ANY(v_requested_sources)
    ), query_projection AS MATERIALIZED (
        SELECT binding.occurrence_id,binding.source_id,binding.artifact_version_id,
               binding.legacy_chunk_id,payload.legacy_score
          FROM ranked_payload payload JOIN storage_v2_legacy_rank_binding binding
            ON binding.payload_id=payload.id AND binding.source_id=payload.source_id
         WHERE EXISTS (SELECT 1 FROM requested WHERE requested.id=binding.occurrence_id)
    $read$;
BEGIN
    definition := pg_get_functiondef(
        'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::REGPROCEDURE);
    first_position := strpos(definition,'    ), query_projection AS MATERIALIZED (');
    last_position := strpos(definition,'    ), ranked_projection AS MATERIALIZED (');
    IF first_position=0 OR last_position<=first_position THEN
        RAISE EXCEPTION 'preceding rank reader projection is unavailable';
    END IF;
    EXECUTE substring(definition FROM 1 FOR first_position-1)||replacement
         ||substring(definition FROM last_position);
END $$;

-- Remove only the physically redundant native relation after complete
-- equality was proven in this transaction. Legacy files/chunks stay intact.
DROP TABLE storage_v2_legacy_rank_original;
ANALYZE storage_v2_legacy_rank_payload;
ANALYZE storage_v2_legacy_rank_binding;
COMMIT;
