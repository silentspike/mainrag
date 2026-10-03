-- Reuse complete export proofs while their source data and exporter are unchanged.
-- Statement transition tables invalidate only affected sources, including updates
-- with unchanged record counts. No record content is retained in this cache.
BEGIN;
DO $$ BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
        RAISE EXCEPTION 'export proof installation requires the database administrator';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS storage_v2_intelligence_export_revision (
    source_id BIGINT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    revision BIGINT NOT NULL CHECK (revision >= 0)
);
CREATE TABLE IF NOT EXISTS storage_v2_intelligence_export_proof (
    generation_id BIGINT PRIMARY KEY REFERENCES source_generation(id) ON DELETE CASCADE,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    identity JSONB NOT NULL,
    public_envelope JSONB NOT NULL,
    serialized_bytes BIGINT NOT NULL CHECK (serialized_bytes >= 0)
);

CREATE OR REPLACE FUNCTION storage_v2_invalidate_intelligence_export_proof()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = off AS $$
DECLARE v_rows TEXT; v_sources TEXT;
BEGIN
    IF TG_OP='TRUNCATE' THEN
        INSERT INTO storage_v2_intelligence_export_revision(source_id,revision)
        SELECT source_id,1 FROM storage_v2_intelligence_export_proof GROUP BY source_id
        ON CONFLICT(source_id) DO UPDATE SET revision=
            storage_v2_intelligence_export_revision.revision+1;
        RETURN NULL;
    END IF;
    v_rows := CASE TG_OP
        WHEN 'INSERT' THEN 'SELECT * FROM new_export_rows'
        WHEN 'DELETE' THEN 'SELECT * FROM old_export_rows'
        ELSE 'SELECT * FROM new_export_rows UNION ALL SELECT * FROM old_export_rows'
    END;
    IF TG_ARGV[0] = 'source_id' THEN
        v_sources := 'SELECT DISTINCT source_id FROM (' || v_rows || ') changed';
    ELSIF TG_ARGV[0] = 'symbol_occurrence_id' THEN
        v_sources := 'SELECT DISTINCT occurrence.source_id FROM (' || v_rows ||
            ') changed JOIN public.storage_v2_symbol_occurrence occurrence ' ||
            'ON occurrence.id=changed.symbol_occurrence_id';
    ELSE
        RAISE EXCEPTION 'unknown export invalidation scope';
    END IF;
    EXECUTE 'INSERT INTO public.storage_v2_intelligence_export_revision(source_id,revision) '
        || 'SELECT affected.source_id,1 FROM (' || v_sources || ') affected '
        || 'JOIN public.sources source ON source.id=affected.source_id '
        || 'ON CONFLICT (source_id) DO UPDATE SET revision='
        || 'storage_v2_intelligence_export_revision.revision+1';
    RETURN NULL;
END $$;

DO $$
DECLARE v_table TEXT; v_scope TEXT; v_operation TEXT; v_transition TEXT;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'storage_v2_intelligence_profile','storage_v2_symbol',
        'storage_v2_symbol_occurrence','storage_v2_symbol_card',
        'storage_v2_symbol_annotation','storage_v2_intelligence_entity',
        'storage_v2_intelligence_relation','storage_v2_call_edge',
        'storage_v2_unresolved_call','storage_v2_negative_evidence',
        'source_item','artifact_version','generation_item_version'
    ] LOOP
        v_scope := CASE WHEN v_table='storage_v2_symbol_card'
            THEN 'symbol_occurrence_id' ELSE 'source_id' END;
        FOREACH v_operation IN ARRAY ARRAY['INSERT','UPDATE','DELETE'] LOOP
            v_transition := CASE v_operation
                WHEN 'INSERT' THEN 'NEW TABLE AS new_export_rows'
                WHEN 'DELETE' THEN 'OLD TABLE AS old_export_rows'
                ELSE 'OLD TABLE AS old_export_rows NEW TABLE AS new_export_rows' END;
            EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I',
                'export_proof_' || lower(v_operation),v_table);
            EXECUTE format('CREATE TRIGGER %I AFTER %s ON %I REFERENCING %s '
                || 'FOR EACH STATEMENT EXECUTE FUNCTION '
                || 'public.storage_v2_invalidate_intelligence_export_proof(%L)',
                'export_proof_' || lower(v_operation),v_operation,v_table,v_transition,v_scope);
        END LOOP;
        EXECUTE format('DROP TRIGGER IF EXISTS export_proof_truncate ON %I',v_table);
        EXECUTE format('CREATE TRIGGER export_proof_truncate AFTER TRUNCATE ON %I '
            || 'FOR EACH STATEMENT EXECUTE FUNCTION '
            || 'public.storage_v2_invalidate_intelligence_export_proof(%L)',v_table,v_scope);
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION storage_v2_intelligence_export_proof_identity(
    p_source_id BIGINT, p_generation TEXT
) RETURNS JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = off AS $$
DECLARE v_generation source_generation;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id,'read') THEN
        RAISE EXCEPTION 'intelligence export is not authorized' USING ERRCODE='42501';
    END IF;
    v_generation := storage_v2_resolve_generation(p_source_id,p_generation);
    IF v_generation.status::TEXT NOT IN ('verified','release_candidate','active','superseded')
       OR v_generation.verification_manifest_sha256 IS NULL THEN
        RAISE EXCEPTION 'complete verified generation required for an export proof';
    END IF;
    RETURN jsonb_build_object(
        'schema_version','mainrag.intelligence-export-proof.identity.v1',
        'digest_protocol','native-jsonb-ordered-collections-v1',
        'postgres_version',current_setting('server_version_num'),
        'encoding',current_setting('server_encoding'),
        'collation_version',(SELECT pg_database_collation_actual_version(oid)
            FROM pg_database WHERE datname=current_database()),
        'source_id',p_source_id,'generation_id',v_generation.id,
        'generation_seq',v_generation.generation_seq,
        'verification_manifest_sha256',v_generation.verification_manifest_sha256,
        'revision',COALESCE((SELECT revision FROM storage_v2_intelligence_export_revision
            WHERE source_id=p_source_id),0),
        'exporter_sha256',encode(sha256(convert_to(
            pg_get_functiondef('storage_v2_intelligence_export_records(bigint,text,text)'::regprocedure)
            || pg_get_functiondef('storage_v2_export_intelligence(bigint,text,text)'::regprocedure)
            || pg_get_functiondef('storage_v2_resolve_generation(bigint,text)'::regprocedure)
            || pg_get_functiondef('storage_v2_hash_parts(text,bytea[])'::regprocedure),
            'UTF8')),'hex')
    );
