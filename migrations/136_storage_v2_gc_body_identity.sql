-- Keep pack-manifest identities after an unreachable body has been collected.
-- No content bytes, pointers or existing generations are removed here.
BEGIN;

CREATE TABLE storage_v2_body_identity (
    id BIGINT PRIMARY KEY,
    digest_algorithm TEXT NOT NULL CHECK (digest_algorithm='sha256-v1'),
    digest BYTEA NOT NULL CHECK (octet_length(digest)=32),
    logical_length BIGINT NOT NULL CHECK (logical_length>=0)
);
INSERT INTO storage_v2_body_identity
    SELECT id,digest_algorithm,digest,logical_length FROM content_body;
ALTER TABLE storage_v2_body_identity ENABLE ROW LEVEL SECURITY;
CREATE POLICY body_identity_admin ON storage_v2_body_identity
    USING(storage_v2_is_admin());
REVOKE ALL ON storage_v2_body_identity FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_body_identity TO mainrag;
CREATE TRIGGER body_identity_immutable BEFORE UPDATE OR DELETE
    ON storage_v2_body_identity FOR EACH ROW
    EXECUTE FUNCTION storage_v2_reject_immutable_content();

CREATE FUNCTION storage_v2_record_body_identity() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
BEGIN
    INSERT INTO storage_v2_body_identity(id,digest_algorithm,digest,logical_length)
        VALUES(NEW.id,NEW.digest_algorithm,NEW.digest,NEW.logical_length)
        ON CONFLICT(id) DO NOTHING;
    IF NOT EXISTS(SELECT 1 FROM storage_v2_body_identity
        WHERE id=NEW.id AND digest_algorithm=NEW.digest_algorithm
          AND digest=NEW.digest AND logical_length=NEW.logical_length) THEN
        RAISE EXCEPTION 'body identity differs from its retained pack identity';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION storage_v2_record_body_identity() FROM PUBLIC,mainrag;
CREATE TRIGGER content_body_record_identity AFTER INSERT ON content_body
    FOR EACH ROW EXECUTE FUNCTION storage_v2_record_body_identity();

-- Entries are immutable pack metadata, not live-body reachability roots.
ALTER TABLE content_pack_entry DROP CONSTRAINT content_pack_entry_body_id_fkey;
ALTER TABLE content_pack_entry ADD CONSTRAINT content_pack_entry_body_id_fkey
    FOREIGN KEY(body_id) REFERENCES storage_v2_body_identity(id) ON DELETE RESTRICT;

CREATE TABLE storage_v2_gc_receipt (
    manifest_sha256 TEXT PRIMARY KEY CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
    gc_epoch_id BIGINT NOT NULL UNIQUE REFERENCES storage_v2_gc_epoch(id) ON DELETE RESTRICT,
    before_state_sha256 TEXT NOT NULL CHECK(before_state_sha256 ~ '^[0-9a-f]{64}$'),
    approval_sha256 TEXT NOT NULL CHECK(approval_sha256 ~ '^[0-9a-f]{64}$'),
    result JSONB NOT NULL CHECK(jsonb_typeof(result)='object'),
    committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE storage_v2_gc_receipt ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON storage_v2_gc_receipt FROM PUBLIC,mainrag;
CREATE TRIGGER gc_receipt_immutable BEFORE UPDATE OR DELETE
    ON storage_v2_gc_receipt FOR EACH ROW
    EXECUTE FUNCTION storage_v2_reject_immutable_content();

-- Application administrators may execute exact accepted pack work without
-- receiving direct access to the protected GC receipt ledger.
CREATE FUNCTION storage_v2_gc_pack_authority(p_manifest_sha256 TEXT,p_pack_id UUID)
RETURNS TABLE(gc_epoch_id BIGINT,pack_root TEXT,maintenance_binary_sha256 TEXT)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'GC pack work requires administrator authority' USING ERRCODE='42501';
    END IF;
    RETURN QUERY SELECT receipt.gc_epoch_id,receipt.result->'resource_policy'->>'pack_root',
                        receipt.result->>'maintenance_binary_sha256'
      FROM storage_v2_gc_receipt receipt JOIN storage_v2_gc_epoch epoch ON epoch.id=receipt.gc_epoch_id
      JOIN content_pack pack ON pack.id=p_pack_id
      WHERE receipt.manifest_sha256=p_manifest_sha256 AND epoch.root_manifest_sha256=p_manifest_sha256
        AND epoch.source_id IS NULL AND epoch.status IN ('sweeping','complete')
        AND pack.status IN ('published','retired','reclaimed')
        AND receipt.result->>'phase'='DB_COMMITTED_PACK_RECLAIM_PENDING'
        AND EXISTS(SELECT 1 FROM jsonb_array_elements(receipt.result->'pack_targets') target
          WHERE target->>'id'=pack.id::text AND target->>'storage_key'=pack.storage_key
            AND (target->>'stored_bytes')::BIGINT=pack.stored_bytes
            AND target->>'manifest_sha256'=to_jsonb(pack)->>'manifest_sha256');
END $$;
REVOKE ALL ON FUNCTION storage_v2_gc_pack_authority(TEXT,UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_gc_pack_authority(TEXT,UUID) TO mainrag;

ALTER TABLE content_pack_retirement ALTER COLUMN replacement_pack_id DROP NOT NULL;
CREATE FUNCTION storage_v2_retire_empty_pack(p_pack_id UUID,p_gc_epoch_id BIGINT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE v_bytes BIGINT;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'empty pack retirement requires administrator authority' USING ERRCODE='42501';
    END IF;
    PERFORM 1 FROM storage_v2_gc_epoch WHERE id=p_gc_epoch_id
        AND source_id IS NULL AND status IN ('verified','sweeping') FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'verified global GC epoch required'; END IF;
    SELECT stored_bytes INTO v_bytes FROM content_pack
        WHERE id=p_pack_id AND status='published' FOR UPDATE;
    IF NOT FOUND OR EXISTS(SELECT 1 FROM content_body WHERE pack_id=p_pack_id) THEN
        RAISE EXCEPTION 'published pack still has live body assignments';
    END IF;
    UPDATE content_pack SET status='retired',retired_at=clock_timestamp(),live_bytes=0
        WHERE id=p_pack_id;
    INSERT INTO content_pack_retirement(pack_id,replacement_pack_id,gc_epoch_id,reclaimed_bytes)
        VALUES(p_pack_id,NULL,p_gc_epoch_id,v_bytes);
END $$;
REVOKE ALL ON FUNCTION storage_v2_retire_empty_pack(UUID,BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_retire_empty_pack(UUID,BIGINT) TO mainrag;
COMMIT;
