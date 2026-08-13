-- Migration 029: additive storage-v2 generation and activation invariants
--
-- This migration creates no storage-v2 content, changes no current read/write
-- path, and moves no active pointer. Production application is a separate gate.

CREATE EXTENSION IF NOT EXISTS btree_gist;

DO $$
BEGIN
    CREATE TYPE storage_v2_generation_status AS ENUM (
        'building',
        'sealed',
        'verified',
        'release_candidate',
        'active',
        'superseded'
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

CREATE TABLE IF NOT EXISTS logical_source (
    id BIGINT PRIMARY KEY REFERENCES sources(id) ON DELETE RESTRICT,
    active_generation_id BIGINT,
    next_generation_seq BIGINT NOT NULL DEFAULT 1 CHECK (next_generation_seq > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, active_generation_id)
);

CREATE TABLE IF NOT EXISTS source_generation (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    generation_seq BIGINT NOT NULL CHECK (generation_seq > 0),
    status storage_v2_generation_status NOT NULL DEFAULT 'building',
    witness_type TEXT NOT NULL,
    witness JSONB NOT NULL,
    verification_manifest_sha256 TEXT,
    item_count BIGINT NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sealed_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    superseded_at TIMESTAMPTZ,
    UNIQUE (source_id, generation_seq),
    UNIQUE (source_id, id),
    CHECK (
        verification_manifest_sha256 IS NULL
        OR verification_manifest_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CHECK (status = 'building' OR sealed_at IS NOT NULL),
    CHECK (status NOT IN ('verified', 'release_candidate', 'active', 'superseded') OR verified_at IS NOT NULL),
    CHECK (status <> 'active' OR activated_at IS NOT NULL),
    CHECK (status <> 'superseded' OR superseded_at IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_generation_one_active
    ON source_generation (source_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_source_generation_source_status
    ON source_generation (source_id, status, generation_seq);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'logical_source_active_generation_fkey'
           AND conrelid = 'public.logical_source'::REGCLASS
    ) THEN
        ALTER TABLE logical_source
            ADD CONSTRAINT logical_source_active_generation_fkey
            FOREIGN KEY (id, active_generation_id)
            REFERENCES source_generation(source_id, id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS source_item (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    item_key TEXT NOT NULL CHECK (item_key <> ''),
    item_kind TEXT NOT NULL CHECK (item_kind <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, source_id),
    UNIQUE (source_id, item_kind, item_key)
);

CREATE TABLE IF NOT EXISTS artifact_version (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    item_id BIGINT NOT NULL,
    source_id BIGINT NOT NULL,
    witness_type TEXT NOT NULL,
    witness JSONB NOT NULL,
    adapter_profile_id TEXT NOT NULL,
    content_root_node_id BIGINT,
    raw_body_id BIGINT,
    expected_content_hash TEXT NOT NULL CHECK (expected_content_hash <> ''),
    byte_length BIGINT NOT NULL CHECK (byte_length >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, item_id),
    UNIQUE (id, item_id, source_id),
    FOREIGN KEY (item_id, source_id)
        REFERENCES source_item(id, source_id) ON DELETE RESTRICT,
    CHECK ((content_root_node_id IS NOT NULL)::INTEGER + (raw_body_id IS NOT NULL)::INTEGER = 1)
);

COMMENT ON COLUMN artifact_version.content_root_node_id IS
    'Structured content anchor. The content-node FK is added by the graph migration.';
COMMENT ON COLUMN artifact_version.raw_body_id IS
    'Unstructured content anchor. The content-body FK is added by the body migration.';

CREATE TABLE IF NOT EXISTS generation_item_version (
    source_id BIGINT NOT NULL,
    source_item_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    valid_from_seq BIGINT NOT NULL,
    valid_to_seq BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_id, source_item_id, valid_from_seq),
    FOREIGN KEY (source_item_id, source_id)
        REFERENCES source_item(id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY (artifact_version_id, source_item_id)
        REFERENCES artifact_version(id, item_id) ON DELETE RESTRICT,
    FOREIGN KEY (artifact_version_id, source_item_id, source_id)
        REFERENCES artifact_version(id, item_id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY (source_id, valid_from_seq)
        REFERENCES source_generation(source_id, generation_seq) ON DELETE RESTRICT,
    FOREIGN KEY (source_id, valid_to_seq)
        REFERENCES source_generation(source_id, generation_seq) ON DELETE RESTRICT,
    CHECK (valid_to_seq IS NULL OR valid_to_seq > valid_from_seq),
    EXCLUDE USING gist (
        source_id WITH =,
        source_item_id WITH =,
        int8range(valid_from_seq, valid_to_seq, '[)') WITH &&
    )
);

CREATE INDEX IF NOT EXISTS idx_generation_item_version_visible
    ON generation_item_version (source_id, valid_from_seq, valid_to_seq, source_item_id);

CREATE TABLE IF NOT EXISTS storage_v2_gc_epoch (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('planned', 'marking', 'verified', 'sweeping', 'complete', 'failed')),
    root_manifest_sha256 TEXT NOT NULL CHECK (root_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    code_sha TEXT NOT NULL CHECK (code_sha ~ '^[0-9a-f]{40}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_storage_v2_gc_epoch_source_status
    ON storage_v2_gc_epoch (source_id, status, created_at);

-- Fail closed when the current security bootstrap is not present. Dynamic SQL
-- keeps this additive migration usable in the historical schema bootstrap while
-- preserving the repository's authoritative user_can_access_source contract.
CREATE OR REPLACE FUNCTION storage_v2_can_access_source(
    p_source_id BIGINT,
    p_action TEXT DEFAULT 'read'
) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_user_id UUID;
    v_allowed BOOLEAN := FALSE;
BEGIN
    v_user_id := NULLIF(current_setting('app.user_id', TRUE), '')::UUID;
    IF v_user_id IS NULL
       OR to_regprocedure('user_can_access_source(uuid,bigint,text)') IS NULL THEN
        RETURN FALSE;
    END IF;
    EXECUTE 'SELECT user_can_access_source($1, $2, $3)'
        INTO v_allowed USING v_user_id, p_source_id, p_action;
    RETURN COALESCE(v_allowed, FALSE);
EXCEPTION
    WHEN invalid_text_representation THEN RETURN FALSE;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_is_admin() RETURNS BOOLEAN
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_user_id UUID;
    v_admin BOOLEAN := FALSE;
BEGIN
    v_user_id := NULLIF(current_setting('app.user_id', TRUE), '')::UUID;
    IF v_user_id IS NULL OR to_regclass('public.users') IS NULL THEN
        RETURN FALSE;
    END IF;
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM users WHERE id = $1 AND is_admin)'
        INTO v_admin USING v_user_id;
    RETURN COALESCE(v_admin, FALSE);
EXCEPTION
    WHEN invalid_text_representation THEN RETURN FALSE;
END
$$;

ALTER TABLE logical_source ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_generation ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifact_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE generation_item_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_gc_epoch ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS logical_source_isolation ON logical_source;
CREATE POLICY logical_source_isolation ON logical_source
    USING (storage_v2_can_access_source(id, 'read'))
    WITH CHECK (storage_v2_can_access_source(id, 'write'));

DROP POLICY IF EXISTS source_generation_isolation ON source_generation;
CREATE POLICY source_generation_isolation ON source_generation
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));

DROP POLICY IF EXISTS source_item_isolation ON source_item;
CREATE POLICY source_item_isolation ON source_item
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));

DROP POLICY IF EXISTS artifact_version_isolation ON artifact_version;
CREATE POLICY artifact_version_isolation ON artifact_version
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));

DROP POLICY IF EXISTS generation_item_version_isolation ON generation_item_version;
CREATE POLICY generation_item_version_isolation ON generation_item_version
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));

