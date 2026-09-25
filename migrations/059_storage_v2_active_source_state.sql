-- Migration 059: inspect one active source only under a complete activation receipt.
-- Installation alone does not change the application read path or any pointer.

CREATE OR REPLACE FUNCTION storage_v2_require_complete_active_set(
    p_manifest_sha256 TEXT
) RETURNS VOID
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_receipt storage_v2_activation_set_evidence;
BEGIN
    IF p_manifest_sha256 IS NULL OR p_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'exact activated manifest digest is required';
    END IF;
    SELECT * INTO v_receipt FROM storage_v2_activation_set_evidence
     ORDER BY created_at DESC, id DESC LIMIT 1;
    IF NOT FOUND OR v_receipt.manifest_sha256 <> p_manifest_sha256
       OR v_receipt.source_count <> (SELECT COUNT(*) FROM sources)
       OR v_receipt.source_count <> (SELECT COUNT(*) FROM logical_source)
       OR v_receipt.pointer_set_sha256 IS DISTINCT FROM (
           SELECT encode(digest(convert_to(jsonb_agg(jsonb_build_object(
               'source_id', pointer.id,
               'active_generation_id', pointer.active_generation_id
           ) ORDER BY pointer.id)::TEXT, 'UTF8'), 'sha256'), 'hex')
             FROM logical_source pointer
       )
       OR v_receipt.source_classification_sha256 IS DISTINCT FROM (
           SELECT encode(digest(convert_to(COALESCE(jsonb_agg(jsonb_build_object(
               'source_id', source.id, 'is_test', source.is_test
           ) ORDER BY source.id), '[]'::JSONB)::TEXT, 'UTF8'), 'sha256'), 'hex')
             FROM sources source
       )
       OR EXISTS (
           SELECT 1 FROM logical_source pointer
           LEFT JOIN source_generation active_generation
             ON active_generation.id = pointer.active_generation_id
            AND active_generation.source_id = pointer.id
           WHERE active_generation.id IS NULL OR active_generation.status <> 'active'
       ) THEN
        RAISE EXCEPTION 'complete activated source set and exact receipt are required';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_active_source_state(
    p_manifest_sha256 TEXT,
    p_source_id BIGINT,
    p_include_test BOOLEAN DEFAULT FALSE
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_state JSONB;
BEGIN
    PERFORM storage_v2_require_complete_active_set(p_manifest_sha256);
    v_state := storage_v2_shadow_source_state(p_source_id, 'current', p_include_test);
    RETURN v_state || jsonb_build_object(
        'read_path', 'storage_v2_active',
        'activation_manifest_sha256', p_manifest_sha256
    );
END
$$;
