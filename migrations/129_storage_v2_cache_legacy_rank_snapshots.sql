-- Reuse a complete file/chunk-to-payload map across native fragments.
-- Legacy statement revisions invalidate reuse without retaining legacy text.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $$ BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
        RAISE EXCEPTION 'legacy rank snapshot installation requires the database administrator';
    END IF;
    IF encode(sha256(convert_to(pg_get_functiondef(
        'storage_v2_materialize_legacy_chunk_ranks(bigint,bigint)'::REGPROCEDURE),'UTF8')),'hex')
        <>'f3b6348b87a8b6bd3cf462743e6b3624c1a6837b7d70bba0c336d2f158da0a88' THEN
        RAISE EXCEPTION 'unexpected preceding legacy rank materializer';
    END IF;
END $$;

-- These are coexistence invalidation witnesses, not retention roots. In
-- particular no foreign key prevents later retirement of files/chunks.
CREATE TABLE storage_v2_legacy_rank_revision (
    file_id BIGINT PRIMARY KEY,
    revision BIGINT NOT NULL CHECK (revision>0)
);
CREATE TABLE storage_v2_legacy_rank_epoch (
    singleton BOOLEAN PRIMARY KEY CHECK (singleton),
    revision BIGINT NOT NULL CHECK (revision>=0)
);
INSERT INTO storage_v2_legacy_rank_epoch VALUES (true,0);

CREATE FUNCTION storage_v2_invalidate_legacy_rank_snapshot()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE v_column TEXT; v_rows TEXT;
BEGIN
    IF TG_RELID='public.files'::REGCLASS THEN v_column:='id';
    ELSIF TG_RELID='public.chunks'::REGCLASS THEN v_column:='file_id';
    ELSE RAISE EXCEPTION 'unexpected legacy rank invalidation relation';
    END IF;
    IF TG_OP='TRUNCATE' THEN
        UPDATE storage_v2_legacy_rank_epoch SET revision=revision+1 WHERE singleton;
        RETURN NULL;
    END IF;
    v_rows:=CASE TG_OP
        WHEN 'INSERT' THEN 'SELECT * FROM new_rank_rows'
        WHEN 'DELETE' THEN 'SELECT * FROM old_rank_rows'
        ELSE 'SELECT * FROM old_rank_rows UNION ALL SELECT * FROM new_rank_rows' END;
    EXECUTE format('INSERT INTO public.storage_v2_legacy_rank_revision(file_id,revision) '
        ||'SELECT DISTINCT %I,1 FROM (%s) changed '
        ||'ON CONFLICT(file_id) DO UPDATE SET revision='
        ||'storage_v2_legacy_rank_revision.revision+1',v_column,v_rows);
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION storage_v2_invalidate_legacy_rank_snapshot() FROM PUBLIC,mainrag;
REVOKE ALL ON storage_v2_legacy_rank_revision,storage_v2_legacy_rank_epoch FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_legacy_rank_revision,storage_v2_legacy_rank_epoch
    TO mainrag_v2_frontier_owner;

DO $$ DECLARE v_table TEXT; v_op TEXT; v_transition TEXT; BEGIN
    FOREACH v_table IN ARRAY ARRAY['files','chunks'] LOOP
        FOREACH v_op IN ARRAY ARRAY['INSERT','UPDATE','DELETE'] LOOP
            v_transition:=CASE v_op
                WHEN 'INSERT' THEN 'NEW TABLE AS new_rank_rows'
                WHEN 'DELETE' THEN 'OLD TABLE AS old_rank_rows'
                ELSE 'OLD TABLE AS old_rank_rows NEW TABLE AS new_rank_rows' END;
            EXECUTE format('CREATE TRIGGER %I AFTER %s ON public.%I REFERENCING %s '
                ||'FOR EACH STATEMENT EXECUTE FUNCTION public.storage_v2_invalidate_legacy_rank_snapshot()',
                'rank_snapshot_'||lower(v_op),v_op,v_table,v_transition);
        END LOOP;
        EXECUTE format('CREATE TRIGGER rank_snapshot_truncate AFTER TRUNCATE ON public.%I '
            ||'FOR EACH STATEMENT EXECUTE FUNCTION public.storage_v2_invalidate_legacy_rank_snapshot()',v_table);
    END LOOP;
