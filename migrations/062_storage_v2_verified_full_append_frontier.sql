-- Publish a full-read append baseline only from a verified, sealed ingest run.
-- This is a measured full comparison; delta-only advancement requires a
-- separate trusted producer witness and remains unavailable to the app role.

ALTER TABLE storage_v2_append_frontier
    ADD COLUMN IF NOT EXISTS last_generation_id BIGINT,
    ADD COLUMN IF NOT EXISTS last_generation_seq BIGINT NOT NULL DEFAULT 0
        CHECK (last_generation_seq >= 0);

ALTER TABLE storage_v2_append_frontier
    DROP CONSTRAINT IF EXISTS storage_v2_append_frontier_generation_fk;
ALTER TABLE storage_v2_append_frontier
    ADD CONSTRAINT storage_v2_append_frontier_generation_fk
    FOREIGN KEY (source_id, last_generation_id)
    REFERENCES source_generation(source_id, id) ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION storage_v2_publish_full_append_frontiers(
    p_run_id BIGINT,
    p_item_keys TEXT[]
) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_run storage_v2_ingest_run;
    v_generation source_generation;
    v_expected BIGINT;
    v_matched BIGINT;
    v_updated BIGINT;
BEGIN
    SELECT * INTO v_run FROM storage_v2_ingest_run WHERE id = p_run_id FOR UPDATE;
    IF NOT FOUND OR v_run.status <> 'sealed'
       OR NOT storage_v2_can_access_source(v_run.source_id, 'write') THEN
        RAISE EXCEPTION 'sealed ingest run not found or access denied' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_generation FROM source_generation
     WHERE id = v_run.generation_id AND source_id = v_run.source_id FOR UPDATE;
    IF NOT FOUND OR v_generation.status NOT IN ('verified', 'release_candidate') THEN
        RAISE EXCEPTION 'append baseline requires a verified generation';
    END IF;
    IF p_item_keys IS NULL OR array_position(p_item_keys, NULL) IS NOT NULL THEN
        RAISE EXCEPTION 'append baseline requires explicit non-null item keys';
    END IF;
    v_expected := cardinality(p_item_keys);
    IF v_expected <> (
        SELECT COUNT(DISTINCT key) FROM unnest(p_item_keys) AS item_keys(key)
    ) THEN
        RAISE EXCEPTION 'append baseline item keys are duplicated';
    END IF;
    IF EXISTS (
        SELECT 1 FROM storage_v2_ingest_run newer
        JOIN source_generation newer_generation ON newer_generation.id = newer.generation_id
         WHERE newer.source_id = v_run.source_id
           AND newer.adapter_profile_id = v_run.adapter_profile_id
           AND newer.status = 'sealed'
           AND newer_generation.generation_seq > v_generation.generation_seq
    ) THEN
        RAISE EXCEPTION 'append baseline run is superseded';
    END IF;
    SELECT COUNT(*) INTO v_matched
      FROM storage_v2_ingest_run_item run_item
      JOIN source_item item ON item.id = run_item.source_item_id
     WHERE run_item.run_id = p_run_id
       AND run_item.source_id = v_run.source_id
       AND item.item_kind = 'document'
       AND item.item_key = ANY(p_item_keys);
    IF v_matched <> v_expected THEN
        RAISE EXCEPTION 'append baseline item is absent from the sealed run';
    END IF;

    INSERT INTO storage_v2_append_frontier(
        source_id, source_item_id, adapter_profile_id,
        prefix_bytes, prefix_sha256, last_full_sha256,
        appends_since_full, full_compared_at, updated_at,
        last_generation_id, last_generation_seq
    )
    SELECT v_run.source_id, run_item.source_item_id, v_run.adapter_profile_id,
           run_item.byte_length, run_item.content_identity_sha256,
           run_item.content_identity_sha256, 0, NOW(), NOW(),
           v_run.generation_id, v_generation.generation_seq
      FROM storage_v2_ingest_run_item run_item
      JOIN source_item item ON item.id = run_item.source_item_id
     WHERE run_item.run_id = p_run_id
       AND run_item.source_id = v_run.source_id
       AND item.item_kind = 'document'
       AND item.item_key = ANY(p_item_keys)
    ON CONFLICT (source_id, source_item_id, adapter_profile_id) DO UPDATE
       SET prefix_bytes = EXCLUDED.prefix_bytes,
           prefix_sha256 = EXCLUDED.prefix_sha256,
           last_full_sha256 = EXCLUDED.last_full_sha256,
           appends_since_full = 0,
           full_compared_at = NOW(),
           updated_at = NOW(),
           last_generation_id = EXCLUDED.last_generation_id,
           last_generation_seq = EXCLUDED.last_generation_seq
     WHERE storage_v2_append_frontier.last_generation_seq < EXCLUDED.last_generation_seq
        OR (
            storage_v2_append_frontier.last_generation_seq = EXCLUDED.last_generation_seq
            AND storage_v2_append_frontier.last_generation_id = EXCLUDED.last_generation_id
            AND storage_v2_append_frontier.prefix_bytes = EXCLUDED.prefix_bytes
            AND storage_v2_append_frontier.prefix_sha256 = EXCLUDED.prefix_sha256
        );
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    IF v_updated <> v_expected THEN
        RAISE EXCEPTION 'append baseline generation or content drift';
    END IF;
    RETURN v_updated;
END
$$;

REVOKE EXECUTE ON FUNCTION storage_v2_publish_full_append_frontiers(BIGINT, TEXT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_publish_full_append_frontiers(BIGINT, TEXT[]) TO mainrag;
REVOKE EXECUTE ON FUNCTION storage_v2_update_append_frontier(
    BIGINT, BIGINT, TEXT, BIGINT, BYTEA, BIGINT, BYTEA, BYTEA, BIGINT
) FROM PUBLIC, mainrag;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_append_frontier FROM PUBLIC, mainrag;
