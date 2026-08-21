-- Migration 036: independently qualified, pointer-neutral release candidates
--
-- A verified generation is not a release candidate until the production
-- qualification envelope has passed. The transition below records that
-- envelope and changes only generation state; active pointers remain unchanged.

CREATE UNIQUE INDEX IF NOT EXISTS uq_storage_v2_one_release_candidate_per_source
    ON source_generation(source_id)
    WHERE status = 'release_candidate';

CREATE TABLE IF NOT EXISTS storage_v2_release_candidate_evidence (
    id UUID PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    generation_id BIGINT NOT NULL REFERENCES source_generation(id) ON DELETE RESTRICT,
    commit_sha TEXT NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    source_watermark_sha256 TEXT NOT NULL
        CHECK (source_watermark_sha256 ~ '^[0-9a-f]{64}$'),
    adapter_profile_id TEXT NOT NULL CHECK (adapter_profile_id <> ''),
    analysis_profile_id TEXT NOT NULL CHECK (analysis_profile_id <> ''),
    search_profile_id TEXT NOT NULL CHECK (search_profile_id <> ''),
    manifest JSONB NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    manifest_sha256 BYTEA NOT NULL CHECK (octet_length(manifest_sha256) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, generation_id),
    UNIQUE (source_id, id)
);

ALTER TABLE storage_v2_release_candidate_evidence ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS storage_v2_release_candidate_evidence_isolation
    ON storage_v2_release_candidate_evidence;
CREATE POLICY storage_v2_release_candidate_evidence_isolation
    ON storage_v2_release_candidate_evidence
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));

