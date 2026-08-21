-- Migration 030: content-addressed bodies and integrity-checked packs
--
-- This migration is additive. It does not copy legacy file/chunk bytes and it
-- does not route current reads or writes through storage v2.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS btree_gist;

DO $$
BEGIN
    CREATE TYPE storage_v2_pack_status AS ENUM (
        'candidate',
        'verified',
        'published',
        'retired',
        'reclaimed',
        'abandoned'
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    CREATE TYPE storage_v2_body_codec AS ENUM ('identity', 'zstd');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

CREATE TABLE IF NOT EXISTS content_dictionary (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    codec storage_v2_body_codec NOT NULL CHECK (codec = 'zstd'),
    digest_algorithm TEXT NOT NULL CHECK (digest_algorithm = 'sha256-v1'),
    digest BYTEA NOT NULL CHECK (octet_length(digest) = 32),
    dictionary_bytes BYTEA NOT NULL CHECK (octet_length(dictionary_bytes) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (codec, digest_algorithm, digest),
    CHECK (digest(dictionary_bytes, 'sha256') = digest)
);

CREATE TABLE IF NOT EXISTS content_pack (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    storage_key TEXT NOT NULL UNIQUE
        CHECK (storage_key ~ '^[0-9a-f]{8}-[0-9a-f-]{27}[.]pack$'),
    build_nonce UUID NOT NULL UNIQUE,
    status storage_v2_pack_status NOT NULL DEFAULT 'candidate',
    manifest_sha256 BYTEA CHECK (
        manifest_sha256 IS NULL OR octet_length(manifest_sha256) = 32
    ),
    stored_bytes BIGINT NOT NULL DEFAULT 0 CHECK (stored_bytes >= 0),
    live_bytes BIGINT NOT NULL DEFAULT 0 CHECK (live_bytes >= 0),
    entry_count BIGINT NOT NULL DEFAULT 0 CHECK (entry_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    reclaimed_at TIMESTAMPTZ,
    CHECK (live_bytes <= stored_bytes),
    CHECK (status IN ('candidate', 'abandoned') OR manifest_sha256 IS NOT NULL),
    CHECK (status NOT IN ('verified', 'published', 'retired', 'reclaimed') OR verified_at IS NOT NULL),
    CHECK (status NOT IN ('published', 'retired', 'reclaimed') OR published_at IS NOT NULL),
    CHECK (status NOT IN ('retired', 'reclaimed') OR retired_at IS NOT NULL),
    CHECK (status <> 'reclaimed' OR reclaimed_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS content_body (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    digest_algorithm TEXT NOT NULL CHECK (digest_algorithm = 'sha256-v1'),
    digest BYTEA NOT NULL CHECK (octet_length(digest) = 32),
    logical_length BIGINT NOT NULL CHECK (logical_length >= 0),
    inline_bytes BYTEA,
    pack_id UUID REFERENCES content_pack(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (digest_algorithm, digest, logical_length),
    UNIQUE (pack_id, id),
    CHECK ((inline_bytes IS NOT NULL)::INTEGER + (pack_id IS NOT NULL)::INTEGER = 1),
    CHECK (inline_bytes IS NULL OR octet_length(inline_bytes) <= 65536),
    CHECK (inline_bytes IS NULL OR octet_length(inline_bytes) = logical_length),
    CHECK (inline_bytes IS NULL OR digest(inline_bytes, 'sha256') = digest)
);

CREATE TABLE IF NOT EXISTS content_pack_entry (
    pack_id UUID NOT NULL REFERENCES content_pack(id) ON DELETE RESTRICT,
    ordinal BIGINT NOT NULL CHECK (ordinal >= 0),
    body_id BIGINT NOT NULL REFERENCES content_body(id) ON DELETE RESTRICT,
    pack_offset BIGINT NOT NULL CHECK (pack_offset >= 0),
    stored_length BIGINT NOT NULL CHECK (stored_length > 0),
    codec storage_v2_body_codec NOT NULL,
    dictionary_id BIGINT REFERENCES content_dictionary(id) ON DELETE RESTRICT,
    entry_digest BYTEA NOT NULL CHECK (octet_length(entry_digest) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (pack_id, ordinal),
    UNIQUE (pack_id, body_id),
    UNIQUE (pack_id, body_id, pack_offset, stored_length),
    CHECK (pack_offset <= 9223372036854775807 - stored_length),
    CHECK ((codec = 'identity' AND dictionary_id IS NULL) OR codec = 'zstd'),
    EXCLUDE USING gist (
        pack_id WITH =,
        int8range(pack_offset, pack_offset + stored_length, '[)') WITH &&
    )
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'content_body_pack_entry_fkey'
           AND conrelid = 'public.content_body'::REGCLASS
    ) THEN
        ALTER TABLE content_body
            ADD CONSTRAINT content_body_pack_entry_fkey
            FOREIGN KEY (pack_id, id)
            REFERENCES content_pack_entry(pack_id, body_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'artifact_version_raw_body_fkey'
           AND conrelid = 'public.artifact_version'::REGCLASS
    ) THEN
        ALTER TABLE artifact_version
            ADD CONSTRAINT artifact_version_raw_body_fkey
            FOREIGN KEY (raw_body_id) REFERENCES content_body(id) ON DELETE RESTRICT;
    END IF;
END
$$;

-- Pack reclamation is global because bodies are globally deduplicated. A NULL
-- source denotes a global epoch and remains admin-only under the policy below.
ALTER TABLE storage_v2_gc_epoch ALTER COLUMN source_id DROP NOT NULL;

CREATE TABLE IF NOT EXISTS content_reader_epoch (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id UUID NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE INDEX IF NOT EXISTS idx_content_reader_epoch_open
    ON content_reader_epoch (started_at) WHERE finished_at IS NULL;

CREATE TABLE IF NOT EXISTS content_pack_retirement (
    pack_id UUID PRIMARY KEY REFERENCES content_pack(id) ON DELETE RESTRICT,
    replacement_pack_id UUID NOT NULL REFERENCES content_pack(id) ON DELETE RESTRICT,
    gc_epoch_id BIGINT NOT NULL REFERENCES storage_v2_gc_epoch(id) ON DELETE RESTRICT,
    switched_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    readers_drained_at TIMESTAMPTZ,
    reclaimed_bytes BIGINT CHECK (reclaimed_bytes IS NULL OR reclaimed_bytes >= 0),
    CHECK (pack_id <> replacement_pack_id),
    CHECK (readers_drained_at IS NULL OR readers_drained_at >= switched_at)
);

CREATE OR REPLACE VIEW storage_v2_content_metrics AS
SELECT
    COALESCE((SELECT SUM(logical_length) FROM content_body), 0)::BIGINT AS unique_logical_bytes,
    COALESCE((SELECT SUM(octet_length(inline_bytes)) FROM content_body WHERE inline_bytes IS NOT NULL), 0)::BIGINT
      + COALESCE((SELECT SUM(stored_bytes) FROM content_pack WHERE status IN ('published', 'retired')), 0)::BIGINT
        AS stored_bytes,
    (SELECT COUNT(*) FROM content_body WHERE inline_bytes IS NOT NULL)::BIGINT AS inline_count,
    (SELECT COUNT(*) FROM content_body WHERE pack_id IS NOT NULL)::BIGINT AS packed_count,
    COALESCE((SELECT SUM(stored_bytes - live_bytes) FROM content_pack WHERE status IN ('published', 'retired')), 0)::BIGINT
        AS dead_bytes,
    COALESCE((SELECT SUM(reclaimed_bytes) FROM content_pack_retirement), 0)::BIGINT AS reclaimed_bytes;

CREATE OR REPLACE FUNCTION storage_v2_reject_immutable_content() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME USING ERRCODE = '55000';
END
$$;

DROP TRIGGER IF EXISTS content_dictionary_immutable ON content_dictionary;
CREATE TRIGGER content_dictionary_immutable
    BEFORE UPDATE OR DELETE ON content_dictionary
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_immutable_content();

DROP TRIGGER IF EXISTS content_pack_entry_immutable ON content_pack_entry;
CREATE TRIGGER content_pack_entry_immutable
    BEFORE UPDATE OR DELETE ON content_pack_entry
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_immutable_content();

CREATE OR REPLACE FUNCTION storage_v2_guard_body_update() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_owner OID;
BEGIN
    IF (NEW.digest_algorithm, NEW.digest, NEW.logical_length, NEW.inline_bytes, NEW.created_at)
       IS DISTINCT FROM
       (OLD.digest_algorithm, OLD.digest, OLD.logical_length, OLD.inline_bytes, OLD.created_at) THEN
        RAISE EXCEPTION 'content body identity and bytes are immutable'
            USING ERRCODE = '55000';
    END IF;
    SELECT relowner INTO v_owner FROM pg_class WHERE oid = TG_RELID;
    IF current_user::REGROLE::OID <> v_owner THEN
        RAISE EXCEPTION 'content body placement changes require a controlled function'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS content_body_controlled_update ON content_body;
CREATE TRIGGER content_body_controlled_update
    BEFORE UPDATE ON content_body
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_body_update();

DROP TRIGGER IF EXISTS content_body_immutable_delete ON content_body;
CREATE TRIGGER content_body_immutable_delete
    BEFORE DELETE ON content_body
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_immutable_content();

CREATE OR REPLACE FUNCTION storage_v2_guard_pack_update() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_owner OID;
BEGIN
    IF (NEW.id, NEW.storage_key, NEW.build_nonce, NEW.created_at) IS DISTINCT FROM
       (OLD.id, OLD.storage_key, OLD.build_nonce, OLD.created_at) THEN
        RAISE EXCEPTION 'content pack identity is immutable' USING ERRCODE = '55000';
    END IF;
    SELECT relowner INTO v_owner FROM pg_class WHERE oid = TG_RELID;
    IF current_user::REGROLE::OID <> v_owner THEN
        RAISE EXCEPTION 'content pack state changes require a controlled function'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS content_pack_controlled_update ON content_pack;
CREATE TRIGGER content_pack_controlled_update
    BEFORE UPDATE ON content_pack
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_pack_update();

CREATE OR REPLACE FUNCTION storage_v2_put_inline_body(p_bytes BYTEA)
RETURNS content_body
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_digest BYTEA := digest(p_bytes, 'sha256');
    v_body content_body;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'content writes require administrator authority' USING ERRCODE = '42501';
    END IF;
    IF octet_length(p_bytes) > 65536 THEN
        RAISE EXCEPTION 'inline body exceeds 65536-byte bound';
    END IF;
    INSERT INTO content_body(digest_algorithm, digest, logical_length, inline_bytes)
        VALUES ('sha256-v1', v_digest, octet_length(p_bytes), p_bytes)
        ON CONFLICT (digest_algorithm, digest, logical_length) DO NOTHING
        RETURNING * INTO v_body;
    IF FOUND THEN
        RETURN v_body;
    END IF;
    SELECT * INTO v_body
      FROM content_body
     WHERE digest_algorithm = 'sha256-v1'
       AND digest = v_digest
       AND logical_length = octet_length(p_bytes)
     FOR SHARE;
    IF v_body.inline_bytes IS NULL OR v_body.inline_bytes IS DISTINCT FROM p_bytes THEN
        RAISE EXCEPTION 'digest collision or packed duplicate requires full-byte verification'
            USING ERRCODE = '22000';
    END IF;
    RETURN v_body;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_create_pack(
    p_pack_id UUID,
    p_storage_key TEXT,
    p_build_nonce UUID
) RETURNS content_pack
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_pack content_pack;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'pack creation requires administrator authority' USING ERRCODE = '42501';
    END IF;
    INSERT INTO content_pack(id, storage_key, build_nonce)
        VALUES (p_pack_id, p_storage_key, p_build_nonce)
        RETURNING * INTO v_pack;
    RETURN v_pack;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_verify_pack(
    p_pack_id UUID,
    p_manifest_sha256 BYTEA,
    p_stored_bytes BIGINT
) RETURNS content_pack
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_pack content_pack;
    v_count BIGINT;
    v_sum BIGINT;
    v_min BIGINT;
    v_max BIGINT;
BEGIN
    IF NOT storage_v2_is_admin() OR octet_length(p_manifest_sha256) <> 32 THEN
        RAISE EXCEPTION 'pack verification requires administrator authority and SHA-256 manifest'
            USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_pack FROM content_pack WHERE id = p_pack_id FOR UPDATE;
    IF NOT FOUND OR v_pack.status <> 'candidate' THEN
        RAISE EXCEPTION 'only a candidate pack can be verified';
    END IF;
    SELECT COUNT(*), COALESCE(SUM(stored_length), 0), COALESCE(MIN(pack_offset), 0),
           COALESCE(MAX(pack_offset + stored_length), 0)
      INTO v_count, v_sum, v_min, v_max
      FROM content_pack_entry WHERE pack_id = p_pack_id;
    IF p_stored_bytes <= 0 OR v_count = 0 OR v_min <> 0
       OR v_sum <> p_stored_bytes OR v_max <> p_stored_bytes THEN
        RAISE EXCEPTION 'pack entries do not form one complete bounded byte range';
    END IF;
    UPDATE content_pack
       SET status = 'verified', manifest_sha256 = p_manifest_sha256,
           stored_bytes = p_stored_bytes, live_bytes = p_stored_bytes,
           entry_count = v_count, verified_at = NOW()
     WHERE id = p_pack_id RETURNING * INTO v_pack;
    RETURN v_pack;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_publish_pack(p_pack_id UUID)
RETURNS content_pack
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_pack content_pack;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'pack publication requires administrator authority' USING ERRCODE = '42501';
    END IF;
    UPDATE content_pack
       SET status = 'published', published_at = NOW()
     WHERE id = p_pack_id AND status = 'verified'
     RETURNING * INTO v_pack;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'only a verified pack can be published';
    END IF;
    RETURN v_pack;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_abandon_pack(p_pack_id UUID)
RETURNS content_pack
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_pack content_pack;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'pack cleanup requires administrator authority' USING ERRCODE = '42501';
    END IF;
    UPDATE content_pack SET status = 'abandoned'
     WHERE id = p_pack_id AND status = 'candidate'
       AND NOT EXISTS (SELECT 1 FROM content_body WHERE pack_id = p_pack_id)
     RETURNING * INTO v_pack;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'only an unreferenced candidate pack can be abandoned';
    END IF;
    RETURN v_pack;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_begin_reader_epoch() RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_principal_id UUID;
    v_epoch_id UUID;
BEGIN
    v_principal_id := NULLIF(current_setting('app.user_id', TRUE), '')::UUID;
    IF v_principal_id IS NULL THEN
        RAISE EXCEPTION 'reader epoch requires an authenticated principal'
            USING ERRCODE = '42501';
    END IF;
    INSERT INTO content_reader_epoch(principal_id)
        VALUES (v_principal_id) RETURNING id INTO v_epoch_id;
    RETURN v_epoch_id;
EXCEPTION
    WHEN invalid_text_representation THEN
        RAISE EXCEPTION 'reader epoch requires an authenticated principal'
            USING ERRCODE = '42501';
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_end_reader_epoch(p_epoch_id UUID) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_principal_id UUID;
BEGIN
    v_principal_id := NULLIF(current_setting('app.user_id', TRUE), '')::UUID;
    UPDATE content_reader_epoch SET finished_at = clock_timestamp()
     WHERE id = p_epoch_id AND finished_at IS NULL
       AND (principal_id = v_principal_id OR storage_v2_is_admin());
    IF NOT FOUND THEN
        RAISE EXCEPTION 'open reader epoch not found';
    END IF;
EXCEPTION
    WHEN invalid_text_representation THEN
        RAISE EXCEPTION 'reader epoch requires an authenticated principal'
            USING ERRCODE = '42501';
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_switch_pack(
    p_old_pack_id UUID,
    p_new_pack_id UUID,
    p_gc_epoch_id BIGINT
) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_switched BIGINT;
    v_old_bytes BIGINT;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'pack switch requires administrator authority' USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM storage_v2_gc_epoch
     WHERE id = p_gc_epoch_id AND source_id IS NULL AND status IN ('verified', 'sweeping')
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'verified global GC epoch required';
    END IF;
    SELECT stored_bytes INTO v_old_bytes FROM content_pack
     WHERE id = p_old_pack_id AND status = 'published' FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM content_pack WHERE id = p_new_pack_id AND status = 'published' FOR UPDATE
    ) THEN
        RAISE EXCEPTION 'old and replacement packs must be published';
    END IF;
    IF EXISTS (
        SELECT 1 FROM content_body old_body
         WHERE old_body.pack_id = p_old_pack_id
           AND NOT EXISTS (
               SELECT 1 FROM content_pack_entry new_entry
                WHERE new_entry.pack_id = p_new_pack_id
                  AND new_entry.body_id = old_body.id
           )
    ) THEN
        RAISE EXCEPTION 'replacement pack is missing a live body';
    END IF;
    UPDATE content_body SET pack_id = p_new_pack_id WHERE pack_id = p_old_pack_id;
    GET DIAGNOSTICS v_switched = ROW_COUNT;
    UPDATE content_pack SET status = 'retired', retired_at = clock_timestamp(), live_bytes = 0
     WHERE id = p_old_pack_id;
    INSERT INTO content_pack_retirement(pack_id, replacement_pack_id, gc_epoch_id, reclaimed_bytes)
        VALUES (p_old_pack_id, p_new_pack_id, p_gc_epoch_id, v_old_bytes);
    RETURN v_switched;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_mark_pack_readers_drained(p_pack_id UUID) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_switched_at TIMESTAMPTZ;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'reader drain requires administrator authority' USING ERRCODE = '42501';
    END IF;
    SELECT switched_at INTO v_switched_at FROM content_pack_retirement
     WHERE pack_id = p_pack_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'pack retirement not found';
    END IF;
    IF EXISTS (
        SELECT 1 FROM content_reader_epoch
         WHERE started_at <= v_switched_at AND finished_at IS NULL
    ) THEN
        RAISE EXCEPTION 'pre-switch readers are still active';
    END IF;
    UPDATE content_pack_retirement SET readers_drained_at = clock_timestamp()
     WHERE pack_id = p_pack_id;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_reclaim_pack(p_pack_id UUID) RETURNS content_pack
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_pack content_pack;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'pack reclamation requires administrator authority' USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM content_body WHERE pack_id = p_pack_id) THEN
        RAISE EXCEPTION 'pack remains referenced';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM content_pack_retirement retirement
          JOIN storage_v2_gc_epoch epoch ON epoch.id = retirement.gc_epoch_id
         WHERE retirement.pack_id = p_pack_id
           AND retirement.readers_drained_at IS NOT NULL
           AND epoch.status IN ('sweeping', 'complete')
    ) THEN
        RAISE EXCEPTION 'accepted GC epoch and drained readers required';
    END IF;
    UPDATE content_pack SET status = 'reclaimed', reclaimed_at = clock_timestamp()
     WHERE id = p_pack_id AND status = 'retired'
     RETURNING * INTO v_pack;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'only a retired pack can be reclaimed';
    END IF;
    RETURN v_pack;
END
$$;

ALTER TABLE content_dictionary ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_pack ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_body ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_pack_entry ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_reader_epoch ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_pack_retirement ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS content_dictionary_admin ON content_dictionary;
CREATE POLICY content_dictionary_admin ON content_dictionary
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP POLICY IF EXISTS content_pack_admin ON content_pack;
CREATE POLICY content_pack_admin ON content_pack
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP POLICY IF EXISTS content_body_admin ON content_body;
CREATE POLICY content_body_admin ON content_body
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP POLICY IF EXISTS content_pack_entry_admin ON content_pack_entry;
CREATE POLICY content_pack_entry_admin ON content_pack_entry
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP POLICY IF EXISTS content_reader_epoch_admin ON content_reader_epoch;
CREATE POLICY content_reader_epoch_admin ON content_reader_epoch
    USING (
        principal_id = NULLIF(current_setting('app.user_id', TRUE), '')::UUID
        OR storage_v2_is_admin()
    )
    WITH CHECK (
        principal_id = NULLIF(current_setting('app.user_id', TRUE), '')::UUID
        OR storage_v2_is_admin()
    );
DROP POLICY IF EXISTS content_pack_retirement_admin ON content_pack_retirement;
CREATE POLICY content_pack_retirement_admin ON content_pack_retirement
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());

DROP POLICY IF EXISTS storage_v2_gc_epoch_isolation ON storage_v2_gc_epoch;
CREATE POLICY storage_v2_gc_epoch_isolation ON storage_v2_gc_epoch
    USING (
        (source_id IS NULL AND storage_v2_is_admin())
        OR (source_id IS NOT NULL AND storage_v2_can_access_source(source_id, 'read'))
    )
    WITH CHECK (
        (source_id IS NULL AND storage_v2_is_admin())
        OR (source_id IS NOT NULL AND storage_v2_can_access_source(source_id, 'write'))
    );

REVOKE UPDATE, DELETE ON content_dictionary FROM PUBLIC;
REVOKE UPDATE, DELETE ON content_pack_entry FROM PUBLIC;
REVOKE UPDATE, DELETE ON content_body FROM PUBLIC;
REVOKE UPDATE, DELETE ON content_pack FROM PUBLIC;
