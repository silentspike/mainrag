-- Migration 115: actor-scoped global/source notes and lossless legacy retention.
-- A free-form created_by label is never treated as an authenticated owner.
-- Installing this migration neither imports nor deletes legacy records.

CREATE TABLE IF NOT EXISTS storage_v2_intelligence_note (
    id BIGINT GENERATED ALWAYS AS IDENTITY (MAXVALUE 4611686018427387903) PRIMARY KEY,
    legacy_id BIGINT UNIQUE CHECK (legacy_id > 0),
    source_id BIGINT REFERENCES logical_source(id) ON DELETE RESTRICT,
    owner_id UUID,
    domain_profile TEXT,
    concept TEXT NOT NULL,
    path_description TEXT NOT NULL,
    reason TEXT NOT NULL,
    symbols JSONB,
    severity TEXT NOT NULL,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbols_namespace TEXT NOT NULL CHECK (symbols_namespace IN
        ('user_symbol_reference','legacy_symbol_reference')),
    legacy_record JSONB,
    legacy_record_sha256 BYTEA CHECK (octet_length(legacy_record_sha256)=32),
    CHECK ((legacy_id IS NOT NULL AND owner_id IS NULL AND legacy_record IS NOT NULL
            AND legacy_record_sha256 IS NOT NULL AND symbols_namespace='legacy_symbol_reference')
        OR (legacy_id IS NULL AND owner_id IS NOT NULL AND legacy_record IS NULL
            AND legacy_record_sha256 IS NULL AND symbols_namespace='user_symbol_reference'))
);
CREATE INDEX IF NOT EXISTS idx_storage_v2_note_concept ON storage_v2_intelligence_note
    USING GIN (to_tsvector('simple',concept));
CREATE INDEX IF NOT EXISTS idx_storage_v2_note_recent ON storage_v2_intelligence_note
    (created_at DESC,id DESC);
ALTER TABLE storage_v2_intelligence_note ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS storage_v2_note_isolation ON storage_v2_intelligence_note;
CREATE POLICY storage_v2_note_isolation ON storage_v2_intelligence_note FOR SELECT USING (
    CASE WHEN source_id IS NOT NULL THEN storage_v2_can_access_source(source_id,'read')
         ELSE storage_v2_is_admin() OR owner_id=NULLIF(current_setting('app.user_id',true),'')::UUID END
);
REVOKE ALL ON storage_v2_intelligence_note FROM PUBLIC;
GRANT SELECT ON storage_v2_intelligence_note TO mainrag;

CREATE OR REPLACE FUNCTION storage_v2_validate_intelligence_note(p_record JSONB,p_legacy BOOLEAN)
RETURNS VOID LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE v_key TEXT;
BEGIN
    IF p_record IS NULL OR jsonb_typeof(p_record)<>'object' OR p_legacy IS NULL THEN
        RAISE EXCEPTION 'note object required' USING ERRCODE='22023';
    END IF;
    FOREACH v_key IN ARRAY ARRAY['concept','path_description','reason','severity'] LOOP
        IF jsonb_typeof(p_record->v_key) IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'note text field required: %',v_key USING ERRCODE='22023';
        END IF;
    END LOOP;
    FOREACH v_key IN ARRAY ARRAY['created_by','domain_profile'] LOOP
        IF p_record?v_key AND jsonb_typeof(p_record->v_key) NOT IN ('string','null') THEN
            RAISE EXCEPTION 'invalid optional note text field' USING ERRCODE='22023';
        END IF;
    END LOOP;
    IF p_record->'source_id' IS NOT NULL AND p_record->'source_id'<>'null'::JSONB
       AND (jsonb_typeof(p_record->'source_id')<>'number'
            OR (p_record->>'source_id')!~'^[0-9]{1,19}$'
            OR (p_record->>'source_id')::NUMERIC NOT BETWEEN 1 AND 9223372036854775807) THEN
        RAISE EXCEPTION 'invalid note source ID' USING ERRCODE='22023';
    END IF;
    IF p_legacy AND (jsonb_typeof(p_record->'id') IS DISTINCT FROM 'number'
        OR (p_record->>'id')!~'^[0-9]{1,19}$'
        OR (p_record->>'id')::NUMERIC NOT BETWEEN 1 AND 9223372036854775807
        OR jsonb_typeof(p_record->'created_at') IS DISTINCT FROM 'string') THEN
        RAISE EXCEPTION 'legacy note identity and timestamp required' USING ERRCODE='22023';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION storage_v2_create_intelligence_note(p_manifest TEXT,p_record JSONB)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE v_actor UUID; v_source BIGINT; v_id BIGINT;
