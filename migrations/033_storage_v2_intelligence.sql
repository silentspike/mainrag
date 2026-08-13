-- Migration 033: stable, generation-aware storage-v2 intelligence
--
-- Additive only. Legacy symbols, cards, annotations, entities, relations, and
-- negative evidence remain readable and are not modified by this migration.

CREATE TABLE IF NOT EXISTS storage_v2_intelligence_profile (
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    profile_id TEXT NOT NULL CHECK (profile_id <> ''),
    profile_version BIGINT NOT NULL CHECK (profile_version > 0),
    profile_sha256 BYTEA NOT NULL CHECK (octet_length(profile_sha256) = 32),
    rules JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_id, profile_id, profile_version),
    UNIQUE (source_id, profile_id, profile_sha256)
);

CREATE TABLE IF NOT EXISTS storage_v2_symbol (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    symbol_key TEXT NOT NULL CHECK (symbol_key <> ''),
    identity_sha256 BYTEA NOT NULL CHECK (octet_length(identity_sha256) = 32),
    language TEXT NOT NULL CHECK (language <> ''),
    symbol_kind TEXT NOT NULL CHECK (symbol_kind <> ''),
    qualified_name TEXT NOT NULL CHECK (qualified_name <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, source_id),
    UNIQUE (source_id, symbol_key),
    UNIQUE (source_id, identity_sha256)
);