END $$;

CREATE TABLE storage_v2_legacy_rank_snapshot (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    file_id BIGINT NOT NULL,
    file_hash BYTEA NOT NULL CHECK (octet_length(file_hash)=32),
    file_revision BIGINT NOT NULL CHECK (file_revision>=0),
    global_epoch BIGINT NOT NULL CHECK (global_epoch>=0),
    chunk_count BIGINT NOT NULL CHECK (chunk_count>=0),
    UNIQUE (source_id,file_id,file_hash,file_revision,global_epoch),
    UNIQUE (id,source_id)
);
CREATE TABLE storage_v2_legacy_rank_snapshot_chunk (
    snapshot_id BIGINT NOT NULL,
    source_id BIGINT NOT NULL,
    legacy_chunk_id BIGINT NOT NULL CHECK (legacy_chunk_id>0),
    payload_id BIGINT NOT NULL,
    PRIMARY KEY (snapshot_id,legacy_chunk_id),
    FOREIGN KEY (snapshot_id,source_id) REFERENCES storage_v2_legacy_rank_snapshot(id,source_id),
    FOREIGN KEY (payload_id,source_id) REFERENCES storage_v2_legacy_rank_payload(id,source_id)
);
ALTER TABLE storage_v2_legacy_rank_snapshot OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE storage_v2_legacy_rank_snapshot_chunk OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE storage_v2_legacy_rank_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_legacy_rank_snapshot_chunk ENABLE ROW LEVEL SECURITY;
CREATE POLICY legacy_rank_snapshot_source ON storage_v2_legacy_rank_snapshot
    USING (storage_v2_can_access_source(source_id,'read'))
    WITH CHECK (storage_v2_can_access_source(source_id,'write'));
CREATE POLICY legacy_rank_snapshot_chunk_source ON storage_v2_legacy_rank_snapshot_chunk
    USING (storage_v2_can_access_source(source_id,'read'))
    WITH CHECK (storage_v2_can_access_source(source_id,'write'));
CREATE TRIGGER legacy_rank_snapshot_immutable BEFORE UPDATE OR DELETE
    ON storage_v2_legacy_rank_snapshot FOR EACH ROW
    EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
CREATE TRIGGER legacy_rank_snapshot_chunk_immutable BEFORE UPDATE OR DELETE
    ON storage_v2_legacy_rank_snapshot_chunk FOR EACH ROW
    EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
REVOKE ALL ON storage_v2_legacy_rank_snapshot,storage_v2_legacy_rank_snapshot_chunk FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_legacy_rank_snapshot,storage_v2_legacy_rank_snapshot_chunk TO mainrag;

