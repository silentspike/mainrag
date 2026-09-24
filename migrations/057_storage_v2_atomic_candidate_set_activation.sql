-- Migration 057: one controlled transaction boundary for the complete candidate set.
-- Installation is additive. Calling the function is a separate, freshly approved
-- operation after final-delta, quality, resource, package and default-read gates.

CREATE TABLE IF NOT EXISTS storage_v2_activation_set_evidence (
    id UUID PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    source_count BIGINT NOT NULL CHECK (source_count > 0),
    pointer_set_sha256 TEXT NOT NULL CHECK (pointer_set_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE storage_v2_activation_set_evidence ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS storage_v2_activation_set_evidence_admin
    ON storage_v2_activation_set_evidence;
CREATE POLICY storage_v2_activation_set_evidence_admin
    ON storage_v2_activation_set_evidence
    USING (storage_v2_is_admin())
    WITH CHECK (storage_v2_is_admin());

CREATE OR REPLACE FUNCTION storage_v2_activate_candidate_set(
    p_manifest JSONB,
    p_expected_manifest_sha256 TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_entries JSONB;
    v_entry JSONB;
    v_activation_id UUID;
    v_source_id BIGINT;
    v_candidate_id BIGINT;
    v_expected_active_id BIGINT;
    v_source_count BIGINT;
    v_seen BIGINT[] := ARRAY[]::BIGINT[];
    v_pointer_sha256 TEXT;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'candidate-set activation requires administrator authority'
            USING ERRCODE = '42501';
    END IF;
    IF p_manifest IS NULL OR jsonb_typeof(p_manifest) <> 'object'
       OR p_manifest ->> 'schema_version' IS DISTINCT FROM
          'mainrag.storage-v2.activation-set.v1'
       OR p_expected_manifest_sha256 IS NULL
       OR p_expected_manifest_sha256 !~ '^[0-9a-f]{64}$'
       OR encode(digest(convert_to(p_manifest::TEXT, 'UTF8'), 'sha256'), 'hex')
          <> p_expected_manifest_sha256
       OR COALESCE(p_manifest ->> 'code_commit_sha', '') !~ '^[0-9a-f]{40}$'
       OR COALESCE(p_manifest ->> 'schema_sha256', '') !~ '^[0-9a-f]{64}$'
       OR COALESCE(p_manifest ->> 'backend_package_sha256', '') !~ '^[0-9a-f]{64}$'
       OR COALESCE(p_manifest ->> 'aggregate_evidence_sha256', '') !~ '^[0-9a-f]{64}$'
       OR COALESCE(p_manifest ->> 'activation_id', '') !~
          '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RAISE EXCEPTION 'exact approved candidate-set manifest identity is required';
    END IF;
    v_activation_id := (p_manifest ->> 'activation_id')::UUID;
    IF EXISTS (SELECT 1 FROM storage_v2_activation_set_evidence WHERE id = v_activation_id) THEN
        RAISE EXCEPTION 'activation identity was already used';
    END IF;
    v_entries := p_manifest -> 'sources';
    IF jsonb_typeof(v_entries) <> 'array' OR jsonb_array_length(v_entries) = 0 THEN
        RAISE EXCEPTION 'complete nonempty candidate set is required';
    END IF;

    -- Block source registration, candidate/evidence changes, and pointer
    -- changes until this statement commits or rolls back. Readers can proceed.
    LOCK TABLE sources, logical_source, source_generation,
        storage_v2_release_candidate_evidence IN SHARE ROW EXCLUSIVE MODE;
    -- Lock the entire registered set in stable order before validating or
    -- activating any candidate.
    PERFORM id FROM logical_source ORDER BY id FOR UPDATE;
    SELECT COUNT(*) INTO v_source_count FROM sources;
    IF v_source_count <> (SELECT COUNT(*) FROM logical_source)
       OR v_source_count <> jsonb_array_length(v_entries) THEN
        RAISE EXCEPTION 'candidate set does not cover every registered source';
    END IF;

    FOR v_entry IN SELECT value FROM jsonb_array_elements(v_entries) AS value
    LOOP
        IF jsonb_typeof(v_entry) <> 'object'
           OR NOT v_entry ?& ARRAY[
               'source_id', 'candidate_generation_id', 'expected_active_generation_id',
               'evidence_id', 'evidence_manifest_sha256', 'source_watermark_sha256'
           ]
           OR jsonb_typeof(v_entry -> 'source_id') <> 'number'
           OR jsonb_typeof(v_entry -> 'candidate_generation_id') <> 'number'
           OR COALESCE(v_entry ->> 'source_id', '') !~ '^[1-9][0-9]*$'
           OR COALESCE(v_entry ->> 'candidate_generation_id', '') !~ '^[1-9][0-9]*$'
           OR COALESCE(v_entry ->> 'evidence_id', '') !~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR COALESCE(v_entry ->> 'evidence_manifest_sha256', '') !~ '^[0-9a-f]{64}$'
           OR COALESCE(v_entry ->> 'source_watermark_sha256', '') !~ '^[0-9a-f]{64}$'
           OR (jsonb_typeof(v_entry -> 'expected_active_generation_id') NOT IN ('null', 'number'))
           OR (jsonb_typeof(v_entry -> 'expected_active_generation_id') = 'number'
               AND COALESCE(v_entry ->> 'expected_active_generation_id', '') !~ '^[1-9][0-9]*$')
        THEN
            RAISE EXCEPTION 'candidate-set entry has invalid identity';
        END IF;
        v_source_id := (v_entry ->> 'source_id')::BIGINT;
        v_candidate_id := (v_entry ->> 'candidate_generation_id')::BIGINT;
        v_expected_active_id := NULLIF(v_entry ->> 'expected_active_generation_id', '')::BIGINT;
        IF v_source_id = ANY(v_seen) THEN
            RAISE EXCEPTION 'candidate set contains duplicate source';
        END IF;
        v_seen := array_append(v_seen, v_source_id);
        IF NOT EXISTS (
            SELECT 1 FROM logical_source source
            JOIN source_generation candidate
              ON candidate.source_id = source.id AND candidate.id = v_candidate_id
            JOIN storage_v2_release_candidate_evidence evidence
              ON evidence.source_id = source.id AND evidence.generation_id = candidate.id
            WHERE source.id = v_source_id
              AND source.active_generation_id IS NOT DISTINCT FROM v_expected_active_id
              AND candidate.status = 'release_candidate'
              AND evidence.id = (v_entry ->> 'evidence_id')::UUID
              AND encode(evidence.manifest_sha256, 'hex') =
                  v_entry ->> 'evidence_manifest_sha256'
              AND evidence.source_watermark_sha256 =
                  v_entry ->> 'source_watermark_sha256'
              AND evidence.commit_sha = p_manifest ->> 'code_commit_sha'
        ) THEN
            RAISE EXCEPTION 'candidate, pointer or qualification evidence drift';
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM sources WHERE NOT id = ANY(v_seen)) THEN
        RAISE EXCEPTION 'candidate set omitted a registered source';
    END IF;

    FOR v_entry IN
        SELECT value FROM jsonb_array_elements(v_entries) AS value
        ORDER BY (value ->> 'source_id')::BIGINT
    LOOP
        v_source_id := (v_entry ->> 'source_id')::BIGINT;
        v_candidate_id := (v_entry ->> 'candidate_generation_id')::BIGINT;
        v_expected_active_id := NULLIF(v_entry ->> 'expected_active_generation_id', '')::BIGINT;
        PERFORM storage_v2_activate_generation(
            v_source_id, v_candidate_id, v_expected_active_id
        );
    END LOOP;
    IF (SELECT COUNT(*) FROM logical_source source
        JOIN source_generation generation
          ON generation.id = source.active_generation_id
         AND generation.source_id = source.id
         AND generation.status = 'active') <> v_source_count THEN
        RAISE EXCEPTION 'activated pointer set is incomplete';
    END IF;
    SELECT encode(digest(convert_to(
        jsonb_agg(jsonb_build_object(
            'source_id', source.id,
            'active_generation_id', source.active_generation_id
        ) ORDER BY source.id)::TEXT, 'UTF8'), 'sha256'), 'hex')
      INTO v_pointer_sha256
      FROM logical_source source;
    INSERT INTO storage_v2_activation_set_evidence(
        id, manifest_sha256, source_count, pointer_set_sha256
    ) VALUES (
        v_activation_id, p_expected_manifest_sha256, v_source_count, v_pointer_sha256
    );
    RETURN jsonb_build_object(
        'activation_id', v_activation_id,
        'manifest_sha256', p_expected_manifest_sha256,
        'source_count', v_source_count,
        'pointer_set_sha256', v_pointer_sha256,
        'status', 'ACTIVATION_STATEMENT_COMPLETE'
    );
END
$$;