CREATE TABLE IF NOT EXISTS storage_v2_symbol_occurrence (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL,
    symbol_id BIGINT NOT NULL,
    artifact_version_id BIGINT NOT NULL,
    occurrence_id BIGINT NOT NULL REFERENCES occurrence(id) ON DELETE RESTRICT,
    signature TEXT,
    documentation TEXT,
    visibility TEXT,
    structure JSONB NOT NULL,
    source_span JSONB NOT NULL,
    structural_sha256 BYTEA NOT NULL CHECK (octet_length(structural_sha256) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, source_id),
    UNIQUE (symbol_id, artifact_version_id, structural_sha256),
    FOREIGN KEY (symbol_id, source_id)
        REFERENCES storage_v2_symbol(id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY (artifact_version_id) REFERENCES artifact_version(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS storage_v2_intelligence_analysis (
    symbol_occurrence_id BIGINT NOT NULL REFERENCES storage_v2_symbol_occurrence(id) ON DELETE RESTRICT,
    analysis_profile_id TEXT NOT NULL CHECK (analysis_profile_id <> ''),
    status TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'failed')),
    attempt_count BIGINT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    output_sha256 BYTEA CHECK (output_sha256 IS NULL OR octet_length(output_sha256) = 32),
    error_code TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol_occurrence_id, analysis_profile_id),
    CHECK (
        (status = 'complete' AND output_sha256 IS NOT NULL AND error_code IS NULL)
        OR (status = 'failed' AND output_sha256 IS NULL AND error_code IS NOT NULL)
        OR (status = 'pending' AND output_sha256 IS NULL AND error_code IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS storage_v2_symbol_card (
    symbol_occurrence_id BIGINT NOT NULL REFERENCES storage_v2_symbol_occurrence(id) ON DELETE RESTRICT,
    analysis_profile_id TEXT NOT NULL CHECK (analysis_profile_id <> ''),
    domain_profile_id TEXT,
    domain_profile_version BIGINT,
    generic_card JSONB NOT NULL,
    domain_fields JSONB NOT NULL,
    field_provenance JSONB NOT NULL,
    normalized_output JSONB NOT NULL,
    output_sha256 BYTEA NOT NULL CHECK (octet_length(output_sha256) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol_occurrence_id, analysis_profile_id),
    CHECK ((domain_profile_id IS NULL) = (domain_profile_version IS NULL))
);

CREATE TABLE IF NOT EXISTS storage_v2_symbol_annotation (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL,
    symbol_id BIGINT NOT NULL,
    symbol_occurrence_id BIGINT REFERENCES storage_v2_symbol_occurrence(id) ON DELETE RESTRICT,
    annotation_type TEXT NOT NULL CHECK (annotation_type <> ''),
    value JSONB NOT NULL,
    provenance JSONB NOT NULL,
    author_kind TEXT NOT NULL CHECK (author_kind IN ('user', 'profile', 'parser')),
    profile_id TEXT,
    profile_version BIGINT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, symbol_id, annotation_type, value, author_kind),
    FOREIGN KEY (symbol_id, source_id)
        REFERENCES storage_v2_symbol(id, source_id) ON DELETE RESTRICT,
    CHECK ((profile_id IS NULL) = (profile_version IS NULL)),
    CHECK (author_kind <> 'profile' OR profile_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS storage_v2_call_edge (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    caller_occurrence_id BIGINT NOT NULL REFERENCES storage_v2_symbol_occurrence(id) ON DELETE RESTRICT,
    callee_symbol_id BIGINT NOT NULL REFERENCES storage_v2_symbol(id) ON DELETE RESTRICT,
    call_kind TEXT NOT NULL CHECK (call_kind <> ''),
    evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (caller_occurrence_id, callee_symbol_id, call_kind, evidence)
);

CREATE TABLE IF NOT EXISTS storage_v2_unresolved_call (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    caller_occurrence_id BIGINT NOT NULL REFERENCES storage_v2_symbol_occurrence(id) ON DELETE RESTRICT,
    callee_name TEXT NOT NULL CHECK (callee_name <> ''),
    call_kind TEXT NOT NULL CHECK (call_kind <> ''),
    evidence JSONB NOT NULL,
    candidate_symbol_keys JSONB NOT NULL DEFAULT '[]'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (caller_occurrence_id, callee_name, call_kind, evidence)
);

CREATE TABLE IF NOT EXISTS storage_v2_intelligence_entity (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    entity_key TEXT NOT NULL CHECK (entity_key <> ''),
    symbol_id BIGINT REFERENCES storage_v2_symbol(id) ON DELETE RESTRICT,
    name TEXT NOT NULL CHECK (name <> ''),
    entity_type TEXT NOT NULL CHECK (entity_type <> ''),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, source_id),
    UNIQUE (source_id, entity_key)
);

CREATE TABLE IF NOT EXISTS storage_v2_intelligence_relation (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    source_entity_id BIGINT NOT NULL,
    target_entity_id BIGINT NOT NULL,
    relation_type TEXT NOT NULL CHECK (relation_type <> ''),
    evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_entity_id, target_entity_id, relation_type),
    FOREIGN KEY (source_entity_id, source_id)
        REFERENCES storage_v2_intelligence_entity(id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY (target_entity_id, source_id)
        REFERENCES storage_v2_intelligence_entity(id, source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS storage_v2_negative_evidence (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    evidence_key TEXT NOT NULL CHECK (evidence_key <> ''),
    concept TEXT NOT NULL CHECK (concept <> ''),
    path_description TEXT NOT NULL CHECK (path_description <> ''),
    reason TEXT NOT NULL CHECK (reason <> ''),
    symbol_keys JSONB NOT NULL DEFAULT '[]'::JSONB,
    severity TEXT NOT NULL,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, evidence_key)
);

CREATE INDEX IF NOT EXISTS idx_storage_v2_symbol_qualified
    ON storage_v2_symbol(source_id, qualified_name, symbol_kind);
CREATE INDEX IF NOT EXISTS idx_storage_v2_symbol_occurrence_artifact
    ON storage_v2_symbol_occurrence(source_id, artifact_version_id, symbol_id);
CREATE INDEX IF NOT EXISTS idx_storage_v2_card_profile
    ON storage_v2_symbol_card(analysis_profile_id, domain_profile_id, domain_profile_version);

DO $$
DECLARE v_table TEXT;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'storage_v2_intelligence_profile', 'storage_v2_symbol',
        'storage_v2_symbol_occurrence', 'storage_v2_intelligence_analysis',
        'storage_v2_symbol_card', 'storage_v2_symbol_annotation',
        'storage_v2_call_edge', 'storage_v2_unresolved_call',
        'storage_v2_intelligence_entity', 'storage_v2_intelligence_relation',
        'storage_v2_negative_evidence'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', v_table);
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', v_table || '_controlled', v_table);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_controlled_update()',
            v_table || '_controlled', v_table
        );
    END LOOP;
END
$$;

DROP POLICY IF EXISTS storage_v2_intelligence_profile_isolation ON storage_v2_intelligence_profile;
CREATE POLICY storage_v2_intelligence_profile_isolation ON storage_v2_intelligence_profile
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_symbol_isolation ON storage_v2_symbol;
CREATE POLICY storage_v2_symbol_isolation ON storage_v2_symbol
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_symbol_occurrence_isolation ON storage_v2_symbol_occurrence;
CREATE POLICY storage_v2_symbol_occurrence_isolation ON storage_v2_symbol_occurrence
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_symbol_annotation_isolation ON storage_v2_symbol_annotation;
CREATE POLICY storage_v2_symbol_annotation_isolation ON storage_v2_symbol_annotation
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_call_edge_isolation ON storage_v2_call_edge;
CREATE POLICY storage_v2_call_edge_isolation ON storage_v2_call_edge
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_unresolved_call_isolation ON storage_v2_unresolved_call;
CREATE POLICY storage_v2_unresolved_call_isolation ON storage_v2_unresolved_call
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_intelligence_entity_isolation ON storage_v2_intelligence_entity;
CREATE POLICY storage_v2_intelligence_entity_isolation ON storage_v2_intelligence_entity
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_intelligence_relation_isolation ON storage_v2_intelligence_relation;
CREATE POLICY storage_v2_intelligence_relation_isolation ON storage_v2_intelligence_relation
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS storage_v2_negative_evidence_isolation ON storage_v2_negative_evidence;
CREATE POLICY storage_v2_negative_evidence_isolation ON storage_v2_negative_evidence
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));

-- The remaining tables derive their source through their parent occurrence.
DROP POLICY IF EXISTS storage_v2_intelligence_analysis_isolation ON storage_v2_intelligence_analysis;
CREATE POLICY storage_v2_intelligence_analysis_isolation ON storage_v2_intelligence_analysis
    USING (EXISTS (
        SELECT 1 FROM storage_v2_symbol_occurrence occurrence_row
         WHERE occurrence_row.id = symbol_occurrence_id
           AND storage_v2_can_access_source(occurrence_row.source_id, 'read')
    ));
DROP POLICY IF EXISTS storage_v2_symbol_card_isolation ON storage_v2_symbol_card;
CREATE POLICY storage_v2_symbol_card_isolation ON storage_v2_symbol_card
    USING (EXISTS (
        SELECT 1 FROM storage_v2_symbol_occurrence occurrence_row
         WHERE occurrence_row.id = symbol_occurrence_id
           AND storage_v2_can_access_source(occurrence_row.source_id, 'read')
    ));

CREATE OR REPLACE FUNCTION storage_v2_put_intelligence_profile(
    p_source_id BIGINT,
    p_profile_id TEXT,
    p_profile_version BIGINT,
    p_rules JSONB
) RETURNS storage_v2_intelligence_profile
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_hash BYTEA;
    v_profile storage_v2_intelligence_profile;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'write')
       OR p_profile_id IS NULL OR p_profile_id = ''
       OR p_profile_version < 1 OR p_rules IS NULL THEN
        RAISE EXCEPTION 'valid source-bound intelligence profile required' USING ERRCODE = '42501';
    END IF;
    v_hash := digest(convert_to(p_rules::TEXT, 'UTF8'), 'sha256');
    INSERT INTO storage_v2_intelligence_profile(
        source_id, profile_id, profile_version, profile_sha256, rules
    ) VALUES (p_source_id, p_profile_id, p_profile_version, v_hash, p_rules)
    ON CONFLICT (source_id, profile_id, profile_version) DO NOTHING
    RETURNING * INTO v_profile;
    IF NOT FOUND THEN
        SELECT * INTO v_profile FROM storage_v2_intelligence_profile
         WHERE source_id = p_source_id AND profile_id = p_profile_id
           AND profile_version = p_profile_version;
        IF v_profile.profile_sha256 <> v_hash THEN
            RAISE EXCEPTION 'profile version collision' USING ERRCODE = '22000';
        END IF;
    END IF;
    RETURN v_profile;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_put_symbol_occurrence(
    p_source_id BIGINT,
    p_artifact_version_id BIGINT,
    p_occurrence_id BIGINT,
    p_symbol_key TEXT,
    p_language TEXT,
    p_symbol_kind TEXT,
    p_qualified_name TEXT,
    p_signature TEXT,
    p_documentation TEXT,
    p_visibility TEXT,
    p_structure JSONB,
    p_source_span JSONB
) RETURNS storage_v2_symbol_occurrence
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_symbol storage_v2_symbol;
    v_occurrence storage_v2_symbol_occurrence;
    v_identity BYTEA;
    v_structural BYTEA;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'write')
       OR p_symbol_key IS NULL OR p_symbol_key = ''
       OR p_language IS NULL OR p_language = ''
       OR p_symbol_kind IS NULL OR p_symbol_kind = ''
       OR p_qualified_name IS NULL OR p_qualified_name = ''
       OR p_structure IS NULL OR p_source_span IS NULL
       OR NOT EXISTS (
           SELECT 1 FROM artifact_version artifact
           JOIN occurrence occurrence_row ON occurrence_row.id = p_occurrence_id
          WHERE artifact.id = p_artifact_version_id
            AND artifact.source_id = p_source_id
            AND occurrence_row.source_id = p_source_id
            AND occurrence_row.artifact_version_id = artifact.id
       ) THEN
        RAISE EXCEPTION 'valid authorized symbol occurrence required' USING ERRCODE = '42501';
    END IF;
    v_identity := storage_v2_hash_parts('mainrag.symbol.v1', ARRAY[
        int8send(p_source_id), convert_to(p_symbol_key, 'UTF8')
    ]);
    INSERT INTO storage_v2_symbol(
        source_id, symbol_key, identity_sha256, language, symbol_kind, qualified_name
    ) VALUES (
        p_source_id, p_symbol_key, v_identity, p_language, p_symbol_kind, p_qualified_name
    ) ON CONFLICT (source_id, symbol_key) DO NOTHING
    RETURNING * INTO v_symbol;
    IF NOT FOUND THEN
        SELECT * INTO v_symbol FROM storage_v2_symbol
         WHERE source_id = p_source_id AND symbol_key = p_symbol_key;
        IF (v_symbol.language, v_symbol.symbol_kind, v_symbol.qualified_name)
           IS DISTINCT FROM (p_language, p_symbol_kind, p_qualified_name) THEN
            RAISE EXCEPTION 'stable symbol key collision' USING ERRCODE = '22000';
        END IF;
    END IF;
    v_structural := storage_v2_hash_parts('mainrag.symbol-occurrence.v1', ARRAY[
        convert_to(COALESCE(p_signature, ''), 'UTF8'),
        convert_to(COALESCE(p_documentation, ''), 'UTF8'),
        convert_to(COALESCE(p_visibility, ''), 'UTF8'),
        convert_to(p_structure::TEXT, 'UTF8'), convert_to(p_source_span::TEXT, 'UTF8')
    ]);
    INSERT INTO storage_v2_symbol_occurrence(
        source_id, symbol_id, artifact_version_id, occurrence_id, signature,
        documentation, visibility, structure, source_span, structural_sha256
    ) VALUES (
        p_source_id, v_symbol.id, p_artifact_version_id, p_occurrence_id, p_signature,
        p_documentation, p_visibility, p_structure, p_source_span, v_structural
    ) ON CONFLICT (symbol_id, artifact_version_id, structural_sha256) DO NOTHING
    RETURNING * INTO v_occurrence;
    IF NOT FOUND THEN
        SELECT * INTO v_occurrence FROM storage_v2_symbol_occurrence
         WHERE symbol_id = v_symbol.id AND artifact_version_id = p_artifact_version_id
           AND structural_sha256 = v_structural;
    END IF;
    RETURN v_occurrence;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_begin_intelligence_analysis(
    p_symbol_occurrence_id BIGINT,
    p_analysis_profile_id TEXT
) RETURNS storage_v2_intelligence_analysis
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_source_id BIGINT;
    v_analysis storage_v2_intelligence_analysis;
