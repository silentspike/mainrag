-- A source-local witness for immutable managed segments. Only verified runs
-- may advance it; the application role cannot modify it directly.
CREATE TABLE IF NOT EXISTS storage_v2_managed_append_frontier (
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    adapter_profile_id TEXT NOT NULL CHECK (adapter_profile_id <> ''),
    epoch UUID NOT NULL,
    segment_count BIGINT NOT NULL CHECK (segment_count >= 0),
    chain BYTEA NOT NULL CHECK (octet_length(chain) = 32),
    last_run_id BIGINT NOT NULL REFERENCES storage_v2_ingest_run(id) ON DELETE RESTRICT,
    last_generation_id BIGINT NOT NULL,
    appends_since_full BIGINT NOT NULL CHECK (appends_since_full >= 0),
    full_compared_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_id, adapter_profile_id),
    FOREIGN KEY (source_id, last_generation_id)
        REFERENCES source_generation(source_id, id) ON DELETE RESTRICT
);
ALTER TABLE storage_v2_managed_append_frontier ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS storage_v2_managed_append_frontier_isolation
    ON storage_v2_managed_append_frontier;
CREATE POLICY storage_v2_managed_append_frontier_isolation
    ON storage_v2_managed_append_frontier
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP TRIGGER IF EXISTS storage_v2_managed_append_frontier_controlled
    ON storage_v2_managed_append_frontier;
CREATE TRIGGER storage_v2_managed_append_frontier_controlled
    BEFORE INSERT OR UPDATE OR DELETE ON storage_v2_managed_append_frontier
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_controlled_update();
REVOKE INSERT, UPDATE, DELETE ON storage_v2_managed_append_frontier FROM PUBLIC, mainrag;
GRANT SELECT ON storage_v2_managed_append_frontier TO mainrag;

CREATE OR REPLACE FUNCTION storage_v2_copy_managed_append_prefix(
    p_run_id BIGINT, p_prior_run_id BIGINT,
    p_item_keys TEXT[], p_digests BYTEA[], p_lengths BIGINT[]
) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_run storage_v2_ingest_run;
    v_prior storage_v2_ingest_run;
    v_frontier storage_v2_managed_append_frontier;
    v_count BIGINT;
    v_matched BIGINT;