CREATE FUNCTION storage_v2_get_legacy_rank_snapshot(
    p_source_id BIGINT,p_file_id BIGINT,p_file_hash BYTEA
) RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE v_revision BIGINT; v_epoch BIGINT; v_snapshot BIGINT; v_count BIGINT;
BEGIN
    -- Validate authorization and identity even on a cache hit. SHARE fences
    -- keep both transition revisions and vectors stable until caller COMMIT.
    PERFORM storage_v2_lock_legacy_rank_snapshot(p_source_id,p_file_id,p_file_hash);
    IF NOT pg_try_advisory_xact_lock(hashtextextended(
        'mainrag.storage-v2-ingest-source:'||p_source_id::TEXT,0)) THEN
        RAISE EXCEPTION 'another source writer is active';
    END IF;
    SELECT coalesce((SELECT revision FROM storage_v2_legacy_rank_revision
        WHERE file_id=p_file_id),0),revision INTO v_revision,v_epoch
        FROM storage_v2_legacy_rank_epoch WHERE singleton;
    SELECT id INTO v_snapshot FROM storage_v2_legacy_rank_snapshot
        WHERE source_id=p_source_id AND file_id=p_file_id AND file_hash=p_file_hash
          AND file_revision=v_revision AND global_epoch=v_epoch;
    IF FOUND THEN RETURN v_snapshot; END IF;

    -- Only a cache miss reads and hashes legacy vectors. The complete map is
    -- subsequently shared by all fragments at this file revision.
    WITH vectors AS MATERIALIZED (
        SELECT id,fts_vector,sha256(tsvectorsend(fts_vector)) AS digest
          FROM chunks WHERE file_id=p_file_id
    ), inserted AS (
        INSERT INTO storage_v2_legacy_rank_payload(source_id,payload_sha256,fts_vector)
        SELECT DISTINCT p_source_id,digest,fts_vector FROM vectors
        ON CONFLICT (source_id,payload_sha256) DO NOTHING RETURNING id
    ) SELECT count(*) INTO v_count FROM inserted;
    -- A separate statement sees payloads inserted above. Verify actual vectors,
    -- not merely digests; the fence rules out concurrent legacy replacement.
    IF EXISTS (SELECT 1 FROM chunks chunk JOIN storage_v2_legacy_rank_payload payload
        ON payload.source_id=p_source_id
       AND payload.payload_sha256=sha256(tsvectorsend(chunk.fts_vector))
        WHERE chunk.file_id=p_file_id AND payload.fts_vector IS DISTINCT FROM chunk.fts_vector) THEN
        RAISE EXCEPTION 'legacy rank payload digest collision';
    END IF;
    SELECT count(*) INTO v_count FROM chunks WHERE file_id=p_file_id;
    INSERT INTO storage_v2_legacy_rank_snapshot(
        source_id,file_id,file_hash,file_revision,global_epoch,chunk_count)
    VALUES(p_source_id,p_file_id,p_file_hash,v_revision,v_epoch,v_count) RETURNING id INTO v_snapshot;
    INSERT INTO storage_v2_legacy_rank_snapshot_chunk(snapshot_id,source_id,legacy_chunk_id,payload_id)
    SELECT v_snapshot,p_source_id,chunk.id,payload.id
      FROM chunks chunk JOIN storage_v2_legacy_rank_payload payload
        ON payload.source_id=p_source_id
       AND payload.payload_sha256=sha256(tsvectorsend(chunk.fts_vector))
     WHERE chunk.file_id=p_file_id;
    IF (SELECT count(*) FROM storage_v2_legacy_rank_snapshot_chunk WHERE snapshot_id=v_snapshot)<>v_count THEN
        RAISE EXCEPTION 'legacy rank snapshot is incomplete';
    END IF;
    RETURN v_snapshot;
END $$;
ALTER FUNCTION storage_v2_get_legacy_rank_snapshot(BIGINT,BIGINT,BYTEA) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_get_legacy_rank_snapshot(BIGINT,BIGINT,BYTEA) FROM PUBLIC,mainrag;

DO $$ DECLARE definition TEXT; first_position INTEGER; last_position INTEGER;
replacement TEXT := $write$
    v_count:=storage_v2_get_legacy_rank_snapshot(v_source_id,v_file_id,v_file_hash);
    INSERT INTO storage_v2_legacy_rank_binding(
        occurrence_id,source_id,artifact_version_id,legacy_chunk_id,legacy_file_hash,payload_id)
    SELECT p_occurrence_id,v_source_id,p_artifact_version_id,chunk.legacy_chunk_id,v_file_hash,chunk.payload_id
      FROM storage_v2_legacy_rank_snapshot_chunk chunk
     WHERE chunk.snapshot_id=v_count
    ON CONFLICT (occurrence_id,legacy_chunk_id) DO NOTHING;
    $write$;
BEGIN
    definition:=pg_get_functiondef('storage_v2_materialize_legacy_chunk_ranks(bigint,bigint)'::REGPROCEDURE);
    first_position:=strpos(definition,'    IF NOT pg_try_advisory_xact_lock(');
    last_position:=strpos(definition,'    SELECT count(*) INTO v_count');
    IF first_position=0 OR last_position<=first_position THEN
        RAISE EXCEPTION 'preceding materializer write block is unavailable';
    END IF;
    -- A separate statement both calls the getter once and makes its newly
    -- inserted snapshot rows visible to the following binding statement.
    EXECUTE substring(definition FROM 1 FOR first_position-1)||replacement
        ||substring(definition FROM last_position);
END $$;
COMMIT;