BEGIN
    SELECT source_id INTO v_source_id FROM storage_v2_symbol_occurrence
     WHERE id = p_symbol_occurrence_id;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'write')
       OR p_analysis_profile_id IS NULL OR p_analysis_profile_id = '' THEN
        RAISE EXCEPTION 'authorized symbol analysis required' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_analysis FROM storage_v2_intelligence_analysis
     WHERE symbol_occurrence_id = p_symbol_occurrence_id
       AND analysis_profile_id = p_analysis_profile_id AND status = 'complete';
    IF FOUND THEN RETURN v_analysis; END IF;
    INSERT INTO storage_v2_intelligence_analysis(
        symbol_occurrence_id, analysis_profile_id, status, attempt_count
    ) VALUES (p_symbol_occurrence_id, p_analysis_profile_id, 'pending', 1)
    ON CONFLICT (symbol_occurrence_id, analysis_profile_id) DO UPDATE
       SET status = 'pending', output_sha256 = NULL, error_code = NULL,
           attempt_count = storage_v2_intelligence_analysis.attempt_count + 1,
           updated_at = NOW()
    RETURNING * INTO v_analysis;
    RETURN v_analysis;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_finish_intelligence_analysis(
    p_symbol_occurrence_id BIGINT,
    p_analysis_profile_id TEXT,
    p_output_sha256 BYTEA DEFAULT NULL,
    p_error_code TEXT DEFAULT NULL
) RETURNS storage_v2_intelligence_analysis
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_source_id BIGINT;
    v_analysis storage_v2_intelligence_analysis;
BEGIN
    SELECT source_id INTO v_source_id FROM storage_v2_symbol_occurrence
     WHERE id = p_symbol_occurrence_id;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'write')
       OR (p_output_sha256 IS NULL) = (p_error_code IS NULL)
       OR (p_output_sha256 IS NOT NULL AND octet_length(p_output_sha256) <> 32)
       OR p_error_code = '' THEN
        RAISE EXCEPTION 'provide exactly one valid analysis output or error' USING ERRCODE = '42501';
    END IF;
    UPDATE storage_v2_intelligence_analysis
       SET status = CASE WHEN p_output_sha256 IS NULL THEN 'failed' ELSE 'complete' END,
           output_sha256 = p_output_sha256, error_code = p_error_code, updated_at = NOW()
     WHERE symbol_occurrence_id = p_symbol_occurrence_id
       AND analysis_profile_id = p_analysis_profile_id AND status = 'pending'
     RETURNING * INTO v_analysis;
    IF NOT FOUND THEN RAISE EXCEPTION 'pending intelligence analysis not found'; END IF;
    RETURN v_analysis;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_put_symbol_card(
    p_symbol_occurrence_id BIGINT,
    p_analysis_profile_id TEXT,
    p_generic_card JSONB,
    p_domain_fields JSONB,
    p_field_provenance JSONB,
    p_domain_profile_id TEXT DEFAULT NULL,
    p_domain_profile_version BIGINT DEFAULT NULL
) RETURNS storage_v2_symbol_card
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_source_id BIGINT;
    v_field TEXT;
    v_value JSONB;
    v_domain_fields JSONB;
    v_normalized JSONB;
    v_hash BYTEA;
    v_card storage_v2_symbol_card;
