-- Retain authorized native hit resolution and atomically bind bounded mapping batches.
BEGIN;

-- Independent of legacy files/chunks and ordinary generation membership.
-- These records retain only old hit content that has no proven current target.
CREATE TABLE storage_v2_legacy_hit_history (
    old_hit_id TEXT PRIMARY KEY CHECK(octet_length(old_hit_id) BETWEEN 1 AND 512),
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    occurrence_id BIGINT NOT NULL REFERENCES occurrence(id) ON DELETE RESTRICT,
    body_id BIGINT NOT NULL REFERENCES content_body(id) ON DELETE RESTRICT,
    proof JSONB NOT NULL CHECK(jsonb_typeof(proof)='object'),
    UNIQUE(occurrence_id),
    FOREIGN KEY(occurrence_id,source_id) REFERENCES occurrence(id,source_id) ON DELETE RESTRICT
);
ALTER TABLE storage_v2_legacy_hit_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY legacy_hit_history_isolation ON storage_v2_legacy_hit_history
    USING(storage_v2_can_access_source(source_id,'read'));
CREATE TRIGGER legacy_hit_history_immutable BEFORE UPDATE OR DELETE
    ON storage_v2_legacy_hit_history FOR EACH ROW
    EXECUTE FUNCTION storage_v2_reject_graph_mutation();
REVOKE ALL ON storage_v2_legacy_hit_history FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_legacy_hit_history TO mainrag;

-- The bootstrap cursor is derived from committed proofs, never from a
-- process-local counter. No FK refers to a legacy file or chunk table.
CREATE TABLE storage_v2_legacy_hit_proof (
    old_hit_id TEXT PRIMARY KEY CHECK(octet_length(old_hit_id) BETWEEN 1 AND 512),
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    generation_id BIGINT NOT NULL,
    proof JSONB NOT NULL CHECK(jsonb_typeof(proof)='object'),
    mapping_sha256 TEXT NOT NULL CHECK(mapping_sha256 ~ '^[0-9a-f]{64}$'),
    FOREIGN KEY(source_id,generation_id) REFERENCES source_generation(source_id,id) ON DELETE RESTRICT
);
ALTER TABLE storage_v2_legacy_hit_proof ENABLE ROW LEVEL SECURITY;
CREATE POLICY legacy_hit_proof_isolation ON storage_v2_legacy_hit_proof
    USING(storage_v2_can_access_source(source_id,'read'));
REVOKE ALL ON storage_v2_legacy_hit_proof FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_legacy_hit_proof TO mainrag;

CREATE FUNCTION storage_v2_invalidate_legacy_hit_proof() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=on AS $$
BEGIN
    IF TG_OP IN ('INSERT','DELETE') THEN
        DELETE FROM storage_v2_legacy_hit_proof proof USING changed_hit_mappings changed
            WHERE proof.old_hit_id=changed.old_hit_id;
    ELSE
        DELETE FROM storage_v2_legacy_hit_proof proof WHERE proof.old_hit_id IN
            (SELECT old_hit_id FROM old_hit_mappings UNION SELECT old_hit_id FROM new_hit_mappings);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER legacy_hit_proof_insert AFTER INSERT ON legacy_hit_mapping
    REFERENCING NEW TABLE AS changed_hit_mappings FOR EACH STATEMENT EXECUTE FUNCTION storage_v2_invalidate_legacy_hit_proof();
CREATE TRIGGER legacy_hit_proof_delete AFTER DELETE ON legacy_hit_mapping
    REFERENCING OLD TABLE AS changed_hit_mappings FOR EACH STATEMENT EXECUTE FUNCTION storage_v2_invalidate_legacy_hit_proof();
CREATE TRIGGER legacy_hit_proof_update AFTER UPDATE ON legacy_hit_mapping
    REFERENCING OLD TABLE AS old_hit_mappings NEW TABLE AS new_hit_mappings
    FOR EACH STATEMENT EXECUTE FUNCTION storage_v2_invalidate_legacy_hit_proof();

CREATE OR REPLACE FUNCTION storage_v2_legacy_hit_mapping_state(p_old_hit_id TEXT)
RETURNS TEXT LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = off AS $$
    SELECT encode(sha256(convert_to(COALESCE(jsonb_agg(jsonb_build_object(
        'occurrence_id',occurrence_id,'ordinal',ordinal,'relation_kind',relation_kind,
        'byte_overlap',byte_overlap,'source_offset',source_offset
    ) ORDER BY ordinal),'[]'::JSONB)::TEXT,'UTF8')),'hex')
    FROM legacy_hit_mapping WHERE old_hit_id=p_old_hit_id
$$;

