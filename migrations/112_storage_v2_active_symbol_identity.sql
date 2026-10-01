-- Migration 112: checked active ID resolution and one-snapshot callgraph reads.
-- Negative occurrence/item IDs cannot alias positive legacy IDs. Installation
-- leaves all pointers, legacy rows, and exports unchanged.

CREATE OR REPLACE FUNCTION storage_v2_active_symbol_callgraph(
    p_manifest_sha256 TEXT,
    p_symbol_id BIGINT,
    p_limit BIGINT DEFAULT 200
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off
AS $$
DECLARE
    v_source BIGINT;
    v_symbol JSONB;
    v_callers JSONB;
    v_callees JSONB;
BEGIN
    PERFORM storage_v2_require_complete_active_set(p_manifest_sha256);
    IF p_symbol_id IS NULL OR p_symbol_id>=0 OR p_symbol_id='-9223372036854775808'::BIGINT
       OR p_limit IS NULL OR p_limit<1 OR p_limit>200 THEN
        RAISE EXCEPTION 'negative active symbol ID and limit from 1 to 200 required' USING ERRCODE='22023';
    END IF;
    SELECT source_id INTO v_source FROM storage_v2_symbol_occurrence
     WHERE id=-p_symbol_id AND storage_v2_can_access_source(source_id,'read');
    IF NOT FOUND THEN RAISE EXCEPTION 'active symbol not found' USING ERRCODE='P0002'; END IF;
    PERFORM storage_v2_require_test_scope(v_source,false);
    v_symbol := storage_v2_symbol_command(v_source,'current','symbols',
        jsonb_build_object('occurrence_id',-p_symbol_id,'limit',1))->0;
    IF v_symbol IS NULL THEN RAISE EXCEPTION 'active symbol not found' USING ERRCODE='P0002'; END IF;
    v_callers := storage_v2_symbol_command(v_source,'current','callers',
        jsonb_build_object('callee_symbol_key',v_symbol->>'symbol_key','limit',p_limit));
    v_callees := storage_v2_symbol_command(v_source,'current','callees',
        jsonb_build_object('caller_occurrence_id',-p_symbol_id,'limit',p_limit));
    RETURN jsonb_build_object(
        'symbol',v_symbol,
        'callers',COALESCE((SELECT jsonb_agg(value ORDER BY value->>'name',value::TEXT) FROM (
            SELECT DISTINCT jsonb_build_object('symbol_id',row->'caller_id',
                'name',row->'caller_name','symbol_type',row->'caller_type',
                'file_path',row->'file_path','line_start',row->'line') value
            FROM jsonb_array_elements(v_callers) row
        ) nodes),'[]'::JSONB),
        'callees',COALESCE((SELECT jsonb_agg(name ORDER BY name) FROM (
            SELECT DISTINCT row->>'callee_name' name FROM jsonb_array_elements(v_callees) row
        ) names),'[]'::JSONB),
        'callers_complete',jsonb_array_length(v_callers)<p_limit,
        'callees_complete',jsonb_array_length(v_callees)<p_limit,
        'read_path','storage_v2_active','source_id',v_source,
        'generation_seq',v_symbol->'generation_seq',
        'call_evidence',v_callees
    );
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_active_file_symbols(
    p_manifest_sha256 TEXT,
    p_file_id BIGINT,
    p_limit BIGINT DEFAULT 100
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off
AS $$
DECLARE v_source BIGINT; v_symbols JSONB;
BEGIN
    PERFORM storage_v2_require_complete_active_set(p_manifest_sha256);
    IF p_file_id IS NULL OR p_file_id>=0 OR p_file_id='-9223372036854775808'::BIGINT
       OR p_limit IS NULL OR p_limit<1 OR p_limit>200 THEN
        RAISE EXCEPTION 'negative active file ID and limit from 1 to 200 required' USING ERRCODE='22023';
    END IF;
    SELECT item.source_id INTO v_source FROM source_item item
     JOIN logical_source source ON source.id=item.source_id
     JOIN source_generation generation ON generation.id=source.active_generation_id
     JOIN generation_item_version membership ON membership.source_id=item.source_id
      AND membership.source_item_id=item.id
      AND membership.valid_from_seq<=generation.generation_seq
      AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq>generation.generation_seq)
     WHERE item.id=-p_file_id AND storage_v2_can_access_source(item.source_id,'read');
    IF NOT FOUND THEN RAISE EXCEPTION 'active file not found' USING ERRCODE='P0002'; END IF;
    PERFORM storage_v2_require_test_scope(v_source,false);
    v_symbols := storage_v2_symbol_command(v_source,'current','symbols',
        jsonb_build_object('file_item_id',-p_file_id,'limit',p_limit));
    RETURN COALESCE((SELECT jsonb_agg(row ORDER BY (row->>'line_start')::BIGINT,row->>'name',row->>'id')
                    FROM jsonb_array_elements(v_symbols) row),'[]'::JSONB);
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_active_callee_names(
    p_manifest_sha256 TEXT,p_name TEXT,p_source_id BIGINT DEFAULT NULL,p_limit BIGINT DEFAULT 100
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off
AS $$
DECLARE v_source RECORD; v_names JSONB := '[]'::JSONB; v_local JSONB;
BEGIN
    PERFORM storage_v2_require_complete_active_set(p_manifest_sha256);
    IF p_name IS NULL OR p_name='' OR p_limit IS NULL OR p_limit<1 OR p_limit>200 THEN
        RAISE EXCEPTION 'callee name and limit from 1 to 200 required' USING ERRCODE='22023';
    END IF;
    IF p_source_id IS NOT NULL THEN PERFORM storage_v2_require_test_scope(p_source_id,false); END IF;
    FOR v_source IN SELECT id FROM sources WHERE NOT is_test
      AND (p_source_id IS NULL OR id=p_source_id) ORDER BY id
    LOOP
        IF storage_v2_can_access_source(v_source.id,'read') THEN
            v_local := storage_v2_symbol_command(v_source.id,'current','callee_names',
                jsonb_build_object('name',p_name,'exact_name',true,'limit',p_limit));
            -- Global Top-L distinct names are contained in the union of each
            -- source's Top-L. Keep only L after every merge, without scanning
            -- or aggregating a complete call collection.
            SELECT COALESCE(jsonb_agg(name ORDER BY name),'[]'::JSONB) INTO v_names FROM (
                SELECT DISTINCT value name FROM jsonb_array_elements_text(v_names||v_local)
                 ORDER BY name LIMIT p_limit
            ) merged;
        END IF;
    END LOOP;
    RETURN v_names;
END
$$;

DO $$
DECLARE v_owner TEXT; v_signature TEXT;
BEGIN
 SELECT pg_get_userbyid(proowner) INTO v_owner FROM pg_proc
  WHERE oid='storage_v2_intelligence_command(bigint,text,text,jsonb)'::REGPROCEDURE;
 FOREACH v_signature IN ARRAY ARRAY[
  'storage_v2_active_symbol_callgraph(text,bigint,bigint)',
  'storage_v2_active_file_symbols(text,bigint,bigint)',
  'storage_v2_active_callee_names(text,text,bigint,bigint)'
 ] LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO %I',v_signature,v_owner);
  EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC',v_signature);
  EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO mainrag',v_signature);
 END LOOP;
END
$$;