BEGIN
    SELECT * INTO v_run FROM storage_v2_ingest_run WHERE id = p_run_id FOR UPDATE;
    SELECT * INTO v_prior FROM storage_v2_ingest_run WHERE id = p_prior_run_id;
    IF NOT FOUND OR v_run.id IS NULL OR v_run.status <> 'building'
       OR v_prior.status <> 'sealed'
       OR v_run.source_id <> v_prior.source_id
       OR v_run.adapter_profile_id <> v_prior.adapter_profile_id
       OR v_run.adapter_profile_id NOT LIKE 'mainrag.managed-append-%.v2.manifest'
       OR NOT EXISTS (SELECT 1 FROM sources WHERE id = v_run.source_id
                      AND type = 'managed_append')
       OR v_run.id = v_prior.id
       OR NOT storage_v2_can_access_source(v_run.source_id, 'write') THEN
        RAISE EXCEPTION 'managed append runs are not reusable' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_frontier FROM storage_v2_managed_append_frontier
     WHERE source_id = v_run.source_id AND adapter_profile_id = v_run.adapter_profile_id
     FOR UPDATE;
    IF NOT FOUND OR v_frontier.last_run_id <> p_prior_run_id
       OR v_frontier.last_generation_id <> v_prior.generation_id
       OR NOT EXISTS (
           SELECT 1 FROM source_generation generation
            WHERE generation.id = v_prior.generation_id
              AND generation.source_id = v_run.source_id
              AND generation.status IN ('verified', 'release_candidate')
       ) OR EXISTS (
           SELECT 1 FROM storage_v2_ingest_run newer
           JOIN source_generation generation ON generation.id = newer.generation_id
            WHERE newer.source_id = v_run.source_id AND newer.status = 'sealed'
              AND generation.generation_seq > (
                  SELECT generation_seq FROM source_generation WHERE id = v_prior.generation_id
              )
       ) THEN
        RAISE EXCEPTION 'managed append prior frontier is stale';
    END IF;
    v_count := cardinality(p_item_keys);
    IF p_item_keys IS NULL OR p_digests IS NULL OR p_lengths IS NULL
       OR v_count <> cardinality(p_digests)
       OR v_count <> cardinality(p_lengths)
       OR v_count <> v_frontier.segment_count
       OR v_count <> (SELECT COUNT(*) FROM storage_v2_ingest_run_item WHERE run_id = p_prior_run_id)
       OR array_position(p_item_keys, NULL) IS NOT NULL
       OR array_position(p_digests, NULL) IS NOT NULL
       OR array_position(p_lengths, NULL) IS NOT NULL
       OR v_count <> (SELECT COUNT(DISTINCT key) FROM unnest(p_item_keys) AS keys(key)) THEN
        RAISE EXCEPTION 'managed append prefix arrays are invalid';
    END IF;
    SELECT COUNT(*) INTO v_matched
      FROM unnest(p_item_keys, p_digests, p_lengths) AS desired(key, digest, length)
      JOIN source_item item ON item.source_id = v_run.source_id
           AND item.item_kind = 'document' AND item.item_key = desired.key
      JOIN storage_v2_ingest_run_item prior_item ON prior_item.run_id = p_prior_run_id
           AND prior_item.source_id = v_run.source_id AND prior_item.source_item_id = item.id
      JOIN storage_v2_analysis_cache analysis
           ON analysis.content_identity_sha256 = prior_item.content_identity_sha256
           AND analysis.analysis_profile_id = prior_item.analysis_profile_id
     WHERE prior_item.content_identity_sha256 = desired.digest
       AND prior_item.byte_length = desired.length
       AND analysis.status = 'complete';
    IF v_matched <> v_count THEN
        RAISE EXCEPTION 'managed append prior staged content is incomplete or changed';
    END IF;
    INSERT INTO storage_v2_ingest_run_item(
        run_id, source_id, source_item_id, artifact_version_id, occurrence_id,
        content_identity_sha256, analysis_profile_id, byte_length, parser_pass_count
    )
    SELECT p_run_id, prior_item.source_id, prior_item.source_item_id,
           prior_item.artifact_version_id, prior_item.occurrence_id,
           prior_item.content_identity_sha256, prior_item.analysis_profile_id,
           prior_item.byte_length, 0
      FROM storage_v2_ingest_run_item prior_item
     WHERE prior_item.run_id = p_prior_run_id
    ON CONFLICT (run_id, source_item_id) DO NOTHING;
    SELECT COUNT(*) INTO v_matched
      FROM storage_v2_ingest_run_item staged
      JOIN storage_v2_ingest_run_item prior_item
        ON prior_item.run_id = p_prior_run_id
       AND prior_item.source_item_id = staged.source_item_id
     WHERE staged.run_id = p_run_id
       AND (staged.source_id, staged.artifact_version_id, staged.occurrence_id,
            staged.content_identity_sha256, staged.analysis_profile_id, staged.byte_length)
           = (prior_item.source_id, prior_item.artifact_version_id, prior_item.occurrence_id,
              prior_item.content_identity_sha256, prior_item.analysis_profile_id, prior_item.byte_length);
    IF v_matched <> v_count THEN
        RAISE EXCEPTION 'managed append copied run items conflict';
    END IF;
    RETURN v_count;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_publish_managed_append_frontier(
    p_run_id BIGINT, p_prior_generation_id BIGINT, p_epoch UUID,
    p_segment_count BIGINT, p_chain BYTEA, p_full_comparison BOOLEAN
) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_run storage_v2_ingest_run;
    v_generation source_generation;
    v_frontier storage_v2_managed_append_frontier;
    v_appends BIGINT;
    v_full_at TIMESTAMPTZ;
