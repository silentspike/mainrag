-- Migration 038: bind release-candidate reuse to the implementation witness
--
-- Equal source bytes and adapter profiles are a semantic no-op only when the
-- newest sealed run was built by the same implementation commit. A changed
-- commit must allocate a new immutable generation so verification and release
-- candidate evidence can bind to the code that produced it.

CREATE OR REPLACE FUNCTION storage_v2_begin_shadow_ingest(
    p_source_id BIGINT,
    p_idempotency_key TEXT,
    p_semantic_manifest_sha256 TEXT,
    p_adapter_profile_id TEXT,
    p_witness_type TEXT,
    p_witness JSONB,
    p_force BOOLEAN DEFAULT FALSE
) RETURNS storage_v2_ingest_run
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_run storage_v2_ingest_run;
    v_generation source_generation;
    v_active BIGINT;
    v_existing_commit TEXT;
    v_requested_commit TEXT;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'write') THEN
        RAISE EXCEPTION 'source write access denied' USING ERRCODE = '42501';
    END IF;
    v_requested_commit := p_witness ->> 'commit_sha';
    IF p_idempotency_key !~ '^[0-9a-f]{64}$'
       OR p_semantic_manifest_sha256 !~ '^[0-9a-f]{64}$'
       OR p_adapter_profile_id IS NULL OR p_adapter_profile_id = ''
       OR (v_requested_commit IS NOT NULL
           AND v_requested_commit !~ '^[0-9a-f]{40}$') THEN
        RAISE EXCEPTION 'valid ingest identity, adapter profile, and commit witness are required';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('mainrag.storage-v2-ingest-source:' || p_source_id::TEXT, 0)
    );
    SELECT * INTO v_run FROM storage_v2_ingest_run
     WHERE source_id = p_source_id AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        SELECT witness ->> 'commit_sha' INTO v_existing_commit
          FROM source_generation WHERE id = v_run.generation_id;
        IF (v_run.semantic_manifest_sha256, v_run.adapter_profile_id, v_run.forced)
           IS DISTINCT FROM (p_semantic_manifest_sha256, p_adapter_profile_id, p_force)
           OR v_existing_commit IS DISTINCT FROM v_requested_commit THEN
            RAISE EXCEPTION 'ingest idempotency key collision' USING ERRCODE = '22000';
        END IF;
        RETURN v_run;
    END IF;
    IF NOT p_force THEN
        SELECT candidate.* INTO v_run
          FROM storage_v2_ingest_run candidate
          JOIN source_generation generation ON generation.id = candidate.generation_id
         WHERE candidate.source_id = p_source_id
           AND candidate.status = 'sealed'
         ORDER BY generation.generation_seq DESC
         LIMIT 1;
        IF FOUND THEN
            SELECT witness ->> 'commit_sha' INTO v_existing_commit
              FROM source_generation WHERE id = v_run.generation_id;
            IF v_run.adapter_profile_id = p_adapter_profile_id
               AND v_run.semantic_manifest_sha256 = p_semantic_manifest_sha256
               AND v_existing_commit IS NOT DISTINCT FROM v_requested_commit THEN
                RETURN v_run;
            END IF;
        END IF;
    END IF;
    v_generation := storage_v2_allocate_generation(
        p_source_id, p_witness_type, p_witness
    );
    SELECT active_generation_id INTO v_active FROM logical_source WHERE id = p_source_id;
    INSERT INTO storage_v2_ingest_run(
        source_id, generation_id, idempotency_key, semantic_manifest_sha256,
        adapter_profile_id, forced, expected_active_generation_id
    ) VALUES (
        p_source_id, v_generation.id, p_idempotency_key, p_semantic_manifest_sha256,
        p_adapter_profile_id, p_force, v_active
    ) RETURNING * INTO v_run;
    RETURN v_run;
END
$$;