CREATE OR REPLACE FUNCTION storage_v2_replace_legacy_hit_mappings(
    p_source_id BIGINT, p_records JSONB
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = off AS $$
DECLARE
    v_record JSONB;
    v_ids BIGINT[];
    v_overlaps BIGINT[];
    v_offsets BIGINT[];
    v_hit TEXT;
    v_targets BIGINT := 0;
    v_result JSONB := '[]'::JSONB;
BEGIN
    IF NOT storage_v2_is_admin()
       OR NOT storage_v2_can_access_source(p_source_id,'write') THEN
        RAISE EXCEPTION 'legacy mapping batches require authorized administrator source access'
            USING ERRCODE='42501';
    END IF;
    IF p_records IS NULL OR jsonb_typeof(p_records) IS DISTINCT FROM 'array'
       OR jsonb_array_length(p_records) NOT BETWEEN 1 AND 512
       OR octet_length(p_records::TEXT)>8388608 THEN
        RAISE EXCEPTION 'bounded nonempty legacy mapping batch required';
    END IF;
    IF EXISTS(SELECT 1 FROM jsonb_array_elements(p_records) record
        WHERE jsonb_typeof(record) IS DISTINCT FROM 'object') THEN
        RAISE EXCEPTION 'legacy mapping records must be objects';
    END IF;
    IF (SELECT count(DISTINCT record->>'old_hit_id') FROM jsonb_array_elements(p_records) record)
       <>jsonb_array_length(p_records) THEN
        RAISE EXCEPTION 'legacy mapping batch hit identities must be unique';
    END IF;
    -- Validate the whole batch before acquiring locks or changing a mapping.
    FOR v_record IN SELECT value FROM jsonb_array_elements(p_records) LOOP
        IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(v_record) key)
             IS DISTINCT FROM ARRAY['byte_overlaps','expected_mapping_sha256','occurrence_ids',
                'old_hit_id','relation_kind','source_offsets']::TEXT[]
           OR jsonb_typeof(v_record->'old_hit_id') IS DISTINCT FROM 'string'
           OR octet_length(v_record->>'old_hit_id') NOT BETWEEN 1 AND 512
           OR jsonb_typeof(v_record->'expected_mapping_sha256') IS DISTINCT FROM 'string'
           OR v_record->>'expected_mapping_sha256' !~ '^[0-9a-f]{64}$'
           OR jsonb_typeof(v_record->'relation_kind') IS DISTINCT FROM 'string'
           OR v_record->>'relation_kind' NOT IN ('exact','split','merged')
           OR jsonb_typeof(v_record->'occurrence_ids') IS DISTINCT FROM 'array'
           OR jsonb_typeof(v_record->'byte_overlaps') IS DISTINCT FROM 'array'
           OR jsonb_typeof(v_record->'source_offsets') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'legacy mapping record shape differs';
        END IF;
        IF jsonb_array_length(v_record->'occurrence_ids') NOT BETWEEN 1 AND 2048
           OR jsonb_array_length(v_record->'occurrence_ids')
                <>jsonb_array_length(v_record->'byte_overlaps')
           OR jsonb_array_length(v_record->'occurrence_ids')
                <>jsonb_array_length(v_record->'source_offsets')
           OR EXISTS(SELECT 1 FROM jsonb_array_elements(v_record->'occurrence_ids') value
                WHERE jsonb_typeof(value)<>'number' OR value::TEXT !~ '^[1-9][0-9]*$')
           OR EXISTS(SELECT 1 FROM jsonb_array_elements(
                (v_record->'byte_overlaps')||(v_record->'source_offsets')) value
                WHERE jsonb_typeof(value)<>'number' OR value::TEXT !~ '^(0|[1-9][0-9]*)$') THEN
            RAISE EXCEPTION 'legacy mapping targets require bounded integer coordinates';
        END IF;
        v_targets := v_targets+jsonb_array_length(v_record->'occurrence_ids');
        IF v_targets>2048 THEN RAISE EXCEPTION 'legacy mapping batch has too many targets'; END IF;
        v_ids:=ARRAY(SELECT value::BIGINT FROM jsonb_array_elements_text(v_record->'occurrence_ids') value);
        IF cardinality(v_ids)<>(SELECT count(DISTINCT value) FROM unnest(v_ids) value)
           OR (v_record->>'relation_kind'='exact' AND cardinality(v_ids)<>1) THEN
            RAISE EXCEPTION 'legacy mapping targets must be unique and exact mappings singular';
        END IF;
        IF EXISTS(SELECT 1 FROM unnest(v_ids) target(id)
            WHERE NOT EXISTS(SELECT 1 FROM occurrence WHERE id=target.id AND source_id=p_source_id))
           OR EXISTS(SELECT 1 FROM legacy_hit_mapping mapping JOIN occurrence target
                ON target.id=mapping.occurrence_id
                WHERE mapping.old_hit_id=v_record->>'old_hit_id' AND target.source_id<>p_source_id) THEN
            RAISE EXCEPTION 'legacy mapping target source differs' USING ERRCODE='42501';
        END IF;
    END LOOP;
    -- All callers, including the original single-hit surface, share this lock.
    FOR v_hit IN SELECT record->>'old_hit_id' FROM jsonb_array_elements(p_records) record
        ORDER BY record->>'old_hit_id' COLLATE "C" LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended('mainrag.legacy-hit:'||v_hit,0));
    END LOOP;
    FOR v_record IN SELECT value FROM jsonb_array_elements(p_records) LOOP
        v_hit:=v_record->>'old_hit_id';
        IF storage_v2_legacy_hit_mapping_state(v_hit)
            IS DISTINCT FROM v_record->>'expected_mapping_sha256' THEN
            RAISE EXCEPTION 'legacy mapping state drifted before batch apply';
        END IF;
    END LOOP;
    FOR v_record IN SELECT value FROM jsonb_array_elements(p_records) LOOP
        v_ids:=ARRAY(SELECT value::BIGINT FROM jsonb_array_elements_text(v_record->'occurrence_ids') value);
        v_overlaps:=ARRAY(SELECT value::BIGINT FROM jsonb_array_elements_text(v_record->'byte_overlaps') value);
        v_offsets:=ARRAY(SELECT value::BIGINT FROM jsonb_array_elements_text(v_record->'source_offsets') value);
        PERFORM * FROM storage_v2_replace_legacy_hit_mapping(v_record->>'old_hit_id',v_ids,
            v_record->>'relation_kind',v_overlaps,v_offsets);
        v_result:=v_result||jsonb_build_array(jsonb_build_object('old_hit_id',v_record->>'old_hit_id',
            'mapping_sha256',storage_v2_legacy_hit_mapping_state(v_record->>'old_hit_id')));
    END LOOP;
    RETURN jsonb_build_object('schema_version','mainrag.storage-v2.legacy-hit-batch.v1',
        'source_id',p_source_id,'hit_count',jsonb_array_length(p_records),
        'target_count',v_targets,'mappings',v_result);
END $$;