END $$;

CREATE OR REPLACE FUNCTION storage_v2_cached_intelligence_export_proof(
    p_source_id BIGINT, p_generation TEXT
) RETURNS JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = off AS $$
DECLARE v_identity JSONB; v_result JSONB;
BEGIN
    v_identity := storage_v2_intelligence_export_proof_identity(p_source_id,p_generation);
    SELECT jsonb_build_object('identity',proof.identity,'public_envelope',proof.public_envelope,
        'serialized_bytes',proof.serialized_bytes) INTO v_result
      FROM storage_v2_intelligence_export_proof proof
     WHERE proof.generation_id=(v_identity->>'generation_id')::BIGINT
       AND proof.source_id=p_source_id AND proof.identity=v_identity;
    RETURN jsonb_build_object('identity',v_identity,'proof',v_result);
END $$;

CREATE OR REPLACE FUNCTION storage_v2_store_intelligence_export_proof(
    p_source_id BIGINT, p_generation TEXT, p_identity JSONB,
    p_public_envelope JSONB, p_serialized_bytes BIGINT
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = off AS $$
DECLARE v_identity JSONB;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'export proof writes require administrator authority' USING ERRCODE='42501';
    END IF;
    v_identity := storage_v2_intelligence_export_proof_identity(p_source_id,p_generation);
    IF p_identity IS DISTINCT FROM v_identity THEN RETURN FALSE; END IF;
    IF p_serialized_bytes IS NULL OR p_serialized_bytes<0
       OR p_public_envelope->>'schema_version' IS DISTINCT FROM 'mainrag.storage-v2-intelligence-export.v1'
       OR p_public_envelope->>'redaction' IS DISTINCT FROM 'public'
       OR p_public_envelope->>'generation_seq' IS DISTINCT FROM v_identity->>'generation_seq'
       OR p_public_envelope->>'source_ref' IS DISTINCT FROM encode(storage_v2_hash_parts(
           'mainrag.export-source.v1',ARRAY[int8send(p_source_id)]),'hex')
       OR p_public_envelope->>'payload_sha256' IS DISTINCT FROM encode(sha256(convert_to(
           (p_public_envelope->'payload')::TEXT,'UTF8')),'hex')
       OR COALESCE(p_public_envelope->'payload'->>'protected_payload_sha256' !~ '^[0-9a-f]{64}$',TRUE)
       OR jsonb_typeof(p_public_envelope->'payload'->'record_counts') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'complete public export envelope required';
    END IF;
    IF (SELECT array_agg(key ORDER BY key) FROM jsonb_each(
            p_public_envelope->'payload'->'record_counts')) IS DISTINCT FROM ARRAY[
            'annotations','call_edges','cards','entities','negative_evidence',
            'profiles','relations','unresolved_calls']::TEXT[]
       OR EXISTS (SELECT 1 FROM jsonb_each(p_public_envelope->'payload'->'record_counts')
            WHERE jsonb_typeof(value)<>'number' OR value::TEXT !~ '^(0|[1-9][0-9]*)$') THEN
        RAISE EXCEPTION 'complete nonnegative export collection counts required';
    END IF;
    INSERT INTO storage_v2_intelligence_export_proof(
        generation_id,source_id,identity,public_envelope,serialized_bytes
    ) VALUES ((v_identity->>'generation_id')::BIGINT,p_source_id,v_identity,
        p_public_envelope,p_serialized_bytes)
    ON CONFLICT(generation_id) DO UPDATE SET identity=EXCLUDED.identity,
        public_envelope=EXCLUDED.public_envelope,serialized_bytes=EXCLUDED.serialized_bytes;
    RETURN TRUE;
END $$;

REVOKE ALL ON storage_v2_intelligence_export_revision,storage_v2_intelligence_export_proof FROM PUBLIC,mainrag;
REVOKE ALL ON FUNCTION storage_v2_invalidate_intelligence_export_proof() FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_intelligence_export_proof_identity(BIGINT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_cached_intelligence_export_proof(BIGINT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_store_intelligence_export_proof(BIGINT,TEXT,JSONB,JSONB,BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_intelligence_export_proof_identity(BIGINT,TEXT),
    storage_v2_cached_intelligence_export_proof(BIGINT,TEXT),
    storage_v2_store_intelligence_export_proof(BIGINT,TEXT,JSONB,JSONB,BIGINT) TO mainrag;
COMMIT;