CREATE OR REPLACE FUNCTION storage_v2_qualify_release_candidate(
    p_id UUID,
    p_source_id BIGINT,
    p_generation_id BIGINT,
    p_commit_sha TEXT,
    p_source_watermark_sha256 TEXT,
    p_adapter_profile_id TEXT,
    p_analysis_profile_id TEXT,
    p_search_profile_id TEXT,
    p_manifest JSONB
) RETURNS storage_v2_release_candidate_evidence
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
    v_run storage_v2_ingest_run;
    v_active_generation_id BIGINT;
    v_manifest_sha256 BYTEA;
    v_evidence storage_v2_release_candidate_evidence;
    v_required_checks CONSTANT TEXT[] := ARRAY[
        'artifact_root', 'authorization', 'body_pack_integrity',
        'dual_read', 'intelligence', 'intervals',
        'legacy_intelligence_export', 'resource_budget',
        'restart_resume', 'search_quality'
    ];
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'release-candidate qualification requires administrator authority'
            USING ERRCODE = '42501';
    END IF;
    IF p_id IS NULL
       OR p_commit_sha !~ '^[0-9a-f]{40}$'
       OR p_source_watermark_sha256 !~ '^[0-9a-f]{64}$'
       OR COALESCE(p_adapter_profile_id, '') = ''
       OR COALESCE(p_analysis_profile_id, '') = ''
       OR COALESCE(p_search_profile_id, '') = ''
       OR p_manifest IS NULL
       OR jsonb_typeof(p_manifest) <> 'object'
       OR COALESCE(p_manifest ->> 'status', '') <> 'PASS' THEN
        RAISE EXCEPTION 'complete release-candidate identity and PASS manifest required';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(v_required_checks) required_check
         WHERE COALESCE(p_manifest #>> ARRAY['checks', required_check], '') <> 'PASS'
    ) THEN
        RAISE EXCEPTION 'all release-candidate qualification checks must pass';
    END IF;

    SELECT * INTO v_generation
      FROM source_generation
     WHERE id = p_generation_id AND source_id = p_source_id
     FOR UPDATE;
    IF NOT FOUND OR v_generation.status NOT IN ('verified', 'release_candidate') THEN
        RAISE EXCEPTION 'qualification requires the source verified generation';
    END IF;
    SELECT * INTO STRICT v_run
      FROM storage_v2_ingest_run
     WHERE source_id = p_source_id AND generation_id = p_generation_id
       AND status = 'sealed';
    IF v_run.semantic_manifest_sha256 <> p_source_watermark_sha256
       OR v_run.adapter_profile_id <> p_adapter_profile_id
       OR v_generation.item_count IS DISTINCT FROM v_run.expected_item_count THEN
        RAISE EXCEPTION 'candidate identity or item count differs from the sealed ingest';
    END IF;
    SELECT active_generation_id INTO v_active_generation_id
      FROM logical_source WHERE id = p_source_id FOR UPDATE;
    IF v_active_generation_id IS DISTINCT FROM v_run.expected_active_generation_id THEN
        RAISE EXCEPTION 'active pointer drift during candidate construction';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM generation_item_version membership
         WHERE membership.source_id = p_source_id
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > v_generation.generation_seq)
           AND NOT EXISTS (
               SELECT 1 FROM storage_v2_ingest_run_item item
                WHERE item.run_id = v_run.id
                  AND item.source_item_id = membership.source_item_id
                  AND item.artifact_version_id = membership.artifact_version_id
           )
    ) THEN
        RAISE EXCEPTION 'visible membership is not represented by the candidate ingest';
    END IF;
    IF (SELECT COUNT(*) FROM generation_item_version membership
         WHERE membership.source_id = p_source_id
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > v_generation.generation_seq))
       <> v_generation.item_count THEN
        RAISE EXCEPTION 'candidate membership count differs from sealed item count';
    END IF;
    IF EXISTS (
        SELECT 1 FROM storage_v2_ingest_run_item item
         WHERE item.run_id = v_run.id
           AND NOT EXISTS (
               SELECT 1 FROM storage_v2_analysis_cache analysis
                WHERE analysis.content_identity_sha256 = item.content_identity_sha256
                  AND analysis.analysis_profile_id = item.analysis_profile_id
                  AND analysis.status = 'complete'
           )
    ) THEN
        RAISE EXCEPTION 'candidate contains incomplete analysis';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM storage_v2_dual_read_evidence evidence
         WHERE evidence.source_id = p_source_id
           AND evidence.generation_id = p_generation_id
           AND evidence.commit_sha = p_commit_sha
           AND COALESCE((evidence.artifact ->> 'unexplained_count')::BIGINT, -1) = 0
    ) THEN
        RAISE EXCEPTION 'accepted dual-read evidence is required';
    END IF;

    v_manifest_sha256 := digest(convert_to(p_manifest::TEXT, 'UTF8'), 'sha256');
    INSERT INTO storage_v2_release_candidate_evidence(
        id, source_id, generation_id, commit_sha, source_watermark_sha256,
        adapter_profile_id, analysis_profile_id, search_profile_id,
        manifest, manifest_sha256
    ) VALUES (
        p_id, p_source_id, p_generation_id, p_commit_sha,
        p_source_watermark_sha256, p_adapter_profile_id,
        p_analysis_profile_id, p_search_profile_id, p_manifest,
        v_manifest_sha256
    ) ON CONFLICT (source_id, generation_id) DO NOTHING
    RETURNING * INTO v_evidence;
    IF NOT FOUND THEN
        SELECT * INTO STRICT v_evidence
          FROM storage_v2_release_candidate_evidence
         WHERE source_id = p_source_id AND generation_id = p_generation_id;
        IF (v_evidence.id, v_evidence.commit_sha,
            v_evidence.source_watermark_sha256, v_evidence.adapter_profile_id,
            v_evidence.analysis_profile_id, v_evidence.search_profile_id,
            v_evidence.manifest_sha256)
           IS DISTINCT FROM
           (p_id, p_commit_sha, p_source_watermark_sha256,
            p_adapter_profile_id, p_analysis_profile_id,
            p_search_profile_id, v_manifest_sha256) THEN
            RAISE EXCEPTION 'release-candidate evidence identity collision'
                USING ERRCODE = '22000';
        END IF;
    END IF;
    IF v_generation.status = 'verified' THEN
        PERFORM storage_v2_mark_release_candidate(p_generation_id);
    END IF;
    RETURN v_evidence;
END
$$;

REVOKE INSERT, UPDATE, DELETE ON storage_v2_release_candidate_evidence FROM PUBLIC;