-- Strengthen the retained single-hit entry point as well: NULL coordinates must
-- never bypass validation, and an old identity cannot migrate across sources.
CREATE OR REPLACE FUNCTION storage_v2_replace_legacy_hit_mapping(
    p_old_hit_id TEXT,p_occurrence_ids BIGINT[],p_relation_kind TEXT,
    p_byte_overlaps BIGINT[],p_source_offsets BIGINT[]
) RETURNS SETOF legacy_hit_mapping LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE v_source BIGINT;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'legacy mapping writes require administrator authority' USING ERRCODE='42501';
    END IF;
    IF p_old_hit_id IS NULL OR octet_length(p_old_hit_id) NOT BETWEEN 1 AND 512
       OR p_relation_kind IS NULL OR p_relation_kind NOT IN ('exact','split','merged')
       OR p_occurrence_ids IS NULL OR p_byte_overlaps IS NULL OR p_source_offsets IS NULL
       OR cardinality(p_occurrence_ids) NOT BETWEEN 1 AND 2048
       OR cardinality(p_occurrence_ids) IS DISTINCT FROM cardinality(p_byte_overlaps)
       OR cardinality(p_occurrence_ids) IS DISTINCT FROM cardinality(p_source_offsets)
       OR EXISTS(SELECT 1 FROM unnest(p_occurrence_ids,p_byte_overlaps,p_source_offsets)
            target(id,overlap,offset_value) WHERE id IS NULL OR id<=0 OR overlap IS NULL
                OR overlap<0 OR offset_value IS NULL OR offset_value<0)
       OR cardinality(p_occurrence_ids)<>
            (SELECT count(DISTINCT value) FROM unnest(p_occurrence_ids) value)
       OR (p_relation_kind='exact' AND cardinality(p_occurrence_ids)<>1) THEN
        RAISE EXCEPTION 'invalid legacy mapping input';
    END IF;
    SELECT min(source_id) INTO v_source FROM occurrence WHERE id=ANY(p_occurrence_ids);
    IF v_source IS NULL OR NOT storage_v2_can_access_source(v_source,'write')
       OR (SELECT count(*) FROM occurrence WHERE id=ANY(p_occurrence_ids) AND source_id=v_source)
            <>cardinality(p_occurrence_ids) THEN
        RAISE EXCEPTION 'legacy mapping target source differs' USING ERRCODE='42501';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('mainrag.legacy-hit:'||p_old_hit_id,0));
    IF EXISTS(SELECT 1 FROM legacy_hit_mapping mapping JOIN occurrence target
        ON target.id=mapping.occurrence_id
        WHERE mapping.old_hit_id=p_old_hit_id AND target.source_id<>v_source) THEN
        RAISE EXCEPTION 'legacy mapping existing source differs' USING ERRCODE='42501';
    END IF;
    PERFORM set_config('storage_v2.legacy_mapping_write','on',TRUE);
    DELETE FROM legacy_hit_mapping WHERE old_hit_id=p_old_hit_id;
    INSERT INTO legacy_hit_mapping(old_hit_id,occurrence_id,ordinal,relation_kind,byte_overlap,source_offset)
        SELECT p_old_hit_id,id,row_number() OVER(ORDER BY overlap DESC,offset_value,id)-1,
            p_relation_kind,overlap,offset_value
        FROM unnest(p_occurrence_ids,p_byte_overlaps,p_source_offsets) target(id,overlap,offset_value);
    PERFORM set_config('storage_v2.legacy_mapping_write','off',TRUE);
    RETURN QUERY SELECT * FROM legacy_hit_mapping WHERE old_hit_id=p_old_hit_id ORDER BY ordinal;
END $$;

-- The producer must decode and hash the original chunk before calling this
-- administrator-only function. A digest reference alone is never a byte proof.
-- This function verifies the native anchor, records the complete identity and
-- installs its mapping atomically. It neither allocates a source generation nor
-- changes a generation membership or active pointer.
CREATE OR REPLACE FUNCTION storage_v2_preserve_legacy_hit(
    p_source_id BIGINT,p_old_hit_id TEXT,p_body_id BIGINT,p_proof JSONB,
    p_expected_mapping_sha256 TEXT
) RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE
    v_body content_body; v_history storage_v2_legacy_hit_history;
    v_item BIGINT; v_artifact BIGINT; v_view BIGINT; v_occurrence BIGINT;
    v_locator JSONB;
