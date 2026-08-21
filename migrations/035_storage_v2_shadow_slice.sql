-- Migration 035: explicit test-source scope and auditable dual-read evidence
--
-- Test sources are fail-closed. Ordinary callers cannot opt into them by
-- merely naming the source; the dedicated shadow harness must assert the
-- explicit scope. No active generation pointer is read or changed here.

ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE source_generation
    ADD COLUMN IF NOT EXISTS abandoned_at TIMESTAMPTZ;

ALTER TABLE source_generation
    DROP CONSTRAINT IF EXISTS source_generation_abandoned_building_only;
ALTER TABLE source_generation
    ADD CONSTRAINT source_generation_abandoned_building_only
    CHECK (abandoned_at IS NULL OR status = 'building');

CREATE INDEX IF NOT EXISTS idx_sources_test_scope
    ON sources (is_test, id);

-- A semantic no-op is only valid against the newest sealed snapshot. A source
-- that transitions A -> B -> A must allocate a third generation so its
-- membership intervals preserve time; an older A generation is not a no-op.
DROP INDEX IF EXISTS uq_storage_v2_ingest_semantic_noop;
CREATE INDEX IF NOT EXISTS idx_storage_v2_ingest_semantic_lookup
    ON storage_v2_ingest_run(
        source_id, adapter_profile_id, semantic_manifest_sha256, generation_id
    ) WHERE status = 'sealed';

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
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'write') THEN
        RAISE EXCEPTION 'source write access denied' USING ERRCODE = '42501';
    END IF;
    IF p_idempotency_key !~ '^[0-9a-f]{64}$'
       OR p_semantic_manifest_sha256 !~ '^[0-9a-f]{64}$'
       OR p_adapter_profile_id IS NULL OR p_adapter_profile_id = '' THEN
        RAISE EXCEPTION 'valid ingest identity and adapter profile are required';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('mainrag.storage-v2-ingest-source:' || p_source_id::TEXT, 0)
    );
    SELECT * INTO v_run FROM storage_v2_ingest_run
     WHERE source_id = p_source_id AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF (v_run.semantic_manifest_sha256, v_run.adapter_profile_id, v_run.forced)
           IS DISTINCT FROM (p_semantic_manifest_sha256, p_adapter_profile_id, p_force) THEN
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
        IF FOUND
           AND v_run.adapter_profile_id = p_adapter_profile_id
           AND v_run.semantic_manifest_sha256 = p_semantic_manifest_sha256 THEN
            RETURN v_run;
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

CREATE OR REPLACE FUNCTION storage_v2_put_packed_body(
    p_pack_id UUID,
    p_ordinal BIGINT,
    p_digest BYTEA,
    p_logical_length BIGINT,
    p_pack_offset BIGINT,
    p_stored_length BIGINT,
    p_codec storage_v2_body_codec,
    p_entry_digest BYTEA
) RETURNS content_body
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE v_body content_body;
BEGIN
    IF NOT storage_v2_is_admin()
       OR octet_length(p_digest) <> 32
       OR octet_length(p_entry_digest) <> 32
       OR p_logical_length < 0 OR p_ordinal < 0
       OR p_pack_offset < 0 OR p_stored_length <= 0
       OR NOT EXISTS (
           SELECT 1 FROM content_pack WHERE id = p_pack_id AND status = 'candidate'
       ) THEN
        RAISE EXCEPTION 'valid candidate pack entry and administrator authority required'
            USING ERRCODE = '42501';
    END IF;
    INSERT INTO content_body(
        digest_algorithm, digest, logical_length, pack_id
    ) VALUES ('sha256-v1', p_digest, p_logical_length, p_pack_id)
    RETURNING * INTO v_body;
    INSERT INTO content_pack_entry(
        pack_id, ordinal, body_id, pack_offset, stored_length, codec, entry_digest
    ) VALUES (
        p_pack_id, p_ordinal, v_body.id, p_pack_offset,
        p_stored_length, p_codec, p_entry_digest
    );
    RETURN v_body;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_cleanup_abandoned_shadow_ingest(
    p_run_id BIGINT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_run storage_v2_ingest_run;
    v_generation source_generation;
    v_active_generation_id BIGINT;
    v_staged_items BIGINT;
BEGIN
    SELECT * INTO v_run FROM storage_v2_ingest_run WHERE id = p_run_id FOR UPDATE;
    IF NOT FOUND OR NOT storage_v2_is_admin()
       OR NOT EXISTS (SELECT 1 FROM sources WHERE id = v_run.source_id AND is_test)
       OR v_run.status NOT IN ('building', 'cancelled', 'failed') THEN
        RAISE EXCEPTION 'abandoned cleanup requires an admin-owned test run'
            USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_generation FROM source_generation
     WHERE id = v_run.generation_id FOR UPDATE;
    SELECT active_generation_id INTO v_active_generation_id
      FROM logical_source WHERE id = v_run.source_id FOR UPDATE;
    IF v_generation.status <> 'building' OR v_generation.abandoned_at IS NOT NULL
       OR v_active_generation_id IS NOT DISTINCT FROM v_generation.id
       OR EXISTS (
           SELECT 1 FROM generation_item_version
            WHERE source_id = v_run.source_id
              AND valid_from_seq = v_generation.generation_seq
       ) THEN
        RAISE EXCEPTION 'cleanup cannot touch visible, sealed, verified, or active state';
    END IF;
    IF v_run.status = 'building' THEN
        UPDATE storage_v2_ingest_run
           SET status = 'cancelled', finished_at = NOW()
         WHERE id = p_run_id;
    END IF;
    UPDATE source_generation
       SET abandoned_at = NOW()
     WHERE id = v_generation.id;
    SELECT COUNT(*) INTO v_staged_items
      FROM storage_v2_ingest_run_item WHERE run_id = p_run_id;
    RETURN jsonb_build_object(
        'run_id', p_run_id,
        'generation_id', v_generation.id,
        'generation_seq', v_generation.generation_seq,
        'generation_status', 'abandoned',
        'staged_audit_tombstone_count', v_staged_items,
        'visible_membership_count', 0,
        'active_generation_id', v_active_generation_id
    );
END
$$;

CREATE TABLE IF NOT EXISTS storage_v2_dual_read_evidence (
    id UUID PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    generation_id BIGINT NOT NULL REFERENCES source_generation(id) ON DELETE RESTRICT,
    commit_sha TEXT NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    fixture_sha256 TEXT NOT NULL CHECK (fixture_sha256 ~ '^[0-9a-f]{64}$'),
    query_set_sha256 TEXT NOT NULL CHECK (query_set_sha256 ~ '^[0-9a-f]{64}$'),
    artifact JSONB NOT NULL,
    artifact_sha256 BYTEA NOT NULL CHECK (octet_length(artifact_sha256) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (jsonb_typeof(artifact) = 'object'),
    UNIQUE (source_id, generation_id, commit_sha, fixture_sha256, query_set_sha256)
);

ALTER TABLE storage_v2_dual_read_evidence ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS storage_v2_dual_read_evidence_isolation
    ON storage_v2_dual_read_evidence;
CREATE POLICY storage_v2_dual_read_evidence_isolation
    ON storage_v2_dual_read_evidence
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));

