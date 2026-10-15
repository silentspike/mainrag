-- Proposed migration: one source-authorized native body read boundary.
-- No tables, generation membership, search projection or existing routines change.
BEGIN;
CREATE FUNCTION storage_v2_complete_file_components(
    p_source_id BIGINT,p_generation_selector TEXT,p_complete_file_id TEXT,
    p_after_source_offset BIGINT,p_include_test BOOLEAN DEFAULT FALSE
) RETURNS TABLE(
    occurrence_id BIGINT,source_offset BIGINT,byte_overlap BIGINT,
    source_path TEXT,locator JSONB,body_id BIGINT,digest_algorithm TEXT,
    digest BYTEA,logical_length BIGINT,inline_bytes BYTEA,pack_id UUID,
    storage_key TEXT,stored_bytes BIGINT,pack_status TEXT,ordinal BIGINT,
    pack_offset BIGINT,stored_length BIGINT,codec TEXT,entry_digest BYTEA,
    dictionary_id BIGINT,dictionary_digest BYTEA,dictionary_bytes BYTEA
) LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE v_resolved JSONB;
BEGIN
    -- Check source authority before looking up any mapping or storage identity.
    IF p_source_id IS NULL OR p_source_id<=0
       OR storage_v2_can_access_source(p_source_id,'read') IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'authorized source read required' USING ERRCODE='42501';
    END IF;
    IF p_complete_file_id IS NULL OR octet_length(p_complete_file_id)>512
       OR p_complete_file_id !~ '^legacy-file-v1:[1-9][0-9]*:[1-9][0-9]*:(0|[1-9][0-9]*):(0|[1-9][0-9]*):[a-f0-9]{64}$'
       OR split_part(p_complete_file_id,':',2)<>p_source_id::TEXT
       OR p_include_test IS NULL OR p_after_source_offset IS NULL OR p_after_source_offset < -1 THEN
        RAISE EXCEPTION 'bounded canonical complete file selector required';
    END IF;
    PERFORM storage_v2_require_test_scope(p_source_id,p_include_test);
    -- Existing generation policy, retained history and source isolation remain
    -- authoritative. Arbitrary occurrence/body IDs are never accepted as input.
    v_resolved:=storage_v2_resolve_legacy_hit(p_source_id,p_generation_selector,p_complete_file_id);
    IF jsonb_array_length(v_resolved->'targets') NOT BETWEEN 1 AND 2048 THEN
        RAISE EXCEPTION 'complete file mapping is unresolved or exceeds bounds';
    END IF;
    RETURN QUERY
    WITH allowed AS MATERIALIZED (
        SELECT (target->>'occurrence_id')::BIGINT AS id
        FROM jsonb_array_elements(v_resolved->'targets') target
    ), selected AS MATERIALIZED (
        SELECT m.occurrence_id,m.source_offset,m.byte_overlap,o.source_path,o.locator,
            coalesce(a.raw_body_id,n.body_id) AS body_id
        FROM allowed v JOIN legacy_hit_mapping m ON m.occurrence_id=v.id
            AND m.old_hit_id=p_complete_file_id
        JOIN occurrence o ON o.id=m.occurrence_id AND o.source_id=p_source_id
        JOIN artifact_version a ON a.id=o.artifact_version_id AND a.source_id=p_source_id
        LEFT JOIN content_node n ON n.id=a.content_root_node_id
        WHERE m.source_offset>p_after_source_offset
        ORDER BY m.source_offset,m.occurrence_id LIMIT 32
    )
    SELECT s.occurrence_id,s.source_offset,s.byte_overlap,s.source_path,s.locator,
        b.id,b.digest_algorithm,b.digest,b.logical_length,b.inline_bytes,b.pack_id,
        p.storage_key,p.stored_bytes,p.status::TEXT,e.ordinal,e.pack_offset,e.stored_length,
        e.codec::TEXT,e.entry_digest,d.id,d.digest,d.dictionary_bytes
    FROM selected s JOIN content_body b ON b.id=s.body_id
    LEFT JOIN content_pack p ON p.id=b.pack_id
    LEFT JOIN content_pack_entry e ON e.pack_id=b.pack_id AND e.body_id=b.id
    LEFT JOIN content_dictionary d ON d.id=e.dictionary_id
    ORDER BY s.source_offset,s.occurrence_id;
END $$;
DO $$
DECLARE v_owner NAME;v_table REGCLASS;
BEGIN
    SELECT pg_get_userbyid(relowner) INTO STRICT v_owner FROM pg_class
        WHERE oid='public.content_body'::REGCLASS;
    FOREACH v_table IN ARRAY ARRAY[
        'content_body'::REGCLASS,'content_pack'::REGCLASS,'content_pack_entry'::REGCLASS,
        'content_dictionary'::REGCLASS,'occurrence'::REGCLASS,'artifact_version'::REGCLASS,
        'content_node'::REGCLASS,'legacy_hit_mapping'::REGCLASS
    ] LOOP
        IF NOT has_table_privilege(v_owner,v_table,'SELECT') OR EXISTS(
            SELECT 1 FROM pg_class c WHERE c.oid=v_table AND c.relrowsecurity
                AND (c.relforcerowsecurity OR NOT pg_has_role(v_owner,c.relowner,'USAGE'))
                AND NOT EXISTS(SELECT 1 FROM pg_roles r WHERE r.rolname=v_owner
                    AND (r.rolsuper OR r.rolbypassrls))) THEN
            RAISE EXCEPTION 'existing body owner cannot safely serve scoped storage metadata';
        END IF;
    END LOOP;
    EXECUTE format('ALTER FUNCTION storage_v2_complete_file_components(BIGINT,TEXT,TEXT,BIGINT,BOOLEAN) OWNER TO %I',v_owner);
END $$;
REVOKE ALL ON FUNCTION storage_v2_complete_file_components(BIGINT,TEXT,TEXT,BIGINT,BOOLEAN) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_complete_file_components(BIGINT,TEXT,TEXT,BIGINT,BOOLEAN) TO mainrag;
COMMIT;