BEGIN
    PERFORM storage_v2_require_complete_active_set(p_manifest);
    v_actor:=NULLIF(current_setting('app.user_id',true),'')::UUID;
    IF v_actor IS NULL THEN RAISE EXCEPTION 'note actor required' USING ERRCODE='42501'; END IF;
    PERFORM storage_v2_validate_intelligence_note(p_record,false);
    IF octet_length(p_record::TEXT)>1048576 THEN
        RAISE EXCEPTION 'note exceeds byte limit' USING ERRCODE='22023';
    END IF;
    v_source:=(p_record->>'source_id')::BIGINT;
    IF v_source IS NOT NULL THEN
        IF NOT storage_v2_can_access_source(v_source,'write') THEN
            RAISE EXCEPTION 'note source write access denied' USING ERRCODE='42501';
        END IF;
        PERFORM storage_v2_require_test_scope(v_source,false);
    END IF;
    INSERT INTO storage_v2_intelligence_note(source_id,owner_id,domain_profile,concept,
        path_description,reason,symbols,severity,created_by,symbols_namespace)
    VALUES(v_source,v_actor,p_record->>'domain_profile',p_record->>'concept',
        p_record->>'path_description',p_record->>'reason',p_record->'symbols',
        p_record->>'severity',p_record->>'created_by','user_symbol_reference') RETURNING id INTO v_id;
    -- Positive IDs retain the legacy API identity; native stores use disjoint
    -- negative namespaces (odd notes, even source evidence from migration 033).
    RETURN -(2*v_id+1);
END $$;