DROP POLICY IF EXISTS storage_v2_gc_epoch_isolation ON storage_v2_gc_epoch;
CREATE POLICY storage_v2_gc_epoch_isolation ON storage_v2_gc_epoch
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));

CREATE OR REPLACE FUNCTION storage_v2_reject_artifact_mutation() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    RAISE EXCEPTION 'artifact_version rows are immutable'
        USING ERRCODE = '55000';
END
$$;

DROP TRIGGER IF EXISTS artifact_version_immutable ON artifact_version;
CREATE TRIGGER artifact_version_immutable
    BEFORE UPDATE OR DELETE ON artifact_version
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_artifact_mutation();

CREATE OR REPLACE FUNCTION storage_v2_guard_controlled_update() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_owner OID;
BEGIN
    SELECT relowner INTO v_owner FROM pg_class WHERE oid = TG_RELID;
    IF current_user::REGROLE::OID <> v_owner THEN
        RAISE EXCEPTION 'storage-v2 state changes require a controlled function'
            USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS logical_source_controlled_update ON logical_source;
CREATE TRIGGER logical_source_controlled_update
    BEFORE UPDATE ON logical_source
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_controlled_update();

DROP TRIGGER IF EXISTS logical_source_controlled_insert ON logical_source;
CREATE TRIGGER logical_source_controlled_insert
    BEFORE INSERT ON logical_source
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_controlled_update();