BEGIN
    IF NOT storage_v2_is_admin() OR NOT storage_v2_can_access_source(p_source_id,'write') THEN
        RAISE EXCEPTION 'legacy hit preservation requires authorized administrator source access'
            USING ERRCODE='42501';
    END IF;
    IF p_old_hit_id IS NULL OR octet_length(p_old_hit_id) NOT BETWEEN 1 AND 512
       OR p_expected_mapping_sha256 IS NULL OR p_expected_mapping_sha256 !~ '^[0-9a-f]{64}$'
       OR p_proof IS NULL OR jsonb_typeof(p_proof) IS DISTINCT FROM 'object'
       OR octet_length(p_proof::TEXT)>65536 THEN
        RAISE EXCEPTION 'bounded legacy hit preservation identity required';
    END IF;
    IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_proof) key)
        IS DISTINCT FROM ARRAY['chunk_sha256','end_line','file_sha256','logical_bytes',
            'source_path','start_line']::TEXT[]
       OR jsonb_typeof(p_proof->'chunk_sha256') IS DISTINCT FROM 'string'
       OR p_proof->>'chunk_sha256' !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(p_proof->'file_sha256') IS DISTINCT FROM 'string'
       OR p_proof->>'file_sha256' !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(p_proof->'source_path') IS DISTINCT FROM 'string'
       OR octet_length(p_proof->>'source_path') NOT BETWEEN 1 AND 16384
       OR EXISTS(SELECT 1 FROM unnest(ARRAY['logical_bytes','start_line','end_line']) key
           WHERE jsonb_typeof(p_proof->key) IS DISTINCT FROM 'number'
             OR p_proof->>key !~ '^(0|[1-9][0-9]*)$')
       OR (p_proof->>'end_line')::BIGINT<(p_proof->>'start_line')::BIGINT THEN
        RAISE EXCEPTION 'legacy hit preservation proof shape differs';
    END IF;
    SELECT * INTO v_body FROM content_body WHERE id=p_body_id;
    IF NOT FOUND OR v_body.digest_algorithm<>'sha256-v1'
       OR encode(v_body.digest,'hex')<>p_proof->>'chunk_sha256'
       OR v_body.logical_length<>(p_proof->>'logical_bytes')::BIGINT
       OR (v_body.inline_bytes IS NULL AND NOT EXISTS(SELECT 1 FROM content_pack
            WHERE id=v_body.pack_id AND status::TEXT='published'))
       OR (v_body.inline_bytes IS NOT NULL
           AND sha256(v_body.inline_bytes) IS DISTINCT FROM v_body.digest) THEN
        RAISE EXCEPTION 'legacy hit preservation native body differs or is unpublished';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('mainrag.legacy-hit:'||p_old_hit_id,0));
    IF storage_v2_legacy_hit_mapping_state(p_old_hit_id) IS DISTINCT FROM p_expected_mapping_sha256 THEN
        RAISE EXCEPTION 'legacy hit preservation mapping state drifted';
    END IF;
    SELECT * INTO v_history FROM storage_v2_legacy_hit_history WHERE old_hit_id=p_old_hit_id;
    IF FOUND THEN
        IF v_history.source_id<>p_source_id THEN
            RAISE EXCEPTION 'legacy hit preservation existing source differs' USING ERRCODE='42501';
        END IF;
        IF v_history.body_id<>p_body_id OR v_history.proof IS DISTINCT FROM p_proof THEN
            RAISE EXCEPTION 'legacy hit preservation immutable identity drifted';
        END IF;
        v_occurrence:=v_history.occurrence_id;
        IF (SELECT count(*) FROM legacy_hit_mapping WHERE old_hit_id=p_old_hit_id)=1
           AND EXISTS(SELECT 1 FROM legacy_hit_mapping WHERE old_hit_id=p_old_hit_id
               AND occurrence_id=v_occurrence AND ordinal=0 AND relation_kind='exact'
               AND byte_overlap=v_body.logical_length AND source_offset=0) THEN
            RETURN v_occurrence;
        END IF;
    ELSE
        -- Same ID on a different source must fail before allocating graph rows.
        IF EXISTS(SELECT 1 FROM legacy_hit_mapping mapping JOIN occurrence target
            ON target.id=mapping.occurrence_id WHERE mapping.old_hit_id=p_old_hit_id
                AND target.source_id<>p_source_id) THEN
            RAISE EXCEPTION 'legacy hit preservation existing source differs' USING ERRCODE='42501';
        END IF;
        INSERT INTO source_item(source_id,item_kind,item_key)
            VALUES(p_source_id,'legacy-hit-history',p_old_hit_id) RETURNING id INTO v_item;
        INSERT INTO artifact_version(item_id,source_id,witness_type,witness,adapter_profile_id,
            raw_body_id,expected_content_hash,byte_length)
            VALUES(v_item,p_source_id,'legacy-hit-proof',p_proof,'legacy-hit-history-v1',
                p_body_id,p_proof->>'chunk_sha256',v_body.logical_length) RETURNING id INTO v_artifact;
        SELECT id INTO v_view FROM storage_v2_put_retrieval_view('legacy-hit-history',
            'legacy-hit-history-v1','unknown','raw-bytes-v1',0,ARRAY['legacy-content'],
            ARRAY['body'],ARRAY[p_body_id],ARRAY[0::BIGINT],ARRAY[v_body.logical_length]);
        v_locator:=jsonb_build_object('start_line',p_proof->'start_line','end_line',p_proof->'end_line',
            'byte_start',0,'byte_end',v_body.logical_length,'byte_scope','legacy_chunk',
            'line_scope','legacy_source','fragmented',false);
        INSERT INTO occurrence(source_id,artifact_version_id,view_id,role,ordinal,source_path,locator)
            VALUES(p_source_id,v_artifact,v_view,'legacy-hit-history',0,
                p_proof->>'source_path',v_locator) RETURNING id INTO v_occurrence;
        INSERT INTO storage_v2_legacy_hit_history(old_hit_id,source_id,occurrence_id,body_id,proof)
            VALUES(p_old_hit_id,p_source_id,v_occurrence,p_body_id,p_proof);
    END IF;
    PERFORM * FROM storage_v2_replace_legacy_hit_mapping(p_old_hit_id,ARRAY[v_occurrence],
        'exact',ARRAY[v_body.logical_length],ARRAY[0::BIGINT]);
    RETURN v_occurrence;
END $$;

