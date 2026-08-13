-- Migration 032: generation-aware, feature-gated shadow ingest
--
-- This migration is additive. It cannot activate a generation and it does not
-- read, update, or delete legacy files, chunks, Qdrant, or outbox state.

CREATE TABLE IF NOT EXISTS storage_v2_ingest_run (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    generation_id BIGINT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    semantic_manifest_sha256 TEXT NOT NULL CHECK (semantic_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    adapter_profile_id TEXT NOT NULL CHECK (adapter_profile_id <> ''),
    status TEXT NOT NULL DEFAULT 'building'
        CHECK (status IN ('building', 'sealed', 'failed', 'cancelled')),
    forced BOOLEAN NOT NULL DEFAULT FALSE,
    expected_active_generation_id BIGINT,
    expected_item_count BIGINT CHECK (expected_item_count IS NULL OR expected_item_count >= 0),
    staged_item_count BIGINT NOT NULL DEFAULT 0 CHECK (staged_item_count >= 0),
    changed_item_count BIGINT NOT NULL DEFAULT 0 CHECK (changed_item_count >= 0),
    deleted_item_count BIGINT NOT NULL DEFAULT 0 CHECK (deleted_item_count >= 0),
    bytes_read BIGINT NOT NULL DEFAULT 0 CHECK (bytes_read >= 0),
    parser_work_count BIGINT NOT NULL DEFAULT 0 CHECK (parser_work_count >= 0),
    error_count BIGINT NOT NULL DEFAULT 0 CHECK (error_count >= 0),
    membership_delta_us BIGINT NOT NULL DEFAULT 0 CHECK (membership_delta_us >= 0),
    sealing_us BIGINT NOT NULL DEFAULT 0 CHECK (sealing_us >= 0),
    generation_root_sha256 TEXT CHECK (generation_root_sha256 IS NULL OR generation_root_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    UNIQUE (source_id, idempotency_key),
    UNIQUE (source_id, generation_id),
    FOREIGN KEY (source_id, generation_id)
        REFERENCES source_generation(source_id, id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_storage_v2_ingest_semantic_noop
    ON storage_v2_ingest_run(source_id, adapter_profile_id, semantic_manifest_sha256)
    WHERE status = 'sealed' AND NOT forced;

CREATE TABLE IF NOT EXISTS storage_v2_artifact_identity (
    source_id BIGINT NOT NULL,
    source_item_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    identity_sha256 BYTEA NOT NULL CHECK (octet_length(identity_sha256) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_id, source_item_id, identity_sha256),
    UNIQUE (artifact_version_id),
    FOREIGN KEY (source_item_id, source_id)
        REFERENCES source_item(id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY (artifact_version_id, source_item_id, source_id)
        REFERENCES artifact_version(id, item_id, source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS storage_v2_occurrence_identity (
    source_id BIGINT NOT NULL,
    occurrence_id BIGINT NOT NULL REFERENCES occurrence(id) ON DELETE RESTRICT,
    identity_sha256 BYTEA NOT NULL CHECK (octet_length(identity_sha256) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_id, identity_sha256),
    UNIQUE (occurrence_id)
);

CREATE TABLE IF NOT EXISTS storage_v2_analysis_cache (
    content_identity_sha256 BYTEA NOT NULL CHECK (octet_length(content_identity_sha256) = 32),
    analysis_profile_id TEXT NOT NULL CHECK (analysis_profile_id <> ''),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'complete', 'failed')),
    result JSONB,
    error_code TEXT,
    attempt_count BIGINT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (content_identity_sha256, analysis_profile_id),
    CHECK (
        (status = 'complete' AND result IS NOT NULL AND error_code IS NULL)
        OR (status = 'failed' AND result IS NULL AND error_code IS NOT NULL)
        OR (status = 'pending' AND result IS NULL AND error_code IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS storage_v2_ingest_run_item (
    run_id BIGINT NOT NULL REFERENCES storage_v2_ingest_run(id) ON DELETE RESTRICT,
    source_id BIGINT NOT NULL,
    source_item_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    occurrence_id BIGINT NOT NULL REFERENCES occurrence(id) ON DELETE RESTRICT,
    content_identity_sha256 BYTEA NOT NULL CHECK (octet_length(content_identity_sha256) = 32),
    analysis_profile_id TEXT NOT NULL CHECK (analysis_profile_id <> ''),
    byte_length BIGINT NOT NULL CHECK (byte_length >= 0),
    parser_pass_count SMALLINT NOT NULL CHECK (parser_pass_count IN (0, 1)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, source_item_id),
    FOREIGN KEY (source_item_id, source_id)
        REFERENCES source_item(id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY (artifact_version_id, source_item_id, source_id)
        REFERENCES artifact_version(id, item_id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY (content_identity_sha256, analysis_profile_id)
        REFERENCES storage_v2_analysis_cache(content_identity_sha256, analysis_profile_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_storage_v2_ingest_run_item_source
    ON storage_v2_ingest_run_item(source_id, source_item_id, run_id);

CREATE TABLE IF NOT EXISTS storage_v2_append_frontier (
    source_id BIGINT NOT NULL,
    source_item_id BIGINT NOT NULL,
    adapter_profile_id TEXT NOT NULL CHECK (adapter_profile_id <> ''),
    prefix_bytes BIGINT NOT NULL CHECK (prefix_bytes >= 0),
    prefix_sha256 BYTEA NOT NULL CHECK (octet_length(prefix_sha256) = 32),
    last_full_sha256 BYTEA NOT NULL CHECK (octet_length(last_full_sha256) = 32),
    appends_since_full BIGINT NOT NULL DEFAULT 0 CHECK (appends_since_full >= 0),
    full_compared_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_id, source_item_id, adapter_profile_id),
    FOREIGN KEY (source_item_id, source_id)
        REFERENCES source_item(id, source_id) ON DELETE RESTRICT
);

ALTER TABLE storage_v2_ingest_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_artifact_identity ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_occurrence_identity ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_analysis_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_ingest_run_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_append_frontier ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS storage_v2_ingest_run_isolation ON storage_v2_ingest_run;
CREATE POLICY storage_v2_ingest_run_isolation ON storage_v2_ingest_run
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_artifact_identity_isolation ON storage_v2_artifact_identity;
CREATE POLICY storage_v2_artifact_identity_isolation ON storage_v2_artifact_identity
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_occurrence_identity_isolation ON storage_v2_occurrence_identity;
CREATE POLICY storage_v2_occurrence_identity_isolation ON storage_v2_occurrence_identity
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_ingest_run_item_isolation ON storage_v2_ingest_run_item;
CREATE POLICY storage_v2_ingest_run_item_isolation ON storage_v2_ingest_run_item
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_append_frontier_isolation ON storage_v2_append_frontier;
CREATE POLICY storage_v2_append_frontier_isolation ON storage_v2_append_frontier
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_analysis_cache_admin ON storage_v2_analysis_cache;
CREATE POLICY storage_v2_analysis_cache_admin ON storage_v2_analysis_cache
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());

DO $$
DECLARE v_table TEXT;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'storage_v2_ingest_run', 'storage_v2_artifact_identity',
        'storage_v2_occurrence_identity', 'storage_v2_analysis_cache',
        'storage_v2_ingest_run_item', 'storage_v2_append_frontier'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', v_table || '_controlled', v_table);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_controlled_update()',
            v_table || '_controlled', v_table
        );
    END LOOP;
END
$$;

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
    -- A source has one ordered generation stream. Serialize allocation and
    -- semantic no-op detection so concurrent writers cannot create competing
    -- building generations from the same predecessor.
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
        SELECT * INTO v_run FROM storage_v2_ingest_run
         WHERE source_id = p_source_id
           AND adapter_profile_id = p_adapter_profile_id
           AND semantic_manifest_sha256 = p_semantic_manifest_sha256
           AND status = 'sealed' AND NOT forced;
        IF FOUND THEN RETURN v_run; END IF;
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

CREATE OR REPLACE FUNCTION storage_v2_begin_analysis_attempt(
    p_content_identity_sha256 BYTEA,
    p_analysis_profile_id TEXT
) RETURNS storage_v2_analysis_cache
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE v_cache storage_v2_analysis_cache;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'analysis cache writes require administrator authority' USING ERRCODE = '42501';
    END IF;
    IF octet_length(p_content_identity_sha256) <> 32
       OR p_analysis_profile_id IS NULL OR p_analysis_profile_id = '' THEN
        RAISE EXCEPTION 'analysis content identity and profile are required';
    END IF;
    SELECT * INTO v_cache FROM storage_v2_analysis_cache
     WHERE content_identity_sha256 = p_content_identity_sha256
       AND analysis_profile_id = p_analysis_profile_id
       AND status = 'complete';
    IF FOUND THEN RETURN v_cache; END IF;
    INSERT INTO storage_v2_analysis_cache(
        content_identity_sha256, analysis_profile_id, status, attempt_count
    ) VALUES (p_content_identity_sha256, p_analysis_profile_id, 'pending', 1)
    ON CONFLICT (content_identity_sha256, analysis_profile_id) DO UPDATE
       SET status = CASE WHEN storage_v2_analysis_cache.status = 'complete'
                         THEN 'complete' ELSE 'pending' END,
           result = CASE WHEN storage_v2_analysis_cache.status = 'complete'
                         THEN storage_v2_analysis_cache.result ELSE NULL END,
           error_code = NULL,
           attempt_count = storage_v2_analysis_cache.attempt_count + 1,
           updated_at = NOW()
    RETURNING * INTO v_cache;
    RETURN v_cache;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_finish_analysis_attempt(
    p_content_identity_sha256 BYTEA,
    p_analysis_profile_id TEXT,
    p_result JSONB DEFAULT NULL,
    p_error_code TEXT DEFAULT NULL
) RETURNS storage_v2_analysis_cache
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE v_cache storage_v2_analysis_cache;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'analysis cache writes require administrator authority' USING ERRCODE = '42501';
    END IF;
    IF (p_result IS NULL) = (p_error_code IS NULL) OR p_error_code = '' THEN
        RAISE EXCEPTION 'provide exactly one analysis result or error code';
    END IF;
    UPDATE storage_v2_analysis_cache
       SET status = CASE WHEN p_result IS NULL THEN 'failed' ELSE 'complete' END,
           result = p_result, error_code = p_error_code, updated_at = NOW()
     WHERE content_identity_sha256 = p_content_identity_sha256
       AND analysis_profile_id = p_analysis_profile_id
       AND status = 'pending'
     RETURNING * INTO v_cache;
    IF NOT FOUND THEN RAISE EXCEPTION 'pending analysis attempt not found'; END IF;
    RETURN v_cache;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_stage_shadow_item(
    p_run_id BIGINT,
    p_item_key TEXT,
    p_item_kind TEXT,
    p_witness_type TEXT,
    p_witness JSONB,
    p_adapter_profile_id TEXT,
    p_content_root_node_id BIGINT,
    p_raw_body_id BIGINT,
    p_expected_content_hash TEXT,
    p_byte_length BIGINT,
    p_content_identity_sha256 BYTEA,
    p_analysis_profile_id TEXT,
    p_view_id BIGINT,
    p_source_path TEXT,
    p_locator JSONB,
    p_parser_pass_count SMALLINT DEFAULT 1
) RETURNS storage_v2_ingest_run_item
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_run storage_v2_ingest_run;
    v_item source_item;
    v_artifact artifact_version;
    v_artifact_identity BYTEA;
    v_occurrence occurrence;
    v_occurrence_identity BYTEA;
    v_staged storage_v2_ingest_run_item;
    v_anchor_length BIGINT;
    v_anchor_digest BYTEA;
BEGIN
    SELECT * INTO v_run FROM storage_v2_ingest_run WHERE id = p_run_id FOR UPDATE;
    IF NOT FOUND OR v_run.status <> 'building'
       OR NOT storage_v2_can_access_source(v_run.source_id, 'write') THEN
        RAISE EXCEPTION 'building ingest run not found or access denied' USING ERRCODE = '42501';
    END IF;
    IF p_adapter_profile_id IS DISTINCT FROM v_run.adapter_profile_id
       OR p_item_key IS NULL OR p_item_key = '' OR p_item_kind IS NULL OR p_item_kind = ''
       OR p_byte_length < 0 OR p_expected_content_hash !~ '^[0-9a-f]{64}$'
       OR octet_length(p_content_identity_sha256) <> 32
       OR ((p_content_root_node_id IS NOT NULL)::INTEGER + (p_raw_body_id IS NOT NULL)::INTEGER) <> 1
       OR p_view_id IS NULL OR p_source_path IS NULL OR p_locator IS NULL
       OR p_witness_type IS NULL OR p_witness_type = '' OR p_witness IS NULL
       OR p_analysis_profile_id IS NULL OR p_analysis_profile_id = ''
       OR p_parser_pass_count NOT IN (0, 1) THEN
        RAISE EXCEPTION 'invalid staged item';
    END IF;
    IF decode(p_expected_content_hash, 'hex') <> p_content_identity_sha256 THEN
        RAISE EXCEPTION 'content identity does not match expected content hash';
    END IF;
    IF p_raw_body_id IS NOT NULL THEN
        SELECT logical_length, digest INTO v_anchor_length, v_anchor_digest
          FROM content_body WHERE id = p_raw_body_id;
        IF NOT FOUND OR v_anchor_length <> p_byte_length
           OR v_anchor_digest <> p_content_identity_sha256 THEN
            RAISE EXCEPTION 'raw body anchor does not match staged content';
        END IF;
    ELSE
        SELECT logical_length INTO v_anchor_length
          FROM content_node WHERE id = p_content_root_node_id;
        IF NOT FOUND OR v_anchor_length <> p_byte_length THEN
            RAISE EXCEPTION 'content root length does not match staged content';
        END IF;
    END IF;
    INSERT INTO source_item(source_id, item_key, item_kind)
        VALUES (v_run.source_id, p_item_key, p_item_kind)
        ON CONFLICT (source_id, item_kind, item_key) DO NOTHING;
    SELECT * INTO v_item FROM source_item
     WHERE source_id = v_run.source_id AND item_kind = p_item_kind AND item_key = p_item_key;

    v_artifact_identity := storage_v2_hash_parts('mainrag.artifact-version.v1', ARRAY[
        int8send(v_item.id), convert_to(p_witness_type, 'UTF8'), convert_to(p_witness::TEXT, 'UTF8'),
        convert_to(p_adapter_profile_id, 'UTF8'), int8send(COALESCE(p_content_root_node_id, -1)),
        int8send(COALESCE(p_raw_body_id, -1)), convert_to(p_expected_content_hash, 'UTF8'),
        int8send(p_byte_length)
    ]);
    SELECT artifact.* INTO v_artifact
      FROM storage_v2_artifact_identity identity_row
      JOIN artifact_version artifact ON artifact.id = identity_row.artifact_version_id
     WHERE identity_row.source_id = v_run.source_id
       AND identity_row.source_item_id = v_item.id
       AND identity_row.identity_sha256 = v_artifact_identity;
    IF NOT FOUND THEN
        INSERT INTO artifact_version(
            item_id, source_id, witness_type, witness, adapter_profile_id,
            content_root_node_id, raw_body_id, expected_content_hash, byte_length
        ) VALUES (
            v_item.id, v_run.source_id, p_witness_type, p_witness, p_adapter_profile_id,
            p_content_root_node_id, p_raw_body_id, p_expected_content_hash, p_byte_length
        ) RETURNING * INTO v_artifact;
        INSERT INTO storage_v2_artifact_identity(
            source_id, source_item_id, artifact_version_id, identity_sha256
        ) VALUES (v_run.source_id, v_item.id, v_artifact.id, v_artifact_identity);
    END IF;

    INSERT INTO storage_v2_analysis_cache(
        content_identity_sha256, analysis_profile_id, status, attempt_count
    ) VALUES (p_content_identity_sha256, p_analysis_profile_id, 'pending', 0)
    ON CONFLICT DO NOTHING;

    v_occurrence_identity := storage_v2_hash_parts('mainrag.occurrence.v1', ARRAY[
        int8send(v_run.source_id), int8send(v_artifact.id), int8send(p_view_id),
        convert_to(p_source_path, 'UTF8'), convert_to(p_locator::TEXT, 'UTF8')
    ]);
    SELECT occurrence_row.* INTO v_occurrence
      FROM storage_v2_occurrence_identity identity_row
      JOIN occurrence occurrence_row ON occurrence_row.id = identity_row.occurrence_id
     WHERE identity_row.source_id = v_run.source_id
       AND identity_row.identity_sha256 = v_occurrence_identity;
    IF NOT FOUND THEN
        INSERT INTO occurrence(
            source_id, artifact_version_id, view_id, role, ordinal,
            source_path, locator
        ) VALUES (
            v_run.source_id, v_artifact.id, p_view_id, 'artifact', 0,
            p_source_path, p_locator
        ) RETURNING * INTO v_occurrence;
        INSERT INTO storage_v2_occurrence_identity(source_id, occurrence_id, identity_sha256)
            VALUES (v_run.source_id, v_occurrence.id, v_occurrence_identity);
    END IF;

    INSERT INTO storage_v2_ingest_run_item(
        run_id, source_id, source_item_id, artifact_version_id, occurrence_id,
        content_identity_sha256, analysis_profile_id, byte_length, parser_pass_count
    ) VALUES (
        p_run_id, v_run.source_id, v_item.id, v_artifact.id, v_occurrence.id,
        p_content_identity_sha256, p_analysis_profile_id, p_byte_length, p_parser_pass_count
    ) ON CONFLICT (run_id, source_item_id) DO NOTHING
    RETURNING * INTO v_staged;
    IF NOT FOUND THEN
        SELECT * INTO v_staged FROM storage_v2_ingest_run_item
         WHERE run_id = p_run_id AND source_item_id = v_item.id;
        IF (v_staged.artifact_version_id, v_staged.occurrence_id)
           IS DISTINCT FROM (v_artifact.id, v_occurrence.id) THEN
            RAISE EXCEPTION 'run item identity collision' USING ERRCODE = '22000';
        END IF;
    END IF;
    RETURN v_staged;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_shadow_generation_root(
    p_run_id BIGINT
) RETURNS TEXT
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_source_id BIGINT;
    v_parts BYTEA[];
BEGIN
    SELECT source_id INTO v_source_id
      FROM storage_v2_ingest_run
     WHERE id = p_run_id;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'read') THEN
        RAISE EXCEPTION 'ingest run not found or access denied' USING ERRCODE = '42501';
    END IF;
    SELECT COALESCE(
        array_agg(
            storage_v2_hash_parts('mainrag.generation-item.v1', ARRAY[
                convert_to(source_item.item_kind, 'UTF8'),
                convert_to(source_item.item_key, 'UTF8'),
                artifact_identity.identity_sha256
            ])
            ORDER BY source_item.item_kind, source_item.item_key
        ),
        ARRAY[]::BYTEA[]
    ) INTO v_parts
      FROM storage_v2_ingest_run_item run_item
      JOIN source_item ON source_item.id = run_item.source_item_id
      JOIN storage_v2_artifact_identity artifact_identity
        ON artifact_identity.artifact_version_id = run_item.artifact_version_id
     WHERE run_item.run_id = p_run_id;
    RETURN encode(storage_v2_hash_parts('mainrag.generation-root.v1', v_parts), 'hex');
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_commit_shadow_ingest(
    p_run_id BIGINT,
    p_expected_item_count BIGINT,
    p_generation_root_sha256 TEXT
) RETURNS storage_v2_ingest_run
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_run storage_v2_ingest_run;
    v_generation source_generation;
    v_staged BIGINT;
    v_changed BIGINT;
    v_deleted BIGINT;
    v_visible BIGINT;
    v_membership_started TIMESTAMPTZ;
    v_sealing_started TIMESTAMPTZ;
    v_membership_delta_us BIGINT;
    v_sealing_us BIGINT;
    v_generation_root_sha256 TEXT;
BEGIN
    SELECT * INTO v_run FROM storage_v2_ingest_run WHERE id = p_run_id FOR UPDATE;
    IF NOT FOUND OR v_run.status <> 'building'
       OR NOT storage_v2_can_access_source(v_run.source_id, 'write') THEN
        RAISE EXCEPTION 'building ingest run not found or access denied' USING ERRCODE = '42501';
    END IF;
    IF p_expected_item_count < 0 OR p_generation_root_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'valid expected count and generation root are required';
    END IF;
    IF (SELECT active_generation_id FROM logical_source WHERE id = v_run.source_id)
       IS DISTINCT FROM v_run.expected_active_generation_id THEN
        RAISE EXCEPTION 'active pointer drift during shadow ingest';
    END IF;
    SELECT COUNT(*) INTO v_staged FROM storage_v2_ingest_run_item WHERE run_id = p_run_id;
    IF v_staged <> p_expected_item_count THEN
        RAISE EXCEPTION 'staged item count % does not match expected %', v_staged, p_expected_item_count;
    END IF;
    v_generation_root_sha256 := storage_v2_shadow_generation_root(p_run_id);
    IF p_generation_root_sha256 <> v_generation_root_sha256 THEN
        RAISE EXCEPTION 'generation root does not match staged items';
    END IF;
    IF EXISTS (
        SELECT 1 FROM storage_v2_ingest_run_item item
        JOIN storage_v2_analysis_cache analysis
          ON analysis.content_identity_sha256 = item.content_identity_sha256
         AND analysis.analysis_profile_id = item.analysis_profile_id
        WHERE item.run_id = p_run_id AND analysis.status <> 'complete'
    ) THEN
        RAISE EXCEPTION 'all staged analysis must be complete before sealing';
    END IF;

    v_membership_started := clock_timestamp();
    SELECT COUNT(*) INTO v_deleted
      FROM generation_item_version membership
     WHERE membership.source_id = v_run.source_id AND membership.valid_to_seq IS NULL
       AND NOT EXISTS (
           SELECT 1 FROM storage_v2_ingest_run_item item
            WHERE item.run_id = p_run_id AND item.source_item_id = membership.source_item_id
       );
    SELECT COUNT(*) INTO v_changed
      FROM storage_v2_ingest_run_item item
      LEFT JOIN generation_item_version membership
        ON membership.source_id = item.source_id
       AND membership.source_item_id = item.source_item_id
       AND membership.valid_to_seq IS NULL
     WHERE item.run_id = p_run_id
       AND membership.artifact_version_id IS DISTINCT FROM item.artifact_version_id;

    UPDATE generation_item_version membership
       SET valid_to_seq = generation.generation_seq
      FROM source_generation generation
     WHERE generation.id = v_run.generation_id
       AND membership.source_id = v_run.source_id
       AND membership.valid_to_seq IS NULL
       AND NOT EXISTS (
           SELECT 1 FROM storage_v2_ingest_run_item item
            WHERE item.run_id = p_run_id
              AND item.source_item_id = membership.source_item_id
              AND item.artifact_version_id = membership.artifact_version_id
       );

    INSERT INTO generation_item_version(
        source_id, source_item_id, artifact_version_id, valid_from_seq
    )
    SELECT item.source_id, item.source_item_id, item.artifact_version_id, generation.generation_seq
      FROM storage_v2_ingest_run_item item
      JOIN source_generation generation ON generation.id = v_run.generation_id
      LEFT JOIN generation_item_version membership
        ON membership.source_id = item.source_id
       AND membership.source_item_id = item.source_item_id
       AND membership.valid_to_seq IS NULL
     WHERE item.run_id = p_run_id AND membership.source_item_id IS NULL;

    SELECT COUNT(*) INTO v_visible
      FROM generation_item_version membership
      JOIN source_generation generation ON generation.id = v_run.generation_id
     WHERE membership.source_id = v_run.source_id
       AND membership.valid_from_seq <= generation.generation_seq
       AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > generation.generation_seq);
    IF v_visible <> p_expected_item_count THEN
        RAISE EXCEPTION 'visible membership count % does not match expected %', v_visible, p_expected_item_count;
    END IF;
    v_membership_delta_us := GREATEST(
        0,
        (EXTRACT(EPOCH FROM clock_timestamp() - v_membership_started) * 1000000)::BIGINT
    );
    v_sealing_started := clock_timestamp();
    v_generation := storage_v2_seal_generation(v_run.generation_id, p_expected_item_count);
    v_sealing_us := GREATEST(
        0,
        (EXTRACT(EPOCH FROM clock_timestamp() - v_sealing_started) * 1000000)::BIGINT
    );
    UPDATE storage_v2_ingest_run
       SET status = 'sealed', expected_item_count = p_expected_item_count,
           staged_item_count = v_staged, changed_item_count = v_changed,
           deleted_item_count = v_deleted,
           bytes_read = COALESCE((SELECT SUM(byte_length) FROM storage_v2_ingest_run_item WHERE run_id = p_run_id), 0),
           parser_work_count = COALESCE((SELECT SUM(parser_pass_count) FROM storage_v2_ingest_run_item WHERE run_id = p_run_id), 0),
           error_count = 0, membership_delta_us = v_membership_delta_us,
           sealing_us = v_sealing_us, generation_root_sha256 = v_generation_root_sha256,
           finished_at = NOW()
     WHERE id = p_run_id RETURNING * INTO v_run;
    RETURN v_run;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_cancel_shadow_ingest(
    p_run_id BIGINT,
    p_error_count BIGINT DEFAULT 0
) RETURNS storage_v2_ingest_run
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE v_run storage_v2_ingest_run;
BEGIN
    SELECT * INTO v_run FROM storage_v2_ingest_run WHERE id = p_run_id FOR UPDATE;
    IF NOT FOUND OR v_run.status <> 'building'
       OR NOT storage_v2_can_access_source(v_run.source_id, 'write') OR p_error_count < 0 THEN
        RAISE EXCEPTION 'building ingest run not found or access denied' USING ERRCODE = '42501';
    END IF;
    UPDATE storage_v2_ingest_run
       SET status = 'cancelled', error_count = p_error_count, finished_at = NOW()
     WHERE id = p_run_id RETURNING * INTO v_run;
    RETURN v_run;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_update_append_frontier(
    p_source_id BIGINT,
    p_source_item_id BIGINT,
    p_adapter_profile_id TEXT,
    p_expected_prefix_bytes BIGINT,
    p_expected_prefix_sha256 BYTEA,
    p_new_prefix_bytes BIGINT,
    p_new_prefix_sha256 BYTEA,
    p_full_sha256 BYTEA DEFAULT NULL,
    p_full_compare_every BIGINT DEFAULT 32
) RETURNS storage_v2_append_frontier
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE v_frontier storage_v2_append_frontier;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'write')
       OR p_source_item_id IS NULL
       OR p_adapter_profile_id IS NULL OR p_adapter_profile_id = ''
       OR p_expected_prefix_bytes < 0
       OR p_full_compare_every < 1 OR p_full_compare_every > 10000
       OR p_new_prefix_bytes < p_expected_prefix_bytes
       OR (p_expected_prefix_sha256 IS NOT NULL AND octet_length(p_expected_prefix_sha256) <> 32)
       OR octet_length(p_new_prefix_sha256) <> 32
       OR (p_full_sha256 IS NOT NULL AND (
           octet_length(p_full_sha256) <> 32 OR p_full_sha256 <> p_new_prefix_sha256
       )) THEN
        RAISE EXCEPTION 'invalid or unauthorized append frontier' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_frontier FROM storage_v2_append_frontier
     WHERE source_id = p_source_id AND source_item_id = p_source_item_id
       AND adapter_profile_id = p_adapter_profile_id FOR UPDATE;
    IF FOUND AND (v_frontier.prefix_bytes, v_frontier.prefix_sha256)
       IS DISTINCT FROM (p_expected_prefix_bytes, p_expected_prefix_sha256) THEN
        RAISE EXCEPTION 'append frontier drift';
    ELSIF NOT FOUND AND (p_expected_prefix_bytes <> 0 OR p_expected_prefix_sha256 IS NOT NULL) THEN
        RAISE EXCEPTION 'append frontier does not exist';
    END IF;
    IF p_full_sha256 IS NULL
       AND COALESCE(v_frontier.appends_since_full, 0) + 1 >= p_full_compare_every THEN
        RAISE EXCEPTION 'scheduled full comparison required before advancing frontier';
    END IF;
    INSERT INTO storage_v2_append_frontier(
        source_id, source_item_id, adapter_profile_id, prefix_bytes,
        prefix_sha256, last_full_sha256, appends_since_full, full_compared_at
    ) VALUES (
        p_source_id, p_source_item_id, p_adapter_profile_id, p_new_prefix_bytes,
        p_new_prefix_sha256, COALESCE(p_full_sha256, p_new_prefix_sha256),
        CASE WHEN p_full_sha256 IS NULL THEN 1 ELSE 0 END,
        CASE WHEN p_full_sha256 IS NULL THEN '-infinity'::TIMESTAMPTZ ELSE NOW() END
    ) ON CONFLICT (source_id, source_item_id, adapter_profile_id) DO UPDATE
       SET prefix_bytes = EXCLUDED.prefix_bytes,
           prefix_sha256 = EXCLUDED.prefix_sha256,
           last_full_sha256 = COALESCE(p_full_sha256, storage_v2_append_frontier.last_full_sha256),
           appends_since_full = CASE WHEN p_full_sha256 IS NULL
               THEN storage_v2_append_frontier.appends_since_full + 1 ELSE 0 END,
           full_compared_at = CASE WHEN p_full_sha256 IS NULL
               THEN storage_v2_append_frontier.full_compared_at ELSE NOW() END,
           updated_at = NOW()
    RETURNING * INTO v_frontier;
    RETURN v_frontier;
END
$$;

REVOKE INSERT, UPDATE, DELETE ON storage_v2_ingest_run FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_artifact_identity FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_occurrence_identity FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_analysis_cache FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_ingest_run_item FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_append_frontier FROM PUBLIC;
