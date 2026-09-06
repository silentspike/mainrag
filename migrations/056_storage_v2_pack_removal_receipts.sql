-- Metadata retirement/reclamation is permission to remove, not proof of unlink.
CREATE TABLE IF NOT EXISTS storage_v2_pack_removal_receipt (
    pack_id UUID PRIMARY KEY REFERENCES content_pack(id) ON DELETE RESTRICT,
    file_bytes BIGINT NOT NULL CHECK (file_bytes > 0),
    removed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE storage_v2_pack_removal_receipt ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS pack_removal_admin ON storage_v2_pack_removal_receipt;
CREATE POLICY pack_removal_admin ON storage_v2_pack_removal_receipt
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
REVOKE ALL ON storage_v2_pack_removal_receipt FROM PUBLIC;
DROP TRIGGER IF EXISTS pack_removal_receipt_immutable ON storage_v2_pack_removal_receipt;
CREATE TRIGGER pack_removal_receipt_immutable BEFORE UPDATE OR DELETE ON storage_v2_pack_removal_receipt
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_immutable_content();

CREATE OR REPLACE FUNCTION storage_v2_record_pack_removal(p_pack_id UUID, p_file_bytes BIGINT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = off
AS $$
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'pack removal receipt requires administrator authority' USING ERRCODE='42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM content_pack pack
        JOIN content_pack_retirement retirement ON retirement.pack_id=pack.id
        JOIN storage_v2_gc_epoch epoch ON epoch.id=retirement.gc_epoch_id
        WHERE pack.id=p_pack_id AND pack.status='reclaimed'
          AND pack.stored_bytes=p_file_bytes AND retirement.readers_drained_at IS NOT NULL
          AND epoch.status IN ('sweeping','complete')
    ) THEN
        RAISE EXCEPTION 'verified reclamation state and exact file length required';
    END IF;
    INSERT INTO storage_v2_pack_removal_receipt(pack_id,file_bytes)
        VALUES(p_pack_id,p_file_bytes) ON CONFLICT(pack_id) DO NOTHING;
    IF NOT EXISTS (SELECT 1 FROM storage_v2_pack_removal_receipt
                   WHERE pack_id=p_pack_id AND file_bytes=p_file_bytes) THEN
        RAISE EXCEPTION 'pack removal receipt identity differs';
    END IF;
END
$$;

CREATE OR REPLACE VIEW storage_v2_content_metrics AS
SELECT
    COALESCE((SELECT SUM(logical_length) FROM content_body),0)::BIGINT AS unique_logical_bytes,
    COALESCE((SELECT SUM(octet_length(inline_bytes)) FROM content_body WHERE inline_bytes IS NOT NULL),0)::BIGINT
      + COALESCE((SELECT SUM(pack.stored_bytes) FROM content_pack pack
                  WHERE pack.status IN ('published','retired','reclaimed')
                    AND NOT EXISTS (SELECT 1 FROM storage_v2_pack_removal_receipt receipt WHERE receipt.pack_id=pack.id)),0)::BIGINT AS stored_bytes,
    (SELECT COUNT(*) FROM content_body WHERE inline_bytes IS NOT NULL)::BIGINT AS inline_count,
    (SELECT COUNT(*) FROM content_body WHERE pack_id IS NOT NULL)::BIGINT AS packed_count,
    COALESCE((SELECT SUM(pack.stored_bytes-(SELECT COALESCE(SUM(entry.stored_length),0)
                  FROM content_pack_entry entry JOIN content_body body
                    ON body.id=entry.body_id AND body.pack_id=entry.pack_id
                  WHERE entry.pack_id=pack.id)) FROM content_pack pack
              WHERE pack.status IN ('published','retired','reclaimed')
                AND NOT EXISTS (SELECT 1 FROM storage_v2_pack_removal_receipt receipt WHERE receipt.pack_id=pack.id)),0)::BIGINT AS dead_bytes,
    COALESCE((SELECT SUM(file_bytes) FROM storage_v2_pack_removal_receipt),0)::BIGINT AS reclaimed_bytes;