CREATE OR REPLACE FUNCTION storage_v2_complete_legacy_hit_batch(
    p_source_id BIGINT,p_generation_id BIGINT,p_records JSONB
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE
    v_record JSONB; v_native JSONB:='[]'::JSONB; v_hit TEXT;
    v_history BIGINT:=0; v_targets BIGINT:=0; v_generation source_generation;
    v_mapping TEXT; v_existing storage_v2_legacy_hit_proof;
    v_start BIGINT; v_end BIGINT; v_ids BIGINT[]; v_overlaps BIGINT[]; v_offsets BIGINT[];
BEGIN
    IF NOT storage_v2_is_admin() OR NOT storage_v2_can_access_source(p_source_id,'write') THEN
        RAISE EXCEPTION 'legacy proof batch requires authorized administrator source access' USING ERRCODE='42501';
    END IF;
    SELECT * INTO v_generation FROM source_generation WHERE id=p_generation_id AND source_id=p_source_id FOR SHARE;
    IF NOT FOUND OR v_generation.abandoned_at IS NOT NULL
       OR v_generation.status::TEXT NOT IN ('verified','release_candidate','active','superseded')
       OR v_generation.verification_manifest_sha256 IS NULL THEN
        RAISE EXCEPTION 'legacy proof batch requires retained verified generation';
    END IF;
    IF p_records IS NULL OR jsonb_typeof(p_records) IS DISTINCT FROM 'array'
       OR jsonb_array_length(p_records) NOT BETWEEN 1 AND 512 OR octet_length(p_records::TEXT)>8388608 THEN
        RAISE EXCEPTION 'bounded nonempty legacy proof batch required';
    END IF;
    IF EXISTS(SELECT 1 FROM jsonb_array_elements(p_records) record
        WHERE jsonb_typeof(record) IS DISTINCT FROM 'object'
           OR jsonb_typeof(record->'mapping') IS DISTINCT FROM 'object'
           OR jsonb_typeof(record->'proof') IS DISTINCT FROM 'object')
       OR (SELECT count(DISTINCT record->'mapping'->>'old_hit_id') FROM jsonb_array_elements(p_records) record)
            <>jsonb_array_length(p_records) THEN
        RAISE EXCEPTION 'legacy proof batch identities or shape differ';
    END IF;
    FOR v_record IN SELECT value FROM jsonb_array_elements(p_records) LOOP
        v_hit:=v_record->'mapping'->>'old_hit_id';
        IF v_hit IS NULL OR octet_length(v_hit) NOT BETWEEN 1 AND 512
           OR jsonb_typeof(v_record->'proof'->'chunk_sha256') IS DISTINCT FROM 'string'
           OR v_record->'proof'->>'chunk_sha256' !~ '^[0-9a-f]{64}$'
           OR jsonb_typeof(v_record->'proof'->'file_sha256') IS DISTINCT FROM 'string'
           OR v_record->'proof'->>'file_sha256' !~ '^[0-9a-f]{64}$'
           OR jsonb_typeof(v_record->'proof'->'logical_bytes') IS DISTINCT FROM 'number'
           OR v_record->'proof'->>'logical_bytes' !~ '^(0|[1-9][0-9]*)$'
           OR jsonb_typeof(v_record->'proof'->'source_path') IS DISTINCT FROM 'string'
           OR octet_length(v_record->'proof'->>'source_path') NOT BETWEEN 1 AND 16384
           OR octet_length((v_record->'proof')::TEXT)>65536 THEN
            RAISE EXCEPTION 'legacy byte proof identity differs';
        END IF;
        IF EXISTS(SELECT 1 FROM storage_v2_legacy_hit_proof WHERE old_hit_id=v_hit AND source_id<>p_source_id) THEN
            RAISE EXCEPTION 'legacy proof existing source differs' USING ERRCODE='42501';
        END IF;
    END LOOP;
    FOR v_hit IN SELECT record->'mapping'->>'old_hit_id' FROM jsonb_array_elements(p_records) record
        ORDER BY record->'mapping'->>'old_hit_id' COLLATE "C" LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended('mainrag.legacy-hit:'||v_hit,0));
    END LOOP;
    FOR v_record IN SELECT value FROM jsonb_array_elements(p_records) LOOP
        v_hit:=v_record->'mapping'->>'old_hit_id';
        IF storage_v2_legacy_hit_mapping_state(v_hit) IS DISTINCT FROM v_record->'mapping'->>'expected_mapping_sha256' THEN
            RAISE EXCEPTION 'legacy proof mapping state drifted';
        END IF;
        SELECT * INTO v_existing FROM storage_v2_legacy_hit_proof WHERE old_hit_id=v_hit;
        IF FOUND AND v_existing.generation_id=p_generation_id AND v_existing.proof=v_record->'proof'
            AND v_existing.mapping_sha256=storage_v2_legacy_hit_mapping_state(v_hit) THEN
            CONTINUE;
        END IF;
        IF v_record->>'history_body_id' IS NOT NULL THEN
            IF v_record->>'history_body_id' !~ '^[1-9][0-9]*$' THEN
                RAISE EXCEPTION 'legacy historical body identity differs';
            END IF;
            PERFORM storage_v2_preserve_legacy_hit(p_source_id,v_hit,(v_record->>'history_body_id')::BIGINT,
                jsonb_build_object('chunk_sha256',v_record->'proof'->'chunk_sha256',
                    'file_sha256',v_record->'proof'->'file_sha256','logical_bytes',v_record->'proof'->'logical_bytes',
                    'source_path',v_record->'proof'->'source_path','start_line',v_record->'proof'->'start_line',
                    'end_line',v_record->'proof'->'end_line'),v_record->'mapping'->>'expected_mapping_sha256');
            v_history:=v_history+1;
        ELSE
            IF jsonb_typeof(v_record->'proof'->'byte_start') IS DISTINCT FROM 'number'
               OR jsonb_typeof(v_record->'proof'->'byte_end') IS DISTINCT FROM 'number'
               OR v_record->'proof'->>'byte_start' !~ '^(0|[1-9][0-9]*)$'
               OR v_record->'proof'->>'byte_end' !~ '^(0|[1-9][0-9]*)$'
               OR jsonb_typeof(v_record->'proof'->'native_file_sha256') IS DISTINCT FROM 'string'
               OR v_record->'proof'->>'native_file_sha256' !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'legacy native byte range proof differs';
            END IF;
            v_start:=(v_record->'proof'->>'byte_start')::BIGINT;
            v_end:=(v_record->'proof'->>'byte_end')::BIGINT;
            v_ids:=ARRAY(SELECT value::BIGINT FROM jsonb_array_elements_text(v_record->'mapping'->'occurrence_ids') value);
            v_overlaps:=ARRAY(SELECT value::BIGINT FROM jsonb_array_elements_text(v_record->'mapping'->'byte_overlaps') value);
            v_offsets:=ARRAY(SELECT value::BIGINT FROM jsonb_array_elements_text(v_record->'mapping'->'source_offsets') value);
            IF v_end<=v_start OR v_end-v_start<>(v_record->'proof'->>'logical_bytes')::BIGINT
               OR (SELECT sum(value) FROM unnest(v_overlaps) value) IS DISTINCT FROM v_end-v_start
               OR EXISTS(SELECT 1 FROM unnest(v_ids,v_overlaps,v_offsets) supplied(id,overlap,offset_value)
                   LEFT JOIN occurrence target ON target.id=supplied.id
                   LEFT JOIN artifact_version artifact ON artifact.id=target.artifact_version_id
                   WHERE target.id IS NULL OR target.source_id<>p_source_id
                       OR target.source_path IS DISTINCT FROM v_record->'proof'->>'source_path'
                       OR jsonb_typeof(target.locator->'byte_start') IS DISTINCT FROM 'number'
                       OR jsonb_typeof(target.locator->'byte_end') IS DISTINCT FROM 'number'
                       OR supplied.overlap<=0
                       OR supplied.offset_value IS DISTINCT FROM greatest(v_start,(target.locator->>'byte_start')::BIGINT)
                       OR supplied.overlap IS DISTINCT FROM least(v_end,(target.locator->>'byte_end')::BIGINT)
                            -greatest(v_start,(target.locator->>'byte_start')::BIGINT)
                       OR NOT EXISTS(SELECT 1 FROM generation_item_version membership
                           WHERE membership.source_id=p_source_id AND membership.source_item_id=artifact.item_id
                               AND membership.artifact_version_id=artifact.id
                               AND membership.valid_from_seq<=v_generation.generation_seq
                               AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq>v_generation.generation_seq))) THEN
                RAISE EXCEPTION 'legacy native proof coverage or generation differs';
            END IF;
            IF (SELECT min(value) FROM unnest(v_offsets) value) IS DISTINCT FROM v_start
               OR (SELECT max(offset_value+overlap) FROM unnest(v_offsets,v_overlaps) pair(offset_value,overlap)) IS DISTINCT FROM v_end
               OR EXISTS(SELECT 1 FROM (SELECT offset_value,
                    lag(offset_value+overlap) OVER(ORDER BY offset_value,id) AS previous_end
                    FROM unnest(v_ids,v_overlaps,v_offsets) supplied(id,overlap,offset_value)) ordered
                    WHERE previous_end IS NOT NULL AND previous_end<>offset_value) THEN
                RAISE EXCEPTION 'legacy native proof byte coverage contains a gap or overlap';
            END IF;
            v_native:=v_native||jsonb_build_array(v_record->'mapping');
        END IF;
    END LOOP;
    IF jsonb_array_length(v_native)>0 THEN
        v_targets:=(storage_v2_replace_legacy_hit_mappings(p_source_id,v_native)->>'target_count')::BIGINT;
    END IF;
    FOR v_record IN SELECT value FROM jsonb_array_elements(p_records) LOOP
        v_hit:=v_record->'mapping'->>'old_hit_id';
        v_mapping:=storage_v2_legacy_hit_mapping_state(v_hit);
        IF NOT EXISTS(SELECT 1 FROM legacy_hit_mapping mapping JOIN occurrence target ON target.id=mapping.occurrence_id
            WHERE mapping.old_hit_id=v_hit AND target.source_id=p_source_id)
           OR EXISTS(SELECT 1 FROM legacy_hit_mapping mapping JOIN occurrence target ON target.id=mapping.occurrence_id
            WHERE mapping.old_hit_id=v_hit AND target.source_id<>p_source_id) THEN
            RAISE EXCEPTION 'legacy proof completed target source differs';
        END IF;
        INSERT INTO storage_v2_legacy_hit_proof(old_hit_id,source_id,generation_id,proof,mapping_sha256)
            VALUES(v_hit,p_source_id,p_generation_id,v_record->'proof',v_mapping)
            ON CONFLICT(old_hit_id) DO UPDATE SET generation_id=EXCLUDED.generation_id,
                proof=EXCLUDED.proof,mapping_sha256=EXCLUDED.mapping_sha256
            WHERE (storage_v2_legacy_hit_proof.generation_id,storage_v2_legacy_hit_proof.proof,
                storage_v2_legacy_hit_proof.mapping_sha256) IS DISTINCT FROM
                (EXCLUDED.generation_id,EXCLUDED.proof,EXCLUDED.mapping_sha256);
    END LOOP;
    RETURN jsonb_build_object('schema_version','mainrag.storage-v2.legacy-hit-proof-batch.v1',
        'source_id',p_source_id,'generation_id',p_generation_id,'hit_count',jsonb_array_length(p_records),
        'historical_hits',v_history,'native_targets',v_targets,
        'proof_sha256',encode(sha256(convert_to(p_records::TEXT,'UTF8')),'hex'));