BEGIN
    SELECT source_id INTO v_source_id FROM storage_v2_symbol_occurrence
     WHERE id = p_symbol_occurrence_id;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'write')
       OR p_analysis_profile_id IS NULL OR p_analysis_profile_id = ''
       OR p_generic_card IS NULL OR p_domain_fields IS NULL OR p_field_provenance IS NULL
       OR jsonb_typeof(p_generic_card) <> 'object'
       OR jsonb_typeof(p_domain_fields) <> 'object'
       OR jsonb_typeof(p_field_provenance) <> 'object'
       OR (p_domain_profile_id IS NULL) <> (p_domain_profile_version IS NULL) THEN
        RAISE EXCEPTION 'valid authorized intelligence card required' USING ERRCODE = '42501';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_object_keys(p_domain_fields) field_name
         WHERE field_name NOT IN ('layer', 'side_effect', 'resource', 'delegation_target')
    ) OR EXISTS (
        SELECT 1 FROM jsonb_object_keys(p_field_provenance) field_name
         WHERE field_name NOT IN ('layer', 'side_effect', 'resource', 'delegation_target')
    ) THEN
        RAISE EXCEPTION 'unsupported domain field or provenance';
    END IF;
    IF p_domain_profile_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM storage_v2_intelligence_profile
         WHERE source_id = v_source_id AND profile_id = p_domain_profile_id
           AND profile_version = p_domain_profile_version
    ) THEN
        RAISE EXCEPTION 'source-bound domain profile version not found';
    END IF;
    FOREACH v_field IN ARRAY ARRAY['layer', 'side_effect', 'resource', 'delegation_target'] LOOP
        v_value := COALESCE(p_domain_fields -> v_field, '"unknown"'::JSONB);
        IF jsonb_typeof(v_value) <> 'string' THEN
            RAISE EXCEPTION 'domain field % must be a string', v_field;
        ELSIF v_value = '"unknown"'::JSONB THEN
            IF p_field_provenance ? v_field THEN
                RAISE EXCEPTION 'unknown domain field % must not claim provenance', v_field;
            END IF;
        ELSE
            IF p_domain_profile_id IS NULL
               OR NOT (p_field_provenance ? v_field)
               OR p_field_provenance -> v_field ->> 'profile_id' IS DISTINCT FROM p_domain_profile_id
               OR (p_field_provenance -> v_field ->> 'profile_version')::BIGINT
                  IS DISTINCT FROM p_domain_profile_version
               OR COALESCE(p_field_provenance -> v_field ->> 'rule_id', '') = ''
               OR COALESCE(p_field_provenance -> v_field ->> 'evidence', '') = '' THEN
                RAISE EXCEPTION 'domain field % requires matching profile provenance', v_field;
            END IF;
        END IF;
    END LOOP;
    v_domain_fields := jsonb_build_object(
        'layer', COALESCE(p_domain_fields -> 'layer', '"unknown"'::JSONB),
        'side_effect', COALESCE(p_domain_fields -> 'side_effect', '"unknown"'::JSONB),
        'resource', COALESCE(p_domain_fields -> 'resource', '"unknown"'::JSONB),
        'delegation_target', COALESCE(p_domain_fields -> 'delegation_target', '"unknown"'::JSONB)
    );
    v_normalized := jsonb_build_object(
        'generic', p_generic_card,
        'domain', v_domain_fields,
        'provenance', p_field_provenance,
        'analysis_profile_id', p_analysis_profile_id,
        'domain_profile_id', p_domain_profile_id,
        'domain_profile_version', p_domain_profile_version
    );
    v_hash := digest(convert_to(v_normalized::TEXT, 'UTF8'), 'sha256');
    INSERT INTO storage_v2_symbol_card(
        symbol_occurrence_id, analysis_profile_id, domain_profile_id,
        domain_profile_version, generic_card, domain_fields, field_provenance,
        normalized_output, output_sha256
    ) VALUES (
        p_symbol_occurrence_id, p_analysis_profile_id, p_domain_profile_id,
        p_domain_profile_version, p_generic_card, v_domain_fields, p_field_provenance,
        v_normalized, v_hash
    ) ON CONFLICT (symbol_occurrence_id, analysis_profile_id) DO NOTHING
    RETURNING * INTO v_card;
    IF NOT FOUND THEN
        SELECT * INTO v_card FROM storage_v2_symbol_card
         WHERE symbol_occurrence_id = p_symbol_occurrence_id
           AND analysis_profile_id = p_analysis_profile_id;
        IF v_card.output_sha256 <> v_hash THEN
            RAISE EXCEPTION 'analysis profile output collision' USING ERRCODE = '22000';
        END IF;
    END IF;
    RETURN v_card;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_record_call(
    p_caller_occurrence_id BIGINT,
    p_callee_symbol_id BIGINT,
    p_callee_name TEXT,
    p_call_kind TEXT,
    p_evidence JSONB,
    p_candidate_symbol_keys JSONB DEFAULT '[]'::JSONB
) RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_source_id BIGINT;
    v_resolution TEXT;
    v_candidate_count BIGINT;