CREATE OR REPLACE FUNCTION storage_v2_import_intelligence_notes(p_records JSONB,p_sha256 TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE v_record JSONB; v_hash BYTEA; v_old_hash BYTEA; v_source BIGINT;
    v_imported BIGINT:=0; v_existing BIGINT:=0;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'note import requires administrator authority' USING ERRCODE='42501';
    END IF;
    IF p_records IS NULL OR jsonb_typeof(p_records)<>'array' OR jsonb_array_length(p_records)>1000
       OR octet_length(p_records::TEXT)>16777216 OR p_sha256 IS NULL
       OR p_sha256!~'^[0-9a-f]{64}$'
       OR encode(digest(convert_to(p_records::TEXT,'UTF8'),'sha256'),'hex')<>p_sha256 THEN
        RAISE EXCEPTION 'bounded note batch and matching canonical digest required' USING ERRCODE='22023';
    END IF;
    IF (SELECT count(*)<>count(DISTINCT value->>'id') FROM jsonb_array_elements(p_records)) THEN
        RAISE EXCEPTION 'duplicate note batch identity' USING ERRCODE='22023';
    END IF;
    FOR v_record IN SELECT value FROM jsonb_array_elements(p_records) LOOP
        PERFORM storage_v2_validate_intelligence_note(v_record,true);
        v_source:=(v_record->>'source_id')::BIGINT;
        IF v_source IS NOT NULL AND NOT EXISTS(SELECT 1 FROM logical_source WHERE id=v_source) THEN
            RAISE EXCEPTION 'legacy note source is not mapped' USING ERRCODE='23503';
        END IF;
        v_hash:=digest(convert_to(v_record::TEXT,'UTF8'),'sha256');
        INSERT INTO storage_v2_intelligence_note(legacy_id,source_id,domain_profile,concept,
            path_description,reason,symbols,severity,created_by,created_at,symbols_namespace,
            legacy_record,legacy_record_sha256)
        VALUES((v_record->>'id')::BIGINT,v_source,v_record->>'domain_profile',v_record->>'concept',
            v_record->>'path_description',v_record->>'reason',v_record->'symbols',
            v_record->>'severity',v_record->>'created_by',(v_record->>'created_at')::TIMESTAMPTZ,
            'legacy_symbol_reference',v_record,v_hash)
        ON CONFLICT(legacy_id) DO NOTHING;
        IF FOUND THEN v_imported:=v_imported+1;
        ELSE
            SELECT legacy_record_sha256 INTO v_old_hash FROM storage_v2_intelligence_note
                WHERE legacy_id=(v_record->>'id')::BIGINT;
            IF v_hash IS DISTINCT FROM v_old_hash THEN
                RAISE EXCEPTION 'legacy note import drift' USING ERRCODE='23514';
            END IF;
            v_existing:=v_existing+1;
        END IF;
    END LOOP;
    RETURN jsonb_build_object('imported',v_imported,'existing',v_existing,'batch_sha256',p_sha256);
END $$;

CREATE OR REPLACE FUNCTION storage_v2_search_intelligence_notes(
    p_manifest TEXT,p_concept TEXT,p_source BIGINT DEFAULT NULL,p_limit BIGINT DEFAULT 20
) RETURNS JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE v_result JSONB; v_query TSQUERY;
BEGIN
    PERFORM storage_v2_require_complete_active_set(p_manifest);
    IF NULLIF(current_setting('app.user_id',true),'') IS NULL THEN
        RAISE EXCEPTION 'note actor required' USING ERRCODE='42501';
    END IF;
    IF p_concept IS NULL OR octet_length(p_concept)>8192 OR p_limit IS NULL OR p_limit<1 OR p_limit>200 THEN
        RAISE EXCEPTION 'note query and limit from 1 to 200 required' USING ERRCODE='22023';
    END IF;
    IF p_source IS NOT NULL THEN
        IF NOT storage_v2_can_access_source(p_source,'read') THEN
            RAISE EXCEPTION 'note source read access denied' USING ERRCODE='42501';
        END IF;
        PERFORM storage_v2_require_test_scope(p_source,false);
    END IF;
    v_query:=websearch_to_tsquery('simple',p_concept);
    IF EXISTS(SELECT 1 FROM storage_v2_negative_evidence n JOIN sources s ON s.id=n.source_id
        WHERE n.id>4611686018427387903 AND NOT s.is_test
          AND (p_source IS NULL OR n.source_id=p_source)
          AND storage_v2_can_access_source(n.source_id,'read')
          AND to_tsvector('simple',n.concept)@@v_query) THEN
        RAISE EXCEPTION 'source evidence identity exceeds native API range' USING ERRCODE='22003';
    END IF;
    SELECT COALESCE(jsonb_agg(value ORDER BY created_at DESC,id DESC),'[]'::JSONB) INTO v_result
    FROM (
        SELECT * FROM (
            (SELECT COALESCE(n.legacy_id,-(2*n.id+1)) id,n.created_at,jsonb_build_object(
                'id',COALESCE(n.legacy_id,-(2*n.id+1)),'concept',n.concept,
                'path_description',n.path_description,'reason',n.reason,'symbols',n.symbols,
                'severity',n.severity,'created_by',n.created_by,'domain_profile',n.domain_profile,
                'read_provenance',jsonb_build_object('read_path','storage_v2_active',
                    'source_id',n.source_id,'symbols_namespace',n.symbols_namespace,
                    'legacy_record_sha256',encode(n.legacy_record_sha256,'hex'),
                    'identity_namespace','intelligence_note')) value
             FROM storage_v2_intelligence_note n
             LEFT JOIN sources s ON s.id=n.source_id
             WHERE to_tsvector('simple',n.concept)@@v_query
               AND (p_source IS NULL OR n.source_id=p_source)
               AND CASE WHEN n.source_id IS NULL THEN storage_v2_is_admin()
                         OR n.owner_id=NULLIF(current_setting('app.user_id',true),'')::UUID
                        ELSE NOT COALESCE(s.is_test,true) AND storage_v2_can_access_source(n.source_id,'read') END
             ORDER BY n.created_at DESC,COALESCE(n.legacy_id,-(2*n.id+1)) DESC LIMIT p_limit)
            UNION ALL
            (SELECT -(2*n.id),n.created_at,jsonb_build_object('id',-(2*n.id),'concept',n.concept,
                'path_description',n.path_description,'reason',n.reason,'symbols',n.symbol_keys,
                'severity',n.severity,'created_by',n.created_by,'domain_profile',NULL,
                'read_provenance',jsonb_build_object('read_path','storage_v2_active','source_id',n.source_id,
                    'symbols_namespace','storage_v2_symbol_key','identity_namespace','source_negative_evidence'))
             FROM storage_v2_negative_evidence n JOIN sources s ON s.id=n.source_id
             WHERE to_tsvector('simple',n.concept)@@v_query AND NOT s.is_test
               AND (p_source IS NULL OR n.source_id=p_source)
               AND storage_v2_can_access_source(n.source_id,'read')
             ORDER BY n.created_at DESC,n.id ASC LIMIT p_limit)
        ) candidates ORDER BY created_at DESC,id DESC LIMIT p_limit
    ) bounded;
    RETURN v_result;
END $$;

CREATE OR REPLACE FUNCTION storage_v2_export_intelligence_notes()
RETURNS SETOF JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'protected note export requires administrator authority' USING ERRCODE='42501';
    END IF;
    -- Stream complete records, including unknown legacy fields and original
    -- JSON nulls. Search limits never truncate a protected retention export.
    RETURN QUERY SELECT to_jsonb(n) FROM storage_v2_intelligence_note n ORDER BY n.id;
END $$;

DO $$
DECLARE v_owner TEXT; v_signature TEXT;
BEGIN
    SELECT pg_get_userbyid(proowner) INTO v_owner FROM pg_proc
        WHERE oid='storage_v2_intelligence_command(bigint,text,text,jsonb)'::REGPROCEDURE;
    EXECUTE format('ALTER TABLE storage_v2_intelligence_note OWNER TO %I',v_owner);
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_validate_intelligence_note(jsonb,boolean)',
        'storage_v2_create_intelligence_note(text,jsonb)',
        'storage_v2_import_intelligence_notes(jsonb,text)',
        'storage_v2_search_intelligence_notes(text,text,bigint,bigint)',
        'storage_v2_export_intelligence_notes()'
    ] LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO %I',v_signature,v_owner);
        EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC',v_signature);
        IF v_signature<>'storage_v2_validate_intelligence_note(jsonb,boolean)' THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO mainrag',v_signature);
        END IF;
    END LOOP;
END $$;