END $$;

CREATE FUNCTION storage_v2_lock_legacy_hit_source(p_source_id BIGINT,p_file_id BIGINT,p_file_hash BYTEA)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=on AS $$
BEGIN
    IF NOT storage_v2_is_admin() OR NOT storage_v2_can_access_source(p_source_id,'write') THEN
        RAISE EXCEPTION 'legacy producer requires authorized administrator source access' USING ERRCODE='42501';
    END IF;
    IF NOT pg_try_advisory_xact_lock(hashtextextended('mainrag.storage-v2-ingest-source:'||p_source_id::TEXT,0)) THEN
        RAISE EXCEPTION 'another source writer is active';
    END IF;
    PERFORM storage_v2_lock_legacy_rank_snapshot(p_source_id,p_file_id,p_file_hash);
    RETURN jsonb_build_object('file_revision',coalesce((SELECT revision FROM storage_v2_legacy_rank_revision
        WHERE file_id=p_file_id),0),'legacy_epoch',(SELECT revision FROM storage_v2_legacy_rank_epoch WHERE singleton));
END $$;

-- Bootstrap inventory only. Retire with the producer before legacy removal.
CREATE FUNCTION storage_v2_legacy_hit_inventory(p_source_id BIGINT,p_generation_id BIGINT,p_after_file_id BIGINT)
RETURNS JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE v_result JSONB;
BEGIN
    IF NOT storage_v2_is_admin() OR NOT storage_v2_can_access_source(p_source_id,'write') THEN
        RAISE EXCEPTION 'legacy inventory requires authorized administrator source access' USING ERRCODE='42501';
    END IF;
    IF p_after_file_id IS NULL OR p_after_file_id<0 OR NOT EXISTS(SELECT 1 FROM source_generation
        WHERE id=p_generation_id AND source_id=p_source_id AND abandoned_at IS NULL
            AND status::TEXT IN ('verified','release_candidate','active','superseded')
            AND verification_manifest_sha256 IS NOT NULL) THEN
        RAISE EXCEPTION 'legacy inventory requires verified source generation and file cursor';
    END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_object('file_id',file.id,'file_sha256',encode(file.hash,'hex'),
        'legacy_revision',coalesce((SELECT revision FROM storage_v2_legacy_rank_revision WHERE file_id=file.id),0),
        'hit_count',coverage.hit_count,'completed_hits',coverage.completed_hits) ORDER BY file.id),'[]'::JSONB) INTO v_result
    FROM (SELECT id,path,hash FROM files WHERE source_id=p_source_id AND id>p_after_file_id ORDER BY id LIMIT 128) file
    CROSS JOIN LATERAL (SELECT count(*) AS hit_count,count(proof.old_hit_id) AS completed_hits
        FROM chunks chunk LEFT JOIN storage_v2_legacy_hit_proof proof ON proof.old_hit_id=chunk.id::TEXT
            AND proof.source_id=p_source_id AND proof.generation_id=p_generation_id
            AND proof.proof->>'chunk_sha256'=encode(chunk.content_hash,'hex')
            AND proof.proof->>'file_sha256'=encode(file.hash,'hex') AND proof.proof->>'source_path'=file.path
            AND proof.proof->>'start_line'=chunk.start_line::TEXT AND proof.proof->>'end_line'=chunk.end_line::TEXT
        WHERE chunk.file_id=file.id) coverage;
    RETURN jsonb_build_object('source_id',p_source_id,'generation_id',p_generation_id,'files',v_result,
        'legacy_epoch',(SELECT revision FROM storage_v2_legacy_rank_epoch WHERE singleton));