DROP TRIGGER IF EXISTS logical_source_controlled_delete ON logical_source;
CREATE TRIGGER logical_source_controlled_delete
    BEFORE DELETE ON logical_source
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_controlled_update();

DROP TRIGGER IF EXISTS source_generation_controlled_update ON source_generation;
CREATE TRIGGER source_generation_controlled_update
    BEFORE UPDATE ON source_generation
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_controlled_update();

DROP TRIGGER IF EXISTS source_generation_controlled_insert ON source_generation;
CREATE TRIGGER source_generation_controlled_insert
    BEFORE INSERT ON source_generation
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_controlled_update();

DROP TRIGGER IF EXISTS generation_item_version_controlled_update ON generation_item_version;
CREATE TRIGGER generation_item_version_controlled_update
    BEFORE UPDATE ON generation_item_version
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_controlled_update();

CREATE OR REPLACE FUNCTION storage_v2_reject_sealed_generation_mutation() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'source generation rows cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF (NEW.source_id, NEW.generation_seq) IS DISTINCT FROM
       (OLD.source_id, OLD.generation_seq) THEN
        RAISE EXCEPTION 'source generation identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status <> 'building'
       AND (NEW.witness_type, NEW.witness, NEW.item_count) IS DISTINCT FROM
           (OLD.witness_type, OLD.witness, OLD.item_count) THEN
        RAISE EXCEPTION 'sealed source generation metadata is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS source_generation_sealed_immutable ON source_generation;
CREATE TRIGGER source_generation_sealed_immutable
    BEFORE UPDATE OR DELETE ON source_generation
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_sealed_generation_mutation();

CREATE OR REPLACE FUNCTION storage_v2_reject_source_item_mutation() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    RAISE EXCEPTION 'source_item rows are immutable'
        USING ERRCODE = '55000';
END
$$;

DROP TRIGGER IF EXISTS source_item_immutable ON source_item;
CREATE TRIGGER source_item_immutable
    BEFORE UPDATE OR DELETE ON source_item
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_source_item_mutation();

CREATE OR REPLACE FUNCTION storage_v2_validate_membership_write() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_status storage_v2_generation_status;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT status INTO v_status
          FROM source_generation
         WHERE source_id = NEW.source_id
           AND generation_seq = NEW.valid_from_seq;
        IF v_status IS DISTINCT FROM 'building'::storage_v2_generation_status THEN
            RAISE EXCEPTION 'membership must begin in a building generation';
        END IF;
    ELSE
        IF (NEW.source_id, NEW.source_item_id, NEW.artifact_version_id, NEW.valid_from_seq)
           IS DISTINCT FROM
           (OLD.source_id, OLD.source_item_id, OLD.artifact_version_id, OLD.valid_from_seq) THEN
            RAISE EXCEPTION 'membership identity is immutable'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.valid_to_seq IS DISTINCT FROM OLD.valid_to_seq THEN
        IF OLD.valid_to_seq IS NOT NULL OR NEW.valid_to_seq IS NULL THEN
            RAISE EXCEPTION 'membership intervals may only be closed once';
        END IF;
        SELECT status INTO v_status
          FROM source_generation
         WHERE source_id = NEW.source_id
           AND generation_seq = NEW.valid_to_seq;
        IF v_status IS DISTINCT FROM 'building'::storage_v2_generation_status THEN
            RAISE EXCEPTION 'membership must close at a building generation';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS generation_item_version_valid_write ON generation_item_version;