BEGIN
    SELECT source_id INTO v_source_id FROM storage_v2_symbol_occurrence
     WHERE id = p_caller_occurrence_id;
    IF NOT FOUND OR NOT storage_v2_can_access_source(v_source_id, 'write')
       OR p_callee_name IS NULL OR p_callee_name = '' OR p_call_kind IS NULL OR p_call_kind = ''
       OR p_evidence IS NULL THEN
        RAISE EXCEPTION 'valid authorized call evidence required' USING ERRCODE = '42501';
    END IF;
    v_resolution := p_evidence ->> 'resolution_kind';
    IF p_callee_symbol_id IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM storage_v2_symbol WHERE id = p_callee_symbol_id AND source_id = v_source_id)
           OR v_resolution NOT IN ('parser_symbol_id', 'qualified_unique') THEN
            RAISE EXCEPTION 'call edge is not proven';
        END IF;
        IF v_resolution = 'qualified_unique' THEN
            SELECT COUNT(*) INTO v_candidate_count FROM storage_v2_symbol
             WHERE source_id = v_source_id AND qualified_name = p_callee_name;
            IF v_candidate_count <> 1 THEN RAISE EXCEPTION 'qualified call is ambiguous'; END IF;
        END IF;
        INSERT INTO storage_v2_call_edge(
            source_id, caller_occurrence_id, callee_symbol_id, call_kind, evidence
        ) VALUES (v_source_id, p_caller_occurrence_id, p_callee_symbol_id, p_call_kind, p_evidence)
        ON CONFLICT DO NOTHING;
        RETURN 'proven';
    END IF;
    INSERT INTO storage_v2_unresolved_call(
        source_id, caller_occurrence_id, callee_name, call_kind, evidence, candidate_symbol_keys
    ) VALUES (
        v_source_id, p_caller_occurrence_id, p_callee_name, p_call_kind,
        p_evidence, COALESCE(p_candidate_symbol_keys, '[]'::JSONB)
    ) ON CONFLICT DO NOTHING;
    RETURN 'unresolved';
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_put_symbol_annotation(
    p_source_id BIGINT,
    p_symbol_id BIGINT,
    p_symbol_occurrence_id BIGINT,
    p_annotation_type TEXT,
    p_value JSONB,
    p_provenance JSONB,
    p_author_kind TEXT,
    p_profile_id TEXT DEFAULT NULL,
    p_profile_version BIGINT DEFAULT NULL,
    p_created_by TEXT DEFAULT NULL
) RETURNS storage_v2_symbol_annotation
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE v_annotation storage_v2_symbol_annotation;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'write')
       OR NOT EXISTS (SELECT 1 FROM storage_v2_symbol WHERE id = p_symbol_id AND source_id = p_source_id)
       OR (p_symbol_occurrence_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM storage_v2_symbol_occurrence
             WHERE id = p_symbol_occurrence_id AND symbol_id = p_symbol_id AND source_id = p_source_id
       ))
       OR p_annotation_type IS NULL OR p_annotation_type = ''
       OR p_value IS NULL OR p_provenance IS NULL
       OR p_author_kind NOT IN ('user', 'profile', 'parser')
       OR (p_profile_id IS NULL) <> (p_profile_version IS NULL)
       OR (p_author_kind = 'profile' AND NOT EXISTS (
            SELECT 1 FROM storage_v2_intelligence_profile
             WHERE source_id = p_source_id AND profile_id = p_profile_id
               AND profile_version = p_profile_version
       )) THEN
        RAISE EXCEPTION 'valid authorized symbol annotation required' USING ERRCODE = '42501';
    END IF;
    INSERT INTO storage_v2_symbol_annotation(
        source_id, symbol_id, symbol_occurrence_id, annotation_type, value,
        provenance, author_kind, profile_id, profile_version, created_by
    ) VALUES (
        p_source_id, p_symbol_id, p_symbol_occurrence_id, p_annotation_type, p_value,
        p_provenance, p_author_kind, p_profile_id, p_profile_version, p_created_by
    ) ON CONFLICT (source_id, symbol_id, annotation_type, value, author_kind) DO UPDATE
       SET symbol_occurrence_id = COALESCE(EXCLUDED.symbol_occurrence_id,
                                           storage_v2_symbol_annotation.symbol_occurrence_id),
           provenance = EXCLUDED.provenance, profile_id = EXCLUDED.profile_id,
           profile_version = EXCLUDED.profile_version,
           created_by = COALESCE(storage_v2_symbol_annotation.created_by, EXCLUDED.created_by)
    RETURNING * INTO v_annotation;
    RETURN v_annotation;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_put_intelligence_entity(
    p_source_id BIGINT,
    p_entity_key TEXT,
    p_symbol_id BIGINT,
    p_name TEXT,
    p_entity_type TEXT,
    p_payload JSONB
) RETURNS storage_v2_intelligence_entity
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE v_entity storage_v2_intelligence_entity;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'write')
       OR p_entity_key IS NULL OR p_entity_key = '' OR p_name IS NULL OR p_name = ''
       OR p_entity_type IS NULL OR p_entity_type = '' OR p_payload IS NULL
       OR (p_symbol_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM storage_v2_symbol WHERE id = p_symbol_id AND source_id = p_source_id
       )) THEN
        RAISE EXCEPTION 'valid authorized intelligence entity required' USING ERRCODE = '42501';
    END IF;
    INSERT INTO storage_v2_intelligence_entity(
        source_id, entity_key, symbol_id, name, entity_type, payload
    ) VALUES (p_source_id, p_entity_key, p_symbol_id, p_name, p_entity_type, p_payload)
    ON CONFLICT (source_id, entity_key) DO UPDATE
       SET symbol_id = EXCLUDED.symbol_id, name = EXCLUDED.name,
           entity_type = EXCLUDED.entity_type, payload = EXCLUDED.payload
    RETURNING * INTO v_entity;
    RETURN v_entity;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_put_intelligence_relation(
    p_source_id BIGINT,
    p_source_entity_id BIGINT,
    p_target_entity_id BIGINT,
    p_relation_type TEXT,
    p_evidence JSONB
) RETURNS storage_v2_intelligence_relation
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE v_relation storage_v2_intelligence_relation;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'write')
       OR p_relation_type IS NULL OR p_relation_type = '' OR p_evidence IS NULL
       OR COALESCE(p_evidence ->> 'resolution_kind', '') NOT IN (
            'parser_symbol_id', 'qualified_unique', 'user_asserted'
       )
       OR NOT EXISTS (
            SELECT 1 FROM storage_v2_intelligence_entity
             WHERE id = p_source_entity_id AND source_id = p_source_id
       )
       OR NOT EXISTS (
            SELECT 1 FROM storage_v2_intelligence_entity
             WHERE id = p_target_entity_id AND source_id = p_source_id
       ) THEN
        RAISE EXCEPTION 'intelligence relation requires proven same-source evidence' USING ERRCODE = '42501';
    END IF;
    INSERT INTO storage_v2_intelligence_relation(
        source_id, source_entity_id, target_entity_id, relation_type, evidence
    ) VALUES (p_source_id, p_source_entity_id, p_target_entity_id, p_relation_type, p_evidence)
    ON CONFLICT (source_entity_id, target_entity_id, relation_type) DO UPDATE
       SET evidence = EXCLUDED.evidence
    RETURNING * INTO v_relation;
    RETURN v_relation;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_put_negative_evidence(
    p_source_id BIGINT,
    p_evidence_key TEXT,
    p_concept TEXT,
    p_path_description TEXT,
    p_reason TEXT,
    p_symbol_keys JSONB,
    p_severity TEXT,
    p_created_by TEXT DEFAULT NULL
) RETURNS storage_v2_negative_evidence
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE v_evidence storage_v2_negative_evidence;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'write')
       OR p_evidence_key IS NULL OR p_evidence_key = ''
       OR p_concept IS NULL OR p_concept = ''
       OR p_path_description IS NULL OR p_path_description = ''
       OR p_reason IS NULL OR p_reason = '' OR p_symbol_keys IS NULL
       OR jsonb_typeof(p_symbol_keys) <> 'array'
       OR p_severity NOT IN ('info', 'warning', 'error') THEN
        RAISE EXCEPTION 'valid authorized negative evidence required' USING ERRCODE = '42501';
    END IF;
    INSERT INTO storage_v2_negative_evidence(
        source_id, evidence_key, concept, path_description, reason,
        symbol_keys, severity, created_by
    ) VALUES (
        p_source_id, p_evidence_key, p_concept, p_path_description, p_reason,
        p_symbol_keys, p_severity, p_created_by
    ) ON CONFLICT (source_id, evidence_key) DO UPDATE
       SET concept = EXCLUDED.concept, path_description = EXCLUDED.path_description,
           reason = EXCLUDED.reason, symbol_keys = EXCLUDED.symbol_keys,
           severity = EXCLUDED.severity,
           created_by = COALESCE(storage_v2_negative_evidence.created_by, EXCLUDED.created_by)
    RETURNING * INTO v_evidence;
    RETURN v_evidence;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_resolve_generation(
    p_source_id BIGINT,
    p_selector TEXT
) RETURNS source_generation
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE v_generation source_generation;
BEGIN
    IF NOT storage_v2_can_access_source(p_source_id, 'read')
       OR p_selector IS NULL OR p_selector = '' THEN
        RAISE EXCEPTION 'authorized generation selector required' USING ERRCODE = '42501';
    END IF;
    IF p_selector = 'current' THEN
        SELECT generation.* INTO v_generation
          FROM logical_source source_row
          JOIN source_generation generation ON generation.id = source_row.active_generation_id
         WHERE source_row.id = p_source_id;
    ELSIF p_selector ~ '^[1-9][0-9]*$' THEN
        SELECT * INTO v_generation FROM source_generation
         WHERE source_id = p_source_id AND generation_seq = p_selector::BIGINT
           AND status <> 'building';
    ELSE
        RAISE EXCEPTION 'generation selector must be current or a positive generation sequence';
    END IF;
    IF NOT FOUND THEN RAISE EXCEPTION 'selected generation is not readable'; END IF;
    RETURN v_generation;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_export_intelligence(
    p_source_id BIGINT,
    p_generation_selector TEXT,
    p_redaction TEXT DEFAULT 'public'
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
    v_payload JSONB;
    v_protected_payload_sha256 TEXT;
    v_hash TEXT;
BEGIN
    IF p_redaction NOT IN ('public', 'protected') THEN
        RAISE EXCEPTION 'redaction must be public or protected';
    END IF;
    v_generation := storage_v2_resolve_generation(p_source_id, p_generation_selector);
    WITH visible_occurrence AS (
        SELECT symbol_occurrence.*,
               stable_symbol.symbol_key, stable_symbol.language,
               stable_symbol.symbol_kind, stable_symbol.qualified_name,
               source_item.item_key, artifact.expected_content_hash
          FROM storage_v2_symbol_occurrence symbol_occurrence
          JOIN storage_v2_symbol stable_symbol ON stable_symbol.id = symbol_occurrence.symbol_id
          JOIN artifact_version artifact ON artifact.id = symbol_occurrence.artifact_version_id
          JOIN source_item ON source_item.id = artifact.item_id
          JOIN generation_item_version membership
            ON membership.source_id = p_source_id
           AND membership.source_item_id = artifact.item_id
           AND membership.artifact_version_id = artifact.id
         WHERE symbol_occurrence.source_id = p_source_id
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > v_generation.generation_seq)
    )
    SELECT jsonb_build_object(
        'profiles', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'profile_id', profile_id, 'profile_version', profile_version, 'rules', rules
        ) ORDER BY profile_id, profile_version) FROM storage_v2_intelligence_profile
          WHERE source_id = p_source_id), '[]'::JSONB),
        'cards', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'symbol_key', visible.symbol_key, 'language', visible.language,
            'symbol_kind', visible.symbol_kind, 'qualified_name', visible.qualified_name,
            'item_key', visible.item_key, 'content_hash', visible.expected_content_hash,
            'signature', visible.signature, 'documentation', visible.documentation,
            'visibility', visible.visibility, 'structure', visible.structure,
            'source_span', visible.source_span, 'analysis_profile_id', card.analysis_profile_id,
            'domain_profile_id', card.domain_profile_id,
            'domain_profile_version', card.domain_profile_version,
            'generic_card', card.generic_card, 'domain_fields', card.domain_fields,
            'field_provenance', card.field_provenance
        ) ORDER BY visible.symbol_key, card.analysis_profile_id)
          FROM visible_occurrence visible JOIN storage_v2_symbol_card card
            ON card.symbol_occurrence_id = visible.id), '[]'::JSONB),
        'annotations', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'symbol_key', stable_symbol.symbol_key, 'annotation_type', annotation.annotation_type,
            'value', annotation.value, 'provenance', annotation.provenance,
            'author_kind', annotation.author_kind, 'profile_id', annotation.profile_id,
            'profile_version', annotation.profile_version,
            'occurrence_item_key', annotation_item.item_key,
            'occurrence_content_hash', annotation_artifact.expected_content_hash,
            'occurrence_structural_sha256', encode(annotation_occurrence.structural_sha256, 'hex'),
            'created_by', annotation.created_by
        ) ORDER BY stable_symbol.symbol_key, annotation.annotation_type, annotation.value::TEXT)
          FROM storage_v2_symbol_annotation annotation
          JOIN storage_v2_symbol stable_symbol ON stable_symbol.id = annotation.symbol_id
          LEFT JOIN storage_v2_symbol_occurrence annotation_occurrence
            ON annotation_occurrence.id = annotation.symbol_occurrence_id
          LEFT JOIN artifact_version annotation_artifact
            ON annotation_artifact.id = annotation_occurrence.artifact_version_id
          LEFT JOIN source_item annotation_item ON annotation_item.id = annotation_artifact.item_id
         WHERE annotation.source_id = p_source_id), '[]'::JSONB),
        'entities', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'entity_key', entity.entity_key, 'symbol_key', stable_symbol.symbol_key,
            'name', entity.name, 'entity_type', entity.entity_type, 'payload', entity.payload
        ) ORDER BY entity.entity_key) FROM storage_v2_intelligence_entity entity
          LEFT JOIN storage_v2_symbol stable_symbol ON stable_symbol.id = entity.symbol_id
         WHERE entity.source_id = p_source_id), '[]'::JSONB),
        'relations', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'source_entity_key', source_entity.entity_key,
            'target_entity_key', target_entity.entity_key,
            'relation_type', relation.relation_type, 'evidence', relation.evidence
        ) ORDER BY source_entity.entity_key, target_entity.entity_key, relation.relation_type)
          FROM storage_v2_intelligence_relation relation
          JOIN storage_v2_intelligence_entity source_entity ON source_entity.id = relation.source_entity_id
          JOIN storage_v2_intelligence_entity target_entity ON target_entity.id = relation.target_entity_id
         WHERE relation.source_id = p_source_id), '[]'::JSONB),
        'call_edges', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'caller_symbol_key', caller_visible.symbol_key,
            'callee_symbol_key', callee_symbol.symbol_key,
            'call_kind', edge.call_kind, 'evidence', edge.evidence
        ) ORDER BY caller_visible.symbol_key, callee_symbol.symbol_key, edge.call_kind)
          FROM storage_v2_call_edge edge
          JOIN visible_occurrence caller_visible ON caller_visible.id = edge.caller_occurrence_id
          JOIN storage_v2_symbol callee_symbol ON callee_symbol.id = edge.callee_symbol_id
         WHERE edge.source_id = p_source_id), '[]'::JSONB),
        'unresolved_calls', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'caller_symbol_key', caller_visible.symbol_key, 'callee_name', unresolved.callee_name,
            'call_kind', unresolved.call_kind, 'evidence', unresolved.evidence,
            'candidate_symbol_keys', unresolved.candidate_symbol_keys
        ) ORDER BY caller_visible.symbol_key, unresolved.callee_name, unresolved.call_kind)
          FROM storage_v2_unresolved_call unresolved
          JOIN visible_occurrence caller_visible ON caller_visible.id = unresolved.caller_occurrence_id
         WHERE unresolved.source_id = p_source_id), '[]'::JSONB),
        'negative_evidence', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'evidence_key', evidence.evidence_key, 'concept', evidence.concept,
            'path_description', evidence.path_description, 'reason', evidence.reason,
            'symbol_keys', evidence.symbol_keys, 'severity', evidence.severity,
            'created_by', evidence.created_by
        ) ORDER BY evidence.evidence_key) FROM storage_v2_negative_evidence evidence
          WHERE evidence.source_id = p_source_id), '[]'::JSONB)
    ) INTO v_payload;
    v_protected_payload_sha256 := encode(
        digest(convert_to(v_payload::TEXT, 'UTF8'), 'sha256'), 'hex'
    );
    IF p_redaction = 'public' THEN
        v_payload := jsonb_build_object(
            'record_counts', jsonb_build_object(
                'profiles', jsonb_array_length(v_payload -> 'profiles'),
                'cards', jsonb_array_length(v_payload -> 'cards'),
                'annotations', jsonb_array_length(v_payload -> 'annotations'),
                'entities', jsonb_array_length(v_payload -> 'entities'),
                'relations', jsonb_array_length(v_payload -> 'relations'),
                'call_edges', jsonb_array_length(v_payload -> 'call_edges'),
                'unresolved_calls', jsonb_array_length(v_payload -> 'unresolved_calls'),
                'negative_evidence', jsonb_array_length(v_payload -> 'negative_evidence')
            ),
            'protected_payload_sha256', v_protected_payload_sha256
        );
    END IF;
    v_hash := encode(digest(convert_to(v_payload::TEXT, 'UTF8'), 'sha256'), 'hex');
    RETURN jsonb_build_object(
        'schema_version', 'mainrag.storage-v2-intelligence-export.v1',
        'redaction', p_redaction,
        'source_ref', encode(storage_v2_hash_parts('mainrag.export-source.v1', ARRAY[int8send(p_source_id)]), 'hex'),
        'generation_seq', v_generation.generation_seq,
        'payload_sha256', v_hash,
        'payload', v_payload
    );
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_import_intelligence(
    p_target_source_id BIGINT,
    p_target_generation_selector TEXT,
    p_bundle JSONB
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
    v_payload JSONB;
    v_record JSONB;
    v_hash TEXT;
    v_artifact_id BIGINT;
    v_occurrence_id BIGINT;
    v_symbol_occurrence storage_v2_symbol_occurrence;
    v_symbol_id BIGINT;
    v_annotation_occurrence_id BIGINT;
    v_card storage_v2_symbol_card;
    v_analysis storage_v2_intelligence_analysis;
    v_source_entity_id BIGINT;
    v_target_entity_id BIGINT;
    v_caller_occurrence_id BIGINT;
    v_callee_symbol_id BIGINT;
BEGIN
    IF NOT storage_v2_can_access_source(p_target_source_id, 'write')
       OR p_bundle ->> 'schema_version' IS DISTINCT FROM 'mainrag.storage-v2-intelligence-export.v1'
       OR p_bundle ->> 'redaction' IS DISTINCT FROM 'protected' THEN
        RAISE EXCEPTION 'authorized versioned intelligence bundle required' USING ERRCODE = '42501';
    END IF;
    v_payload := p_bundle -> 'payload';
    v_hash := encode(digest(convert_to(v_payload::TEXT, 'UTF8'), 'sha256'), 'hex');
    IF v_hash IS DISTINCT FROM p_bundle ->> 'payload_sha256' THEN
        RAISE EXCEPTION 'intelligence bundle hash mismatch';
    END IF;
    v_generation := storage_v2_resolve_generation(
        p_target_source_id, p_target_generation_selector
    );

    FOR v_record IN SELECT value FROM jsonb_array_elements(v_payload -> 'profiles') LOOP
        PERFORM storage_v2_put_intelligence_profile(
            p_target_source_id, v_record ->> 'profile_id',
            (v_record ->> 'profile_version')::BIGINT, v_record -> 'rules'
        );
    END LOOP;

    FOR v_record IN SELECT value FROM jsonb_array_elements(v_payload -> 'cards') LOOP
        SELECT artifact.id, occurrence_row.id INTO v_artifact_id, v_occurrence_id
          FROM source_item item
          JOIN generation_item_version membership
            ON membership.source_id = p_target_source_id
           AND membership.source_item_id = item.id
          JOIN artifact_version artifact ON artifact.id = membership.artifact_version_id
          JOIN occurrence occurrence_row
            ON occurrence_row.artifact_version_id = artifact.id
           AND occurrence_row.source_id = p_target_source_id
         WHERE item.source_id = p_target_source_id
           AND item.item_key = v_record ->> 'item_key'
           AND artifact.expected_content_hash = v_record ->> 'content_hash'
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > v_generation.generation_seq)
         ORDER BY occurrence_row.id LIMIT 1;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'target generation lacks mapped artifact for item %', v_record ->> 'item_key';
        END IF;
        v_symbol_occurrence := storage_v2_put_symbol_occurrence(
            p_target_source_id, v_artifact_id, v_occurrence_id,
            v_record ->> 'symbol_key', v_record ->> 'language',
            v_record ->> 'symbol_kind', v_record ->> 'qualified_name',
            v_record ->> 'signature', v_record ->> 'documentation',
            v_record ->> 'visibility', v_record -> 'structure', v_record -> 'source_span'
        );
        v_card := storage_v2_put_symbol_card(
            v_symbol_occurrence.id, v_record ->> 'analysis_profile_id',
            v_record -> 'generic_card', v_record -> 'domain_fields',
            v_record -> 'field_provenance', v_record ->> 'domain_profile_id',
            (v_record ->> 'domain_profile_version')::BIGINT
        );
        v_analysis := storage_v2_begin_intelligence_analysis(
            v_symbol_occurrence.id, v_record ->> 'analysis_profile_id'
        );
        IF v_analysis.status = 'pending' THEN
            PERFORM storage_v2_finish_intelligence_analysis(
                v_symbol_occurrence.id, v_record ->> 'analysis_profile_id',
                v_card.output_sha256, NULL
            );
        ELSIF v_analysis.output_sha256 <> v_card.output_sha256 THEN
            RAISE EXCEPTION 'complete analysis output differs from imported card';
        END IF;
    END LOOP;

    FOR v_record IN SELECT value FROM jsonb_array_elements(v_payload -> 'annotations') LOOP
        SELECT id INTO v_symbol_id FROM storage_v2_symbol
         WHERE source_id = p_target_source_id AND symbol_key = v_record ->> 'symbol_key';
        IF NOT FOUND THEN RAISE EXCEPTION 'annotation symbol mapping missing'; END IF;
        v_annotation_occurrence_id := NULL;
        IF v_record ->> 'occurrence_item_key' IS NOT NULL THEN
            SELECT symbol_occurrence.id INTO v_annotation_occurrence_id
              FROM storage_v2_symbol_occurrence symbol_occurrence
              JOIN artifact_version artifact
                ON artifact.id = symbol_occurrence.artifact_version_id
              JOIN source_item item ON item.id = artifact.item_id
              JOIN generation_item_version membership
                ON membership.source_id = p_target_source_id
               AND membership.source_item_id = artifact.item_id
               AND membership.artifact_version_id = artifact.id
             WHERE symbol_occurrence.source_id = p_target_source_id
               AND symbol_occurrence.symbol_id = v_symbol_id
               AND item.item_key = v_record ->> 'occurrence_item_key'
               AND artifact.expected_content_hash = v_record ->> 'occurrence_content_hash'
               AND encode(symbol_occurrence.structural_sha256, 'hex')
                   = v_record ->> 'occurrence_structural_sha256'
               AND membership.valid_from_seq <= v_generation.generation_seq
               AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > v_generation.generation_seq)
             ORDER BY symbol_occurrence.id LIMIT 1;
            IF NOT FOUND THEN RAISE EXCEPTION 'annotation occurrence mapping missing'; END IF;
        END IF;
        PERFORM storage_v2_put_symbol_annotation(
            p_target_source_id, v_symbol_id, v_annotation_occurrence_id,
            v_record ->> 'annotation_type',
            v_record -> 'value', v_record -> 'provenance', v_record ->> 'author_kind',
            v_record ->> 'profile_id', (v_record ->> 'profile_version')::BIGINT,
            v_record ->> 'created_by'
        );
    END LOOP;

    FOR v_record IN SELECT value FROM jsonb_array_elements(v_payload -> 'entities') LOOP
        SELECT id INTO v_symbol_id FROM storage_v2_symbol
         WHERE source_id = p_target_source_id AND symbol_key = v_record ->> 'symbol_key';
        PERFORM storage_v2_put_intelligence_entity(
            p_target_source_id, v_record ->> 'entity_key', v_symbol_id,
            v_record ->> 'name', v_record ->> 'entity_type', v_record -> 'payload'
        );
    END LOOP;

    FOR v_record IN SELECT value FROM jsonb_array_elements(v_payload -> 'relations') LOOP
        SELECT id INTO STRICT v_source_entity_id FROM storage_v2_intelligence_entity
         WHERE source_id = p_target_source_id AND entity_key = v_record ->> 'source_entity_key';
        SELECT id INTO STRICT v_target_entity_id FROM storage_v2_intelligence_entity
         WHERE source_id = p_target_source_id AND entity_key = v_record ->> 'target_entity_key';
        PERFORM storage_v2_put_intelligence_relation(
            p_target_source_id, v_source_entity_id, v_target_entity_id,
            v_record ->> 'relation_type', v_record -> 'evidence'
        );
    END LOOP;

    FOR v_record IN SELECT value FROM jsonb_array_elements(v_payload -> 'call_edges') LOOP
        SELECT symbol_occurrence.id INTO v_caller_occurrence_id
          FROM storage_v2_symbol stable_symbol
          JOIN storage_v2_symbol_occurrence symbol_occurrence
            ON symbol_occurrence.symbol_id = stable_symbol.id
          JOIN artifact_version artifact
            ON artifact.id = symbol_occurrence.artifact_version_id
          JOIN generation_item_version membership
            ON membership.source_id = p_target_source_id
           AND membership.source_item_id = artifact.item_id
           AND membership.artifact_version_id = artifact.id
         WHERE stable_symbol.source_id = p_target_source_id
           AND stable_symbol.symbol_key = v_record ->> 'caller_symbol_key'
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > v_generation.generation_seq)
         ORDER BY symbol_occurrence.id DESC LIMIT 1;
        IF NOT FOUND THEN RAISE EXCEPTION 'call-edge caller mapping missing in target generation'; END IF;
        SELECT id INTO v_callee_symbol_id FROM storage_v2_symbol
         WHERE source_id = p_target_source_id
           AND symbol_key = v_record ->> 'callee_symbol_key';
        IF NOT FOUND THEN RAISE EXCEPTION 'call-edge callee mapping missing'; END IF;
        PERFORM storage_v2_record_call(
            v_caller_occurrence_id, v_callee_symbol_id,
            (SELECT qualified_name FROM storage_v2_symbol WHERE id = v_callee_symbol_id),
            v_record ->> 'call_kind', v_record -> 'evidence', '[]'::JSONB
        );
    END LOOP;

    FOR v_record IN SELECT value FROM jsonb_array_elements(v_payload -> 'unresolved_calls') LOOP
        SELECT symbol_occurrence.id INTO v_caller_occurrence_id
          FROM storage_v2_symbol stable_symbol
          JOIN storage_v2_symbol_occurrence symbol_occurrence
            ON symbol_occurrence.symbol_id = stable_symbol.id
          JOIN artifact_version artifact
            ON artifact.id = symbol_occurrence.artifact_version_id
          JOIN generation_item_version membership
            ON membership.source_id = p_target_source_id
           AND membership.source_item_id = artifact.item_id
           AND membership.artifact_version_id = artifact.id
         WHERE stable_symbol.source_id = p_target_source_id
           AND stable_symbol.symbol_key = v_record ->> 'caller_symbol_key'
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > v_generation.generation_seq)
         ORDER BY symbol_occurrence.id DESC LIMIT 1;
        IF NOT FOUND THEN RAISE EXCEPTION 'unresolved-call caller mapping missing in target generation'; END IF;
        PERFORM storage_v2_record_call(
            v_caller_occurrence_id, NULL, v_record ->> 'callee_name',
            v_record ->> 'call_kind', v_record -> 'evidence',
            v_record -> 'candidate_symbol_keys'
        );
    END LOOP;

    FOR v_record IN SELECT value FROM jsonb_array_elements(v_payload -> 'negative_evidence') LOOP
        PERFORM storage_v2_put_negative_evidence(
            p_target_source_id, v_record ->> 'evidence_key', v_record ->> 'concept',
            v_record ->> 'path_description', v_record ->> 'reason',
            v_record -> 'symbol_keys', v_record ->> 'severity', v_record ->> 'created_by'
        );
    END LOOP;

    RETURN jsonb_build_object(
        'schema_version', p_bundle ->> 'schema_version',
        'payload_sha256', v_hash,
        'cards', jsonb_array_length(v_payload -> 'cards'),
        'annotations', jsonb_array_length(v_payload -> 'annotations'),
        'entities', jsonb_array_length(v_payload -> 'entities'),
        'relations', jsonb_array_length(v_payload -> 'relations'),
        'call_edges', jsonb_array_length(v_payload -> 'call_edges'),
        'unresolved_calls', jsonb_array_length(v_payload -> 'unresolved_calls'),
        'negative_evidence', jsonb_array_length(v_payload -> 'negative_evidence')
    );
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_intelligence_command(
    p_source_id BIGINT,
    p_generation_selector TEXT,
    p_command TEXT,
    p_query JSONB DEFAULT '{}'::JSONB
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_payload JSONB;
    v_symbol_key TEXT;
    v_name TEXT;
BEGIN
    v_payload := storage_v2_export_intelligence(
        p_source_id, p_generation_selector, 'protected'
    ) -> 'payload';
    IF p_command IN ('card', 'layers') THEN
        RETURN COALESCE((
            SELECT jsonb_agg(card ORDER BY card ->> 'symbol_key', card ->> 'analysis_profile_id')
              FROM jsonb_array_elements(v_payload -> 'cards') card
             WHERE (COALESCE(p_query ->> 'name', '') = ''
                    OR card -> 'generic_card' ->> 'name' ILIKE '%' || (p_query ->> 'name') || '%')
               AND (COALESCE(p_query ->> 'layer', '') = ''
                    OR card -> 'domain_fields' ->> 'layer' = p_query ->> 'layer')
               AND (COALESCE(p_query ->> 'resource', '') = ''
                    OR card -> 'domain_fields' ->> 'resource' = p_query ->> 'resource')
               AND (COALESCE(p_query ->> 'side_effect', '') = ''
                    OR card -> 'domain_fields' ->> 'side_effect' = p_query ->> 'side_effect')
        ), '[]'::JSONB);
    ELSIF p_command = 'explain' THEN
        v_name := p_query ->> 'name';
        SELECT card ->> 'symbol_key' INTO v_symbol_key
          FROM jsonb_array_elements(v_payload -> 'cards') card
         WHERE card -> 'generic_card' ->> 'name' = v_name
            OR card ->> 'qualified_name' = v_name
         ORDER BY card ->> 'symbol_key' LIMIT 1;
        IF v_symbol_key IS NULL THEN RETURN jsonb_build_object(
            'symbol_key', NULL, 'proven', '[]'::JSONB, 'unresolved', '[]'::JSONB
        ); END IF;
        RETURN jsonb_build_object(
            'symbol_key', v_symbol_key,
            'proven', COALESCE((SELECT jsonb_agg(edge ORDER BY edge::TEXT)
                FROM jsonb_array_elements(v_payload -> 'call_edges') edge
               WHERE edge ->> 'caller_symbol_key' = v_symbol_key), '[]'::JSONB),
            'unresolved', COALESCE((SELECT jsonb_agg(call_site ORDER BY call_site::TEXT)
                FROM jsonb_array_elements(v_payload -> 'unresolved_calls') call_site
               WHERE call_site ->> 'caller_symbol_key' = v_symbol_key), '[]'::JSONB)
        );
    ELSIF p_command = 'ownership' THEN
        v_name := p_query ->> 'name';
        RETURN COALESCE((
            SELECT jsonb_agg(relation ORDER BY relation::TEXT)
              FROM jsonb_array_elements(v_payload -> 'relations') relation
             WHERE relation ->> 'source_entity_key' IN (
                    SELECT entity ->> 'entity_key'
                      FROM jsonb_array_elements(v_payload -> 'entities') entity
                     WHERE entity ->> 'name' = v_name
                )
                OR relation ->> 'target_entity_key' IN (
                    SELECT entity ->> 'entity_key'
                      FROM jsonb_array_elements(v_payload -> 'entities') entity
                     WHERE entity ->> 'name' = v_name
                )
        ), '[]'::JSONB);
    END IF;
    RAISE EXCEPTION 'unsupported storage-v2 intelligence command';
END
$$;

REVOKE INSERT, UPDATE, DELETE ON storage_v2_intelligence_profile FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_symbol FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_symbol_occurrence FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_intelligence_analysis FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_symbol_card FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_symbol_annotation FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_call_edge FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_unresolved_call FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_intelligence_entity FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_intelligence_relation FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON storage_v2_negative_evidence FROM PUBLIC;