END $$;

CREATE OR REPLACE FUNCTION storage_v2_resolve_legacy_hit(
    p_source_id BIGINT,p_generation_selector TEXT,p_old_hit_id TEXT
) RETURNS JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=on AS $$
DECLARE v_generation source_generation; v_result JSONB;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id,'read') THEN
        RAISE EXCEPTION 'authorized generation selector required' USING ERRCODE='42501';
    END IF;
    IF p_old_hit_id IS NULL OR octet_length(p_old_hit_id) NOT BETWEEN 1 AND 512 THEN
        RAISE EXCEPTION 'bounded legacy hit id is required';
    END IF;
    v_generation:=storage_v2_resolve_generation(p_source_id,p_generation_selector);
    WITH targets AS MATERIALIZED (
        SELECT mapping.old_hit_id,mapping.occurrence_id,mapping.ordinal,
            CASE WHEN mapping.relation_kind='exact' AND EXISTS(SELECT 1 FROM legacy_hit_mapping other
                WHERE other.occurrence_id=mapping.occurrence_id AND other.old_hit_id<>mapping.old_hit_id)
                THEN 'merged' ELSE mapping.relation_kind END AS relation_kind,
            mapping.byte_overlap,mapping.source_offset,mapping.created_at,
            actual.view_id,actual.source_path,actual.locator,actual.role,
            actual.ordinal AS occurrence_ordinal,view_row.view_digest,item.item_key,
            artifact.expected_content_hash,artifact.id AS artifact_id,artifact.item_id
        FROM legacy_hit_mapping mapping JOIN occurrence actual ON actual.id=mapping.occurrence_id
        JOIN retrieval_view view_row ON view_row.id=actual.view_id
        JOIN artifact_version artifact ON artifact.id=actual.artifact_version_id
        JOIN source_item item ON item.id=artifact.item_id
        WHERE actual.source_id=p_source_id AND mapping.old_hit_id=p_old_hit_id
    ), current_targets AS MATERIALIZED (
        SELECT target.*,v_generation.id AS target_generation_id,
            v_generation.generation_seq AS target_generation_seq
        FROM targets target WHERE EXISTS(SELECT 1 FROM generation_item_version membership
            WHERE membership.source_id=p_source_id AND membership.source_item_id=target.item_id
                AND membership.artifact_version_id=target.artifact_id
                AND membership.valid_from_seq<=v_generation.generation_seq
                AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq>v_generation.generation_seq))
    ), retained_targets AS (
        SELECT target.*,retained.id AS target_generation_id,
            retained.generation_seq AS target_generation_seq
        FROM targets target CROSS JOIN LATERAL (
            SELECT generation.id,generation.generation_seq FROM source_generation generation
            JOIN generation_item_version membership ON membership.source_id=generation.source_id
                AND membership.source_item_id=target.item_id AND membership.artifact_version_id=target.artifact_id
                AND membership.valid_from_seq<=generation.generation_seq
                AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq>generation.generation_seq)
            WHERE generation.source_id=p_source_id AND generation.abandoned_at IS NULL
                AND generation.status::TEXT IN ('verified','release_candidate','active','superseded')
                AND generation.verification_manifest_sha256 IS NOT NULL
                AND generation.generation_seq<v_generation.generation_seq
            ORDER BY generation.generation_seq DESC LIMIT 1
        ) retained WHERE NOT EXISTS(SELECT 1 FROM current_targets)
    ), preserved_targets AS (
        SELECT target.*,NULL::BIGINT AS target_generation_id,NULL::BIGINT AS target_generation_seq
        FROM targets target JOIN storage_v2_legacy_hit_history history
            ON history.old_hit_id=target.old_hit_id AND history.occurrence_id=target.occurrence_id
                AND history.source_id=p_source_id
        WHERE NOT EXISTS(SELECT 1 FROM current_targets) AND NOT EXISTS(SELECT 1 FROM retained_targets)
    ), visible AS (
        SELECT current_targets.*,'selected_generation'::TEXT AS resolution_scope FROM current_targets
        UNION ALL SELECT retained_targets.*,'retained_history'::TEXT FROM retained_targets
        UNION ALL SELECT preserved_targets.*,'retained_legacy_hit'::TEXT FROM preserved_targets
    )
    SELECT jsonb_build_object('schema_version','mainrag.storage-v2.legacy-hit-resolution.v1',
        'source_id',p_source_id,'generation_id',v_generation.id,
        'generation_seq',v_generation.generation_seq,'old_hit_id',p_old_hit_id,
        'resolution_scope',COALESCE(min(resolution_scope),'unresolved'),
        'primary_ordinal',min(ordinal),'targets',COALESCE(jsonb_agg(jsonb_build_object(
            'ordinal',ordinal,'relation_kind',relation_kind,'occurrence_id',occurrence_id,
            'view_id',view_id,'target_generation_id',target_generation_id,
            'target_generation_seq',target_generation_seq,'resolution_scope',resolution_scope,
            'external_hit_id','storage-v2:'||encode(storage_v2_hash_parts('mainrag.external-hit.v1',ARRAY[
                int8send(p_source_id),convert_to(item_key,'UTF8'),convert_to(expected_content_hash,'UTF8'),
                view_digest,convert_to(role,'UTF8'),int8send(occurrence_ordinal),convert_to(locator::TEXT,'UTF8')]),'hex'),
            'source_path',source_path,'locator',locator,'byte_overlap',byte_overlap,'source_offset',source_offset
        ) ORDER BY ordinal),'[]'::JSONB)) INTO v_result FROM visible;
    RETURN v_result;