CREATE OR REPLACE FUNCTION storage_v2_require_test_scope(
    p_source_id BIGINT,
    p_include_test BOOLEAN
) RETURNS VOID
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE v_is_test BOOLEAN;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'read') THEN
        RAISE EXCEPTION 'source access denied' USING ERRCODE = '42501';
    END IF;
    SELECT is_test INTO v_is_test FROM sources WHERE id = p_source_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'source not found'; END IF;
    IF v_is_test AND NOT COALESCE(p_include_test, FALSE) THEN
        RAISE EXCEPTION 'test source requires explicit test scope'
            USING ERRCODE = '42501';
    END IF;
    IF COALESCE(p_include_test, FALSE) AND NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'test scope requires administrator authority'
            USING ERRCODE = '42501';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_record_dual_read_evidence(
    p_id UUID,
    p_source_id BIGINT,
    p_generation_id BIGINT,
    p_commit_sha TEXT,
    p_fixture_sha256 TEXT,
    p_query_set_sha256 TEXT,
    p_artifact JSONB
) RETURNS storage_v2_dual_read_evidence
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_hash BYTEA;
    v_evidence storage_v2_dual_read_evidence;
BEGIN
    PERFORM storage_v2_require_test_scope(p_source_id, TRUE);
    IF p_id IS NULL OR p_commit_sha !~ '^[0-9a-f]{40}$'
       OR p_fixture_sha256 !~ '^[0-9a-f]{64}$'
       OR p_query_set_sha256 !~ '^[0-9a-f]{64}$'
       OR p_artifact IS NULL OR jsonb_typeof(p_artifact) <> 'object'
       OR COALESCE(p_artifact ->> 'status', '') NOT IN ('PASS', 'FAIL')
       OR NOT EXISTS (
           SELECT 1 FROM source_generation
            WHERE id = p_generation_id AND source_id = p_source_id
              AND status = 'verified'
       ) THEN
        RAISE EXCEPTION 'valid verified dual-read evidence required';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(COALESCE(p_artifact -> 'comparisons', '[]'::JSONB)) item
         WHERE COALESCE(item ->> 'classification', '') NOT IN (
             'identity_mapping', 'segmentation', 'score_order',
             'missing_current', 'missing_storage_v2', 'authorization'
         )
    ) OR COALESCE((p_artifact ->> 'unexplained_count')::BIGINT, -1) <> 0 THEN
        RAISE EXCEPTION 'dual-read artifact contains unexplained differences';
    END IF;
    v_hash := digest(convert_to(p_artifact::TEXT, 'UTF8'), 'sha256');
    INSERT INTO storage_v2_dual_read_evidence(
        id, source_id, generation_id, commit_sha, fixture_sha256,
        query_set_sha256, artifact, artifact_sha256
    ) VALUES (
        p_id, p_source_id, p_generation_id, p_commit_sha, p_fixture_sha256,
        p_query_set_sha256, p_artifact, v_hash
    ) ON CONFLICT (source_id, generation_id, commit_sha, fixture_sha256, query_set_sha256)
      DO NOTHING
    RETURNING * INTO v_evidence;
    IF NOT FOUND THEN
        SELECT * INTO STRICT v_evidence FROM storage_v2_dual_read_evidence
         WHERE source_id = p_source_id AND generation_id = p_generation_id
           AND commit_sha = p_commit_sha AND fixture_sha256 = p_fixture_sha256
           AND query_set_sha256 = p_query_set_sha256;
        IF v_evidence.artifact_sha256 <> v_hash THEN
            RAISE EXCEPTION 'dual-read evidence identity collision' USING ERRCODE = '22000';
        END IF;
    END IF;
    RETURN v_evidence;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_shadow_source_state(
    p_source_id BIGINT,
    p_generation_selector TEXT,
    p_include_test BOOLEAN DEFAULT FALSE
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
    v_active_generation_id BIGINT;
    v_result JSONB;
BEGIN
    PERFORM storage_v2_require_test_scope(p_source_id, p_include_test);
    v_generation := storage_v2_resolve_generation(p_source_id, p_generation_selector);
    SELECT active_generation_id INTO v_active_generation_id
      FROM logical_source WHERE id = p_source_id;
    WITH visible_membership AS (
        SELECT membership.source_item_id, membership.artifact_version_id
          FROM generation_item_version membership
         WHERE membership.source_id = p_source_id
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > v_generation.generation_seq)
    ), visible_run_item AS (
        SELECT item.source_item_id, item.artifact_version_id,
               item.occurrence_id, item.content_identity_sha256,
               item.analysis_profile_id
          FROM storage_v2_ingest_run run
          JOIN storage_v2_ingest_run_item item ON item.run_id = run.id
         WHERE run.source_id = p_source_id
           AND run.generation_id = v_generation.id
    ), visible_occurrence AS (
        SELECT occurrence_row.id, occurrence_row.view_id
          FROM visible_membership membership
          JOIN visible_run_item item
            ON item.source_item_id = membership.source_item_id
           AND item.artifact_version_id = membership.artifact_version_id
          JOIN occurrence occurrence_row ON occurrence_row.id = item.occurrence_id
         WHERE occurrence_row.source_id = p_source_id
    )
    SELECT jsonb_build_object(
        'source_id', p_source_id,
        'generation_id', v_generation.id,
        'generation_seq', v_generation.generation_seq,
        'status', v_generation.status,
        'declared_item_count', v_generation.item_count,
        'is_active', v_active_generation_id IS NOT DISTINCT FROM v_generation.id,
        'active_generation_id', v_active_generation_id,
        'item_count', (SELECT COUNT(*) FROM visible_membership),
        'occurrence_count', (SELECT COUNT(*) FROM visible_occurrence),
        'view_count', (SELECT COUNT(DISTINCT view_id) FROM visible_occurrence),
        'search_document_count', (
            SELECT COUNT(DISTINCT binding.document_id)
              FROM visible_occurrence occurrence_row
              JOIN storage_v2_search_view_document binding
                ON binding.view_id = occurrence_row.view_id
        ),
        'packed_body_count', (
            SELECT COUNT(DISTINCT body.id)
              FROM visible_membership membership
              JOIN artifact_version artifact ON artifact.id=membership.artifact_version_id
              JOIN content_node node ON node.id=artifact.content_root_node_id
              JOIN content_body body ON body.id=node.body_id
             WHERE body.pack_id IS NOT NULL
        ),
        'published_pack_count', (
            SELECT COUNT(DISTINCT pack.id)
              FROM visible_membership membership
              JOIN artifact_version artifact ON artifact.id=membership.artifact_version_id
              JOIN content_node node ON node.id=artifact.content_root_node_id
              JOIN content_body body ON body.id=node.body_id
              JOIN content_pack pack ON pack.id=body.pack_id
             WHERE pack.status='published'
        ),
        'symbol_count', (
            SELECT COUNT(*) FROM storage_v2_symbol_occurrence symbol_occurrence
             JOIN visible_occurrence occurrence_row
               ON occurrence_row.id = symbol_occurrence.occurrence_id
        ),
        'analysis_incomplete_count', (
            SELECT COUNT(*) FROM visible_membership membership
             WHERE NOT EXISTS (
                SELECT 1 FROM visible_run_item item
                  JOIN storage_v2_analysis_cache analysis
                    ON analysis.content_identity_sha256 = item.content_identity_sha256
                   AND analysis.analysis_profile_id = item.analysis_profile_id
                 WHERE item.source_item_id = membership.source_item_id
                   AND item.artifact_version_id = membership.artifact_version_id
                   AND analysis.status = 'complete'
             )
        ),
        'source_watermark_sha256', (
            SELECT semantic_manifest_sha256 FROM storage_v2_ingest_run
             WHERE generation_id=v_generation.id AND source_id=p_source_id
        ),
        'verification_manifest_sha256', v_generation.verification_manifest_sha256
    ) INTO v_result;
    RETURN v_result;
END
$$;