CREATE TRIGGER generation_item_version_valid_write
    BEFORE INSERT OR UPDATE ON generation_item_version
    FOR EACH ROW EXECUTE FUNCTION storage_v2_validate_membership_write();

CREATE OR REPLACE FUNCTION storage_v2_validate_active_source() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_source_id BIGINT;
    v_pointer BIGINT;
    v_active_ids BIGINT[];
BEGIN
    IF TG_TABLE_NAME = 'logical_source' THEN
        v_source_id := NEW.id;
    ELSE
        v_source_id := NEW.source_id;
    END IF;
    SELECT active_generation_id INTO v_pointer FROM logical_source WHERE id = v_source_id;
    SELECT COALESCE(array_agg(id ORDER BY id), ARRAY[]::BIGINT[])
        INTO v_active_ids
        FROM source_generation
        WHERE source_id = v_source_id AND status = 'active';
    IF v_pointer IS NULL THEN
        IF cardinality(v_active_ids) <> 0 THEN
            RAISE EXCEPTION 'source % has an active generation but no active pointer', v_source_id;
        END IF;
    ELSIF cardinality(v_active_ids) <> 1 OR v_active_ids[1] <> v_pointer THEN
        RAISE EXCEPTION 'source % active pointer and active generation disagree', v_source_id;
    END IF;
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS logical_source_active_consistency ON logical_source;
CREATE CONSTRAINT TRIGGER logical_source_active_consistency
    AFTER INSERT OR UPDATE OF active_generation_id ON logical_source
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION storage_v2_validate_active_source();

DROP TRIGGER IF EXISTS source_generation_active_consistency ON source_generation;
CREATE CONSTRAINT TRIGGER source_generation_active_consistency
    AFTER INSERT OR UPDATE OF status ON source_generation
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION storage_v2_validate_active_source();