END $$;

CREATE OR REPLACE FUNCTION storage_v2_legacy_hit_mapping_states(
    p_source_id BIGINT,p_old_hit_ids TEXT[]
) RETURNS JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE v_result JSONB;
BEGIN
    IF NOT storage_v2_is_admin() OR NOT storage_v2_can_access_source(p_source_id,'write') THEN
        RAISE EXCEPTION 'legacy mapping state requires authorized administrator source access'
            USING ERRCODE='42501';
    END IF;
    IF p_old_hit_ids IS NULL OR cardinality(p_old_hit_ids) NOT BETWEEN 1 AND 512
       OR EXISTS(SELECT 1 FROM unnest(p_old_hit_ids) hit
            WHERE hit IS NULL OR octet_length(hit) NOT BETWEEN 1 AND 512)
       OR cardinality(p_old_hit_ids)<>(SELECT count(DISTINCT hit) FROM unnest(p_old_hit_ids) hit) THEN
        RAISE EXCEPTION 'bounded unique legacy hit identities required';
    END IF;
    IF EXISTS(SELECT 1 FROM legacy_hit_mapping mapping JOIN occurrence target
        ON target.id=mapping.occurrence_id
        WHERE mapping.old_hit_id=ANY(p_old_hit_ids) AND target.source_id<>p_source_id) THEN
        RAISE EXCEPTION 'legacy mapping state source differs' USING ERRCODE='42501';
    END IF;
    SELECT jsonb_agg(jsonb_build_object('old_hit_id',hit,
        'expected_mapping_sha256',storage_v2_legacy_hit_mapping_state(hit)) ORDER BY ordinal)
        INTO v_result FROM unnest(p_old_hit_ids) WITH ORDINALITY hits(hit,ordinal);
    RETURN jsonb_build_object('source_id',p_source_id,'mappings',v_result);
END $$;

REVOKE ALL ON FUNCTION storage_v2_legacy_hit_mapping_state(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_legacy_hit_mapping_states(BIGINT,TEXT[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_replace_legacy_hit_mappings(BIGINT,JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_resolve_legacy_hit(BIGINT,TEXT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION storage_v2_preserve_legacy_hit(BIGINT,TEXT,BIGINT,JSONB,TEXT) FROM PUBLIC;
-- Keep the new proof ledger outside the API login's ownership. The definer
-- inherits mainrag privileges for checked graph constructors and obeys caller
-- source RLS on the existing graph; the reader obeys RLS on this new ledger.
ALTER TABLE storage_v2_legacy_hit_history OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE storage_v2_legacy_hit_proof OWNER TO mainrag_v2_frontier_owner;
GRANT INSERT ON storage_v2_legacy_hit_history TO mainrag_v2_frontier_owner;
GRANT INSERT,UPDATE,DELETE ON storage_v2_legacy_hit_proof TO mainrag_v2_frontier_owner;
ALTER FUNCTION storage_v2_invalidate_legacy_hit_proof() OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_invalidate_legacy_hit_proof() FROM PUBLIC;
ALTER FUNCTION storage_v2_preserve_legacy_hit(BIGINT,TEXT,BIGINT,JSONB,TEXT)
    OWNER TO mainrag_v2_frontier_owner;
GRANT EXECUTE ON FUNCTION storage_v2_legacy_hit_mapping_state(TEXT) TO mainrag_v2_frontier_owner;
ALTER FUNCTION storage_v2_complete_legacy_hit_batch(BIGINT,BIGINT,JSONB) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_complete_legacy_hit_batch(BIGINT,BIGINT,JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_complete_legacy_hit_batch(BIGINT,BIGINT,JSONB) TO mainrag;
ALTER FUNCTION storage_v2_lock_legacy_hit_source(BIGINT,BIGINT,BYTEA) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_lock_legacy_hit_source(BIGINT,BIGINT,BYTEA) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_lock_legacy_hit_source(BIGINT,BIGINT,BYTEA) TO mainrag;
ALTER FUNCTION storage_v2_legacy_hit_inventory(BIGINT,BIGINT,BIGINT) OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON FUNCTION storage_v2_legacy_hit_inventory(BIGINT,BIGINT,BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_legacy_hit_inventory(BIGINT,BIGINT,BIGINT) TO mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_replace_legacy_hit_mappings(BIGINT,JSONB),
    storage_v2_legacy_hit_mapping_states(BIGINT,TEXT[]),
    storage_v2_resolve_legacy_hit(BIGINT,TEXT,TEXT),
    storage_v2_preserve_legacy_hit(BIGINT,TEXT,BIGINT,JSONB,TEXT) TO mainrag;
COMMIT;
