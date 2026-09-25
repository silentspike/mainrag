-- Permit source-local ordinary ingest only after a complete reviewed cutover.
-- Each pointer advance is atomic with an immutable receipt chained to the
-- original activation. Installation changes neither data nor read selection.

CREATE TABLE IF NOT EXISTS storage_v2_active_ingest_receipt (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    activation_id UUID NOT NULL REFERENCES storage_v2_activation_set_evidence(id) ON DELETE RESTRICT,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    prior_generation_id BIGINT NOT NULL REFERENCES source_generation(id) ON DELETE RESTRICT,
    generation_id BIGINT NOT NULL REFERENCES source_generation(id) ON DELETE RESTRICT,
    run_id BIGINT NOT NULL REFERENCES storage_v2_ingest_run(id) ON DELETE RESTRICT,
    source_watermark_sha256 TEXT NOT NULL CHECK (source_watermark_sha256 ~ '^[0-9a-f]{64}$'),
    verification_manifest_sha256 TEXT NOT NULL CHECK (verification_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    verification_proof_sha256 TEXT NOT NULL CHECK (verification_proof_sha256 ~ '^[0-9a-f]{64}$'),
    before_pointer_set_sha256 TEXT NOT NULL CHECK (before_pointer_set_sha256 ~ '^[0-9a-f]{64}$'),
    after_pointer_set_sha256 TEXT NOT NULL CHECK (after_pointer_set_sha256 ~ '^[0-9a-f]{64}$'),
    source_count BIGINT NOT NULL CHECK (source_count > 0),
    source_classification_sha256 TEXT NOT NULL
        CHECK (source_classification_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (generation_id),
    UNIQUE (run_id)
);
CREATE INDEX IF NOT EXISTS storage_v2_active_ingest_receipt_latest
    ON storage_v2_active_ingest_receipt (activation_id, id DESC);

ALTER TABLE storage_v2_active_ingest_receipt ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS storage_v2_active_ingest_receipt_admin
    ON storage_v2_active_ingest_receipt;
CREATE POLICY storage_v2_active_ingest_receipt_admin
    ON storage_v2_active_ingest_receipt
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP TRIGGER IF EXISTS storage_v2_active_ingest_receipt_controlled
    ON storage_v2_active_ingest_receipt;
CREATE TRIGGER storage_v2_active_ingest_receipt_controlled
    BEFORE INSERT OR UPDATE OR DELETE ON storage_v2_active_ingest_receipt
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_controlled_update();

CREATE OR REPLACE FUNCTION storage_v2_reject_active_ingest_receipt_mutation()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    RAISE EXCEPTION 'active ingest receipts are immutable' USING ERRCODE = '55000';
END
$$;
DROP TRIGGER IF EXISTS storage_v2_active_ingest_receipt_immutable
    ON storage_v2_active_ingest_receipt;
CREATE TRIGGER storage_v2_active_ingest_receipt_immutable
    BEFORE UPDATE OR DELETE ON storage_v2_active_ingest_receipt
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_active_ingest_receipt_mutation();

CREATE OR REPLACE FUNCTION storage_v2_pointer_set_sha256() RETURNS TEXT
LANGUAGE SQL STABLE SECURITY DEFINER SET search_path = pg_catalog, public
SET row_security = off AS $$
    SELECT encode(digest(convert_to(COALESCE(jsonb_agg(jsonb_build_object(
        'source_id', pointer.id,
        'active_generation_id', pointer.active_generation_id
    ) ORDER BY pointer.id), '[]'::JSONB)::TEXT, 'UTF8'), 'sha256'), 'hex')
    FROM logical_source pointer
$$;

CREATE OR REPLACE FUNCTION storage_v2_source_classification_sha256() RETURNS TEXT
LANGUAGE SQL STABLE SECURITY DEFINER SET search_path = pg_catalog, public
SET row_security = off AS $$
    SELECT encode(digest(convert_to(COALESCE(jsonb_agg(jsonb_build_object(
        'source_id', source.id, 'is_test', source.is_test
    ) ORDER BY source.id), '[]'::JSONB)::TEXT, 'UTF8'), 'sha256'), 'hex')
    FROM sources source
$$;

CREATE OR REPLACE FUNCTION storage_v2_require_complete_active_set(
    p_manifest_sha256 TEXT
) RETURNS VOID
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = off AS $$
DECLARE
    v_activation storage_v2_activation_set_evidence;
    v_follow_on storage_v2_active_ingest_receipt;
    v_pointer_sha256 TEXT;
BEGIN
    IF p_manifest_sha256 IS NULL OR p_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'exact activated manifest digest is required';
    END IF;
    SELECT * INTO v_activation FROM storage_v2_activation_set_evidence
     ORDER BY created_at DESC, id DESC LIMIT 1;
    IF NOT FOUND OR v_activation.manifest_sha256 <> p_manifest_sha256
       OR v_activation.source_count <> (SELECT COUNT(*) FROM sources)
       OR v_activation.source_count <> (SELECT COUNT(*) FROM logical_source)
       OR v_activation.source_classification_sha256 IS DISTINCT FROM
          storage_v2_source_classification_sha256()
       OR EXISTS (
           SELECT 1 FROM logical_source pointer
           LEFT JOIN source_generation generation
             ON generation.id = pointer.active_generation_id
            AND generation.source_id = pointer.id
           WHERE generation.id IS NULL OR generation.status <> 'active'
       ) THEN
        RAISE EXCEPTION 'complete activated source set and exact receipt are required';
    END IF;
    SELECT * INTO v_follow_on FROM storage_v2_active_ingest_receipt
     WHERE activation_id = v_activation.id ORDER BY id DESC LIMIT 1;
    v_pointer_sha256 := CASE WHEN FOUND THEN v_follow_on.after_pointer_set_sha256
                             ELSE v_activation.pointer_set_sha256 END;
    IF v_pointer_sha256 IS DISTINCT FROM storage_v2_pointer_set_sha256()
       OR (v_follow_on.id IS NOT NULL AND
           (v_follow_on.source_count <> v_activation.source_count
            OR v_follow_on.source_classification_sha256 <>
               v_activation.source_classification_sha256)) THEN
        RAISE EXCEPTION 'active pointer set differs from its latest receipt';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_activate_regular_ingest(
    p_manifest_sha256 TEXT,
    p_source_id BIGINT,
    p_generation_id BIGINT,
    p_expected_active_id BIGINT,
    p_source_type TEXT,
    p_source_path TEXT,
    p_source_watermark_sha256 TEXT,
    p_verification_manifest_sha256 TEXT,
    p_verification_proof_sha256 TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = off AS $$
DECLARE
    v_activation storage_v2_activation_set_evidence;
    v_follow_on storage_v2_active_ingest_receipt;
    v_source logical_source;
    v_prior source_generation;
    v_candidate source_generation;
    v_run storage_v2_ingest_run;
    v_before TEXT;
    v_after TEXT;
    v_receipt_id BIGINT;
BEGIN
    IF NOT storage_v2_is_admin()
       OR NOT storage_v2_can_access_source(p_source_id, 'write') THEN
        RAISE EXCEPTION 'regular ingest requires source administrator authority'
            USING ERRCODE = '42501';
    END IF;
    IF p_generation_id IS NULL OR p_generation_id <= 0
       OR p_expected_active_id IS NULL OR p_expected_active_id <= 0
       OR p_source_type IS NULL OR p_source_type = ''
       OR p_source_path IS NULL OR p_source_path = ''
       OR p_source_watermark_sha256 !~ '^[0-9a-f]{64}$'
       OR p_verification_manifest_sha256 !~ '^[0-9a-f]{64}$'
       OR p_verification_proof_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'complete regular ingest identity and verification are required';
    END IF;
    -- A single activation row serializes advances across all sources. Readers
    -- continue to see the old complete set until this transaction commits.
    SELECT * INTO v_activation FROM storage_v2_activation_set_evidence
     ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE;
    IF NOT FOUND OR v_activation.manifest_sha256 <> p_manifest_sha256 THEN
        RAISE EXCEPTION 'reviewed activation manifest differs';
    END IF;
    LOCK TABLE sources, logical_source, source_generation
        IN SHARE ROW EXCLUSIVE MODE;
    PERFORM storage_v2_require_complete_active_set(p_manifest_sha256);
    SELECT * INTO v_follow_on FROM storage_v2_active_ingest_receipt
     WHERE activation_id = v_activation.id ORDER BY id DESC LIMIT 1;
    v_before := CASE WHEN FOUND THEN v_follow_on.after_pointer_set_sha256
                     ELSE v_activation.pointer_set_sha256 END;
    SELECT * INTO v_source FROM logical_source
     WHERE id = p_source_id FOR UPDATE;
    IF NOT FOUND OR v_source.active_generation_id IS DISTINCT FROM p_expected_active_id THEN
        RAISE EXCEPTION 'regular ingest active pointer drift';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM sources WHERE id = p_source_id AND NOT is_test
          AND type = p_source_type AND path = p_source_path
    ) THEN
        RAISE EXCEPTION 'regular ingest source registry drift';
    END IF;
    SELECT * INTO v_prior FROM source_generation
     WHERE id = p_expected_active_id AND source_id = p_source_id FOR UPDATE;
    SELECT * INTO v_candidate FROM source_generation
     WHERE id = p_generation_id AND source_id = p_source_id FOR UPDATE;
    SELECT * INTO v_run FROM storage_v2_ingest_run
     WHERE generation_id = p_generation_id AND source_id = p_source_id AND status = 'sealed'
     FOR UPDATE;
    IF v_prior.id IS NULL OR v_candidate.id IS NULL OR v_run.id IS NULL
       OR v_prior.status <> 'active' OR v_candidate.status <> 'verified'
       OR v_candidate.generation_seq <= v_prior.generation_seq
       OR v_candidate.verification_manifest_sha256 IS DISTINCT FROM
          p_verification_manifest_sha256
       OR v_candidate.witness ->> 'source_watermark_sha256' IS DISTINCT FROM
          p_source_watermark_sha256
       OR v_candidate.witness ->> 'adapter_profile_id' IS DISTINCT FROM
          v_run.adapter_profile_id
       OR v_candidate.witness ->> 'is_test' IS DISTINCT FROM 'false'
       OR COALESCE(v_candidate.witness ->> 'commit_sha', '') !~ '^[0-9a-f]{40}$'
       OR v_run.expected_active_generation_id IS DISTINCT FROM p_expected_active_id
       OR v_run.semantic_manifest_sha256 IS DISTINCT FROM p_source_watermark_sha256
       OR v_run.expected_item_count IS DISTINCT FROM v_candidate.item_count
       OR v_run.generation_root_sha256 IS DISTINCT FROM
          storage_v2_shadow_generation_root(v_run.id)
       OR EXISTS (
           SELECT 1 FROM source_generation later
            WHERE later.source_id = p_source_id
              AND later.generation_seq > v_candidate.generation_seq
              AND later.status IN ('sealed', 'verified', 'release_candidate', 'active')
       ) THEN
        RAISE EXCEPTION 'regular ingest generation, root or watermark drift';
    END IF;
    UPDATE source_generation SET status = 'superseded', superseded_at = NOW()
     WHERE id = p_expected_active_id AND status = 'active';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'regular ingest prior active generation changed';
    END IF;
    UPDATE source_generation SET status = 'active', activated_at = NOW(),
                                 superseded_at = NULL
     WHERE id = p_generation_id AND status = 'verified';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'regular ingest verified generation changed';
    END IF;
    UPDATE logical_source SET active_generation_id = p_generation_id,
                              updated_at = NOW()
     WHERE id = p_source_id AND active_generation_id = p_expected_active_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'regular ingest pointer changed';
    END IF;
    SELECT encode(digest(convert_to(COALESCE(jsonb_agg(jsonb_build_object(
        'source_id', pointer.id,
        'active_generation_id', pointer.active_generation_id
    ) ORDER BY pointer.id), '[]'::JSONB)::TEXT, 'UTF8'), 'sha256'), 'hex')
      INTO v_after FROM logical_source pointer;
    IF v_after = v_before THEN
        RAISE EXCEPTION 'regular ingest did not advance the active pointer set';
    END IF;
    INSERT INTO storage_v2_active_ingest_receipt(
        activation_id, source_id, prior_generation_id, generation_id, run_id,
        source_watermark_sha256, verification_manifest_sha256,
        verification_proof_sha256, before_pointer_set_sha256,
        after_pointer_set_sha256, source_count, source_classification_sha256
    ) VALUES (
        v_activation.id, p_source_id, p_expected_active_id, p_generation_id,
        v_run.id, p_source_watermark_sha256, p_verification_manifest_sha256,
        p_verification_proof_sha256, v_before, v_after,
        v_activation.source_count, v_activation.source_classification_sha256
    ) RETURNING id INTO v_receipt_id;
    RETURN jsonb_build_object(
        'status', 'ACTIVE_INGEST_COMMITTED',
        'receipt_id', v_receipt_id,
        'source_id', p_source_id,
        'generation_id', p_generation_id,
        'pointer_set_sha256', v_after
    );
END
$$;

REVOKE INSERT, UPDATE, DELETE ON storage_v2_active_ingest_receipt FROM PUBLIC, mainrag;
REVOKE EXECUTE ON FUNCTION storage_v2_activate_regular_ingest(
    TEXT, BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_activate_regular_ingest(
    TEXT, BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT) TO mainrag;
REVOKE EXECUTE ON FUNCTION storage_v2_pointer_set_sha256() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION storage_v2_source_classification_sha256() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION storage_v2_require_complete_active_set(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_require_complete_active_set(TEXT) TO mainrag;

-- Activated sealed generations remain trusted prefix predecessors.
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
              AND generation.status IN ('verified', 'release_candidate', 'active')
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

REVOKE EXECUTE ON FUNCTION storage_v2_copy_managed_append_prefix(
    BIGINT, BIGINT, TEXT[], BYTEA[], BIGINT[]) FROM PUBLIC;