CREATE OR REPLACE FUNCTION storage_v2_allocate_generation(
    p_source_id BIGINT,
    p_witness_type TEXT,
    p_witness JSONB
) RETURNS source_generation
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_seq BIGINT;
    v_generation source_generation;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'write') THEN
        RAISE EXCEPTION 'source write access denied' USING ERRCODE = '42501';
    END IF;
    IF p_witness_type IS NULL OR p_witness_type = '' OR p_witness IS NULL THEN
        RAISE EXCEPTION 'generation witness is required';
    END IF;
    INSERT INTO logical_source(id) VALUES (p_source_id) ON CONFLICT (id) DO NOTHING;
    UPDATE logical_source
        SET next_generation_seq = next_generation_seq + 1,
            updated_at = NOW()
        WHERE id = p_source_id
        RETURNING next_generation_seq - 1 INTO v_seq;
    INSERT INTO source_generation(source_id, generation_seq, witness_type, witness)
        VALUES (p_source_id, v_seq, p_witness_type, p_witness)
        RETURNING * INTO v_generation;
    RETURN v_generation;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_seal_generation(
    p_generation_id BIGINT,
    p_item_count BIGINT
) RETURNS source_generation
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
BEGIN
    SELECT * INTO v_generation FROM source_generation WHERE id = p_generation_id FOR UPDATE;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_generation.source_id, 'write') THEN
        RAISE EXCEPTION 'generation not found or access denied' USING ERRCODE = '42501';
    END IF;
    IF v_generation.status <> 'building' OR p_item_count < 0 THEN
        RAISE EXCEPTION 'only a building generation can be sealed';
    END IF;
    UPDATE source_generation
        SET status = 'sealed', sealed_at = NOW(), item_count = p_item_count
        WHERE id = p_generation_id RETURNING * INTO v_generation;
    RETURN v_generation;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_verify_generation(
    p_generation_id BIGINT,
    p_manifest_sha256 TEXT
) RETURNS source_generation
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
BEGIN
    SELECT * INTO v_generation FROM source_generation WHERE id = p_generation_id FOR UPDATE;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_generation.source_id, 'write') THEN
        RAISE EXCEPTION 'generation not found or access denied' USING ERRCODE = '42501';
    END IF;
    IF v_generation.status <> 'sealed' OR p_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'verification requires a sealed generation and SHA-256 manifest';
    END IF;
    UPDATE source_generation
        SET status = 'verified', verified_at = NOW(),
            verification_manifest_sha256 = p_manifest_sha256
        WHERE id = p_generation_id RETURNING * INTO v_generation;
    RETURN v_generation;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_mark_release_candidate(
    p_generation_id BIGINT
) RETURNS source_generation
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
BEGIN
    SELECT * INTO v_generation FROM source_generation WHERE id = p_generation_id FOR UPDATE;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_generation.source_id, 'write') THEN
        RAISE EXCEPTION 'generation not found or access denied' USING ERRCODE = '42501';
    END IF;
    IF v_generation.status <> 'verified' THEN
        RAISE EXCEPTION 'only a verified generation can become a release candidate';
    END IF;
    UPDATE source_generation SET status = 'release_candidate'
        WHERE id = p_generation_id RETURNING * INTO v_generation;
    RETURN v_generation;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_requalify_generation(
    p_generation_id BIGINT,
    p_manifest_sha256 TEXT
) RETURNS source_generation
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
BEGIN
    SELECT * INTO v_generation FROM source_generation WHERE id = p_generation_id FOR UPDATE;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_generation.source_id, 'write') THEN
        RAISE EXCEPTION 'generation not found or access denied' USING ERRCODE = '42501';
    END IF;
    IF v_generation.status <> 'superseded' OR p_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'requalification requires a superseded generation and SHA-256 manifest';
    END IF;
    UPDATE source_generation
        SET status = 'verified', verified_at = NOW(),
            verification_manifest_sha256 = p_manifest_sha256,
            activated_at = NULL, superseded_at = NULL
        WHERE id = p_generation_id RETURNING * INTO v_generation;
    RETURN v_generation;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_activate_generation(
    p_source_id BIGINT,
    p_candidate_id BIGINT,
    p_expected_active_id BIGINT DEFAULT NULL
) RETURNS source_generation
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_source logical_source;
    v_candidate source_generation;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'activation requires administrator authority' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_source FROM logical_source WHERE id = p_source_id FOR UPDATE;
    IF NOT FOUND OR v_source.active_generation_id IS DISTINCT FROM p_expected_active_id THEN
        RAISE EXCEPTION 'active pointer drift for source %', p_source_id;
    END IF;
    SELECT * INTO v_candidate FROM source_generation
        WHERE id = p_candidate_id AND source_id = p_source_id FOR UPDATE;
    IF NOT FOUND OR v_candidate.status <> 'release_candidate' THEN
        RAISE EXCEPTION 'candidate must belong to the source and be release_candidate';
    END IF;
    IF v_source.active_generation_id IS NOT NULL THEN
        UPDATE source_generation
            SET status = 'superseded', superseded_at = NOW()
            WHERE id = v_source.active_generation_id AND status = 'active';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'expected active generation is not active';
        END IF;
    END IF;
    UPDATE source_generation
        SET status = 'active', activated_at = NOW(), superseded_at = NULL
        WHERE id = p_candidate_id RETURNING * INTO v_candidate;
    UPDATE logical_source
        SET active_generation_id = p_candidate_id, updated_at = NOW()
        WHERE id = p_source_id;
    RETURN v_candidate;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_close_membership(
    p_source_id BIGINT,
    p_source_item_id BIGINT,
    p_valid_from_seq BIGINT,
    p_valid_to_seq BIGINT
) RETURNS generation_item_version
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_membership generation_item_version;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'write') THEN
        RAISE EXCEPTION 'source write access denied' USING ERRCODE = '42501';
    END IF;
    UPDATE generation_item_version
        SET valid_to_seq = p_valid_to_seq
        WHERE source_id = p_source_id
          AND source_item_id = p_source_item_id
          AND valid_from_seq = p_valid_from_seq
          AND valid_to_seq IS NULL
        RETURNING * INTO v_membership;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'open membership not found';
    END IF;
    RETURN v_membership;
END
$$;

REVOKE UPDATE, DELETE ON logical_source FROM PUBLIC;
REVOKE UPDATE, DELETE ON source_generation FROM PUBLIC;
REVOKE UPDATE, DELETE ON source_item FROM PUBLIC;
REVOKE UPDATE, DELETE ON artifact_version FROM PUBLIC;
REVOKE UPDATE, DELETE ON generation_item_version FROM PUBLIC;