BEGIN
    SELECT * INTO v_run FROM storage_v2_ingest_run WHERE id = p_run_id FOR UPDATE;
    IF NOT FOUND OR v_run.status <> 'sealed'
       OR NOT storage_v2_can_access_source(v_run.source_id, 'write') THEN
        RAISE EXCEPTION 'verified managed append run is required' USING ERRCODE = '42501';
    END IF;
    IF v_run.adapter_profile_id NOT LIKE 'mainrag.managed-append-%.v2.manifest'
       OR NOT EXISTS (SELECT 1 FROM sources WHERE id = v_run.source_id
                      AND type = 'managed_append') THEN
        RAISE EXCEPTION 'managed append source and profile are required';
    END IF;
    SELECT * INTO v_generation FROM source_generation
     WHERE id = v_run.generation_id AND source_id = v_run.source_id FOR UPDATE;
    IF NOT FOUND OR v_generation.status NOT IN ('verified', 'release_candidate')
       OR p_epoch IS NULL OR p_segment_count IS NULL OR p_segment_count < 0
       OR octet_length(p_chain) <> 32 OR p_full_comparison IS NULL
       OR p_segment_count <> (SELECT COUNT(*) FROM storage_v2_ingest_run_item
                              WHERE run_id = p_run_id) THEN
        RAISE EXCEPTION 'managed append frontier publication is incomplete';
    END IF;
    IF EXISTS (
        SELECT 1 FROM storage_v2_ingest_run newer
        JOIN source_generation generation ON generation.id = newer.generation_id
         WHERE newer.source_id = v_run.source_id AND newer.status = 'sealed'
           AND generation.generation_seq > v_generation.generation_seq
    ) THEN
        RAISE EXCEPTION 'managed append run is superseded';
    END IF;
    SELECT * INTO v_frontier FROM storage_v2_managed_append_frontier
     WHERE source_id = v_run.source_id AND adapter_profile_id = v_run.adapter_profile_id
     FOR UPDATE;
    IF FOUND THEN
        IF v_frontier.last_generation_id = v_run.generation_id THEN
            IF v_frontier.epoch <> p_epoch OR v_frontier.segment_count <> p_segment_count
               OR v_frontier.chain <> p_chain THEN
                RAISE EXCEPTION 'managed append frontier idempotency drift';
            END IF;
            RETURN p_segment_count;
        END IF;
        IF p_prior_generation_id IS DISTINCT FROM v_frontier.last_generation_id
           OR v_frontier.epoch <> p_epoch
           OR p_segment_count < v_frontier.segment_count THEN
            RAISE EXCEPTION 'managed append prior frontier drift';
        END IF;
        v_appends := CASE WHEN p_full_comparison THEN 0
            ELSE v_frontier.appends_since_full + 1 END;
        v_full_at := CASE WHEN p_full_comparison THEN NOW()
            ELSE v_frontier.full_compared_at END;
    ELSE
        IF p_prior_generation_id IS NOT NULL OR NOT p_full_comparison THEN
            RAISE EXCEPTION 'managed append initial frontier requires a full comparison';
        END IF;
        v_appends := 0;
        v_full_at := NOW();
    END IF;
    INSERT INTO storage_v2_managed_append_frontier(
        source_id, adapter_profile_id, epoch, segment_count, chain,
        last_run_id, last_generation_id, appends_since_full, full_compared_at
    ) VALUES (
        v_run.source_id, v_run.adapter_profile_id, p_epoch, p_segment_count,
        p_chain, p_run_id, v_run.generation_id, v_appends, v_full_at
    ) ON CONFLICT (source_id, adapter_profile_id) DO UPDATE
       SET epoch = EXCLUDED.epoch, segment_count = EXCLUDED.segment_count,
           chain = EXCLUDED.chain, last_run_id = EXCLUDED.last_run_id,
           last_generation_id = EXCLUDED.last_generation_id,
           appends_since_full = EXCLUDED.appends_since_full,
           full_compared_at = EXCLUDED.full_compared_at, updated_at = NOW();
    RETURN p_segment_count;
END
$$;

REVOKE EXECUTE ON FUNCTION storage_v2_copy_managed_append_prefix(
    BIGINT, BIGINT, TEXT[], BYTEA[], BIGINT[]) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION storage_v2_publish_managed_append_frontier(
    BIGINT, BIGINT, UUID, BIGINT, BYTEA, BOOLEAN) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_copy_managed_append_prefix(
    BIGINT, BIGINT, TEXT[], BYTEA[], BIGINT[]) TO mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_publish_managed_append_frontier(
    BIGINT, BIGINT, UUID, BIGINT, BYTEA, BOOLEAN) TO mainrag;
