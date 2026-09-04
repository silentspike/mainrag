-- Migration 037: atomically replace stale, pointer-neutral release candidates
--
-- A source can need a freshly qualified candidate after its adapter/profile or
-- implementation changes. Keep the previous evidence immutable, demote only
-- the previous pointer-neutral candidate, and promote the new verified
-- generation in the same transaction.

CREATE OR REPLACE FUNCTION storage_v2_replace_release_candidate(
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
    v_active_generation_id BIGINT;
    v_evidence storage_v2_release_candidate_evidence;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'release-candidate replacement requires administrator authority'
            USING ERRCODE = '42501';
    END IF;

    SELECT * INTO v_generation
      FROM source_generation
     WHERE id = p_generation_id AND source_id = p_source_id
     FOR UPDATE;
    IF NOT FOUND OR v_generation.status NOT IN ('verified', 'release_candidate') THEN
        RAISE EXCEPTION 'replacement requires the source verified generation';
    END IF;

    SELECT active_generation_id INTO v_active_generation_id
      FROM logical_source
     WHERE id = p_source_id
     FOR UPDATE;
    IF v_active_generation_id = p_generation_id THEN
        RAISE EXCEPTION 'an active generation cannot become a release candidate';
    END IF;
    IF EXISTS (
        SELECT 1 FROM source_generation
         WHERE source_id = p_source_id
           AND status = 'release_candidate'
           AND id <> p_generation_id
           AND id = v_active_generation_id
    ) THEN
        RAISE EXCEPTION 'an active release candidate cannot be replaced';
    END IF;

    UPDATE source_generation
       SET status = 'verified'
     WHERE source_id = p_source_id
       AND status = 'release_candidate'
       AND id <> p_generation_id;

    SELECT * INTO STRICT v_evidence
      FROM storage_v2_qualify_release_candidate(
        p_id,
        p_source_id,
        p_generation_id,
        p_commit_sha,
        p_source_watermark_sha256,
        p_adapter_profile_id,
        p_analysis_profile_id,
        p_search_profile_id,
        p_manifest
      );
    RETURN v_evidence;
END
$$;

REVOKE EXECUTE ON FUNCTION storage_v2_replace_release_candidate(
    UUID, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_replace_release_candidate(
    UUID, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB
) TO mainrag;
