-- Migration 061: targeted intelligence over the complete active source set.
-- Installation leaves the application default and every active pointer alone.

CREATE OR REPLACE FUNCTION storage_v2_active_intelligence_command(
    p_manifest_sha256 TEXT,
    p_command TEXT,
    p_query JSONB DEFAULT '{}'::JSONB,
    p_source_id BIGINT DEFAULT NULL,
    p_include_test BOOLEAN DEFAULT FALSE
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_source RECORD;
    v_results JSONB := '[]'::JSONB;
BEGIN
    PERFORM storage_v2_require_complete_active_set(p_manifest_sha256);
    IF p_command IS NULL OR p_command NOT IN ('card', 'explain', 'layers', 'ownership')
       OR p_query IS NULL OR jsonb_typeof(p_query) <> 'object'
       OR p_include_test IS NULL
       OR (p_source_id IS NOT NULL AND p_source_id <= 0) THEN
        RAISE EXCEPTION 'valid active intelligence request required';
    END IF;
    IF p_include_test AND NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'test scope requires administrator authority'
            USING ERRCODE = '42501';
    END IF;
    IF p_source_id IS NOT NULL THEN
        PERFORM storage_v2_require_test_scope(p_source_id, p_include_test);
    END IF;
    FOR v_source IN
        SELECT source.id, source.is_test FROM sources source
         WHERE (p_source_id IS NULL OR source.id = p_source_id)
           AND (p_include_test OR NOT source.is_test)
         ORDER BY source.id
    LOOP
        IF storage_v2_can_access_source(v_source.id, 'read') THEN
            v_results := v_results || jsonb_build_array(jsonb_build_object(
                'source_id', v_source.id,
                'value', storage_v2_intelligence_command(
                    v_source.id, 'current', p_command, p_query
                )
            ));
        END IF;
    END LOOP;
    RETURN jsonb_build_object(
        'read_path', 'storage_v2_active',
        'activation_manifest_sha256', p_manifest_sha256,
        'command', p_command,
        'source_count', jsonb_array_length(v_results),
        'results', v_results
    );
END
$$;
