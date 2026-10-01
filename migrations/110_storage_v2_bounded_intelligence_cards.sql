-- Migration 110: bound runtime card reads before JSON aggregation.
-- The active source envelope and its authorization/activation fences remain.
-- Omitted limits retain complete named-generation/export command behavior.
-- CREATE OR REPLACE preserves existing function owners, ACLs and identities.

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
    v_generation source_generation;
    v_symbol_key TEXT;
    v_name TEXT;
    v_result JSONB;
    v_limit BIGINT;
BEGIN
    v_generation := storage_v2_resolve_generation(p_source_id, p_generation_selector);
    IF p_command IN ('card', 'layers') THEN
        -- A missing/null limit retains complete named-generation reads. Runtime
        -- callers supply a bounded limit; reject malformed values, never coerce.
        IF p_query -> 'limit' IS NOT NULL AND p_query -> 'limit' <> 'null'::JSONB THEN
            IF jsonb_typeof(p_query -> 'limit') <> 'number'
               OR (p_query ->> 'limit') !~ '^[0-9]{1,3}$' THEN
                RAISE EXCEPTION 'intelligence limit must be an integer from 1 to 200'
                    USING ERRCODE = '22023';
            END IF;
            v_limit := (p_query ->> 'limit')::BIGINT;
            IF v_limit < 1 OR v_limit > 200 THEN
                RAISE EXCEPTION 'intelligence limit must be an integer from 1 to 200'
                    USING ERRCODE = '22023';
            END IF;
        END IF;
        SELECT COALESCE(jsonb_agg(value ORDER BY value ->> 'symbol_key',
                                                value ->> 'analysis_profile_id',
                                                item_key, occurrence_id), '[]'::JSONB)
          INTO v_result
          FROM (
            SELECT jsonb_build_object(
                'symbol_key', stable_symbol.symbol_key, 'language', stable_symbol.language,
                'symbol_kind', stable_symbol.symbol_kind, 'qualified_name', stable_symbol.qualified_name,
                'item_key', source_item.item_key, 'content_hash', artifact.expected_content_hash,
                'signature', visible.signature, 'documentation', visible.documentation,
                'visibility', visible.visibility, 'structure', visible.structure,
                'source_span', visible.source_span, 'analysis_profile_id', card.analysis_profile_id,
                'domain_profile_id', card.domain_profile_id,
                'domain_profile_version', card.domain_profile_version,
                'generic_card', card.generic_card, 'domain_fields', card.domain_fields,
                'field_provenance', card.field_provenance
            ) AS value, source_item.item_key, visible.id AS occurrence_id
              FROM storage_v2_symbol_occurrence visible
              JOIN storage_v2_symbol stable_symbol ON stable_symbol.id = visible.symbol_id
              JOIN artifact_version artifact ON artifact.id = visible.artifact_version_id
              JOIN source_item ON source_item.id = artifact.item_id
              JOIN generation_item_version membership
                ON membership.source_id = p_source_id
               AND membership.source_item_id = artifact.item_id
               AND membership.artifact_version_id = artifact.id
              JOIN storage_v2_symbol_card card ON card.symbol_occurrence_id = visible.id
             WHERE visible.source_id = p_source_id
               AND membership.valid_from_seq <= v_generation.generation_seq
               AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > v_generation.generation_seq)
               AND (COALESCE(p_query ->> 'name', '') = ''
                    OR card.generic_card ->> 'name' ILIKE '%' || (p_query ->> 'name') || '%')
               AND (COALESCE(p_query ->> 'layer', '') = ''
                    OR card.domain_fields ->> 'layer' = p_query ->> 'layer')
               AND (COALESCE(p_query ->> 'resource', '') = ''
                    OR card.domain_fields ->> 'resource' = p_query ->> 'resource')
               AND (COALESCE(p_query ->> 'side_effect', '') = ''
                    OR card.domain_fields ->> 'side_effect' = p_query ->> 'side_effect')
             ORDER BY stable_symbol.symbol_key, card.analysis_profile_id,
                      source_item.item_key, visible.id
             LIMIT v_limit
          ) cards;
        RETURN v_result;
    ELSIF p_command = 'explain' THEN
        v_name := p_query ->> 'name';
        SELECT stable_symbol.symbol_key INTO v_symbol_key
          FROM storage_v2_symbol_occurrence visible
          JOIN storage_v2_symbol stable_symbol ON stable_symbol.id = visible.symbol_id
          JOIN artifact_version artifact ON artifact.id = visible.artifact_version_id
          JOIN source_item ON source_item.id = artifact.item_id
          JOIN generation_item_version membership
            ON membership.source_id = p_source_id
           AND membership.source_item_id = artifact.item_id
           AND membership.artifact_version_id = artifact.id
          JOIN storage_v2_symbol_card card ON card.symbol_occurrence_id = visible.id
         WHERE visible.source_id = p_source_id
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > v_generation.generation_seq)
           AND (card.generic_card ->> 'name' = v_name OR stable_symbol.qualified_name = v_name)
         ORDER BY stable_symbol.symbol_key LIMIT 1;
        IF v_symbol_key IS NULL THEN RETURN jsonb_build_object(
            'symbol_key', NULL, 'proven', '[]'::JSONB, 'unresolved', '[]'::JSONB
        ); END IF;
        WITH visible_caller AS (
            SELECT visible.id, stable_symbol.symbol_key
              FROM storage_v2_symbol_occurrence visible
              JOIN storage_v2_symbol stable_symbol ON stable_symbol.id = visible.symbol_id
              JOIN artifact_version artifact ON artifact.id = visible.artifact_version_id
              JOIN source_item ON source_item.id = artifact.item_id
              JOIN generation_item_version membership
                ON membership.source_id = p_source_id
               AND membership.source_item_id = artifact.item_id
               AND membership.artifact_version_id = artifact.id
             WHERE visible.source_id = p_source_id
               AND stable_symbol.symbol_key = v_symbol_key
               AND membership.valid_from_seq <= v_generation.generation_seq
               AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > v_generation.generation_seq)
        ), proven AS (
            SELECT jsonb_build_object(
                'caller_symbol_key', caller.symbol_key, 'callee_symbol_key', callee.symbol_key,
                'call_kind', edge.call_kind, 'evidence', edge.evidence
            ) AS value
              FROM storage_v2_call_edge edge
              JOIN visible_caller caller ON caller.id = edge.caller_occurrence_id
              JOIN storage_v2_symbol callee ON callee.id = edge.callee_symbol_id
             WHERE edge.source_id = p_source_id
        ), unresolved AS (
            SELECT jsonb_build_object(
                'caller_symbol_key', caller.symbol_key, 'callee_name', call_site.callee_name,
                'call_kind', call_site.call_kind, 'evidence', call_site.evidence,
                'candidate_symbol_keys', call_site.candidate_symbol_keys
            ) AS value
              FROM storage_v2_unresolved_call call_site
              JOIN visible_caller caller ON caller.id = call_site.caller_occurrence_id
             WHERE call_site.source_id = p_source_id
        )
        SELECT jsonb_build_object(
            'symbol_key', v_symbol_key,
            'proven', COALESCE((SELECT jsonb_agg(value ORDER BY value::TEXT) FROM proven), '[]'::JSONB),
            'unresolved', COALESCE((SELECT jsonb_agg(value ORDER BY value::TEXT) FROM unresolved), '[]'::JSONB)
        ) INTO v_result;
        RETURN v_result;
    ELSIF p_command = 'ownership' THEN
        v_name := p_query ->> 'name';
        SELECT COALESCE(jsonb_agg(value ORDER BY value::TEXT), '[]'::JSONB) INTO v_result
          FROM (
            SELECT jsonb_build_object(
                'source_entity_key', source_entity.entity_key,
                'target_entity_key', target_entity.entity_key,
                'relation_type', relation.relation_type, 'evidence', relation.evidence
            ) AS value
              FROM storage_v2_intelligence_relation relation
              JOIN storage_v2_intelligence_entity source_entity ON source_entity.id = relation.source_entity_id
              JOIN storage_v2_intelligence_entity target_entity ON target_entity.id = relation.target_entity_id
             WHERE relation.source_id = p_source_id
               AND (source_entity.entity_key IN (
                        SELECT entity_key FROM storage_v2_intelligence_entity
                         WHERE source_id = p_source_id AND name = v_name
                    ) OR target_entity.entity_key IN (
                        SELECT entity_key FROM storage_v2_intelligence_entity
                         WHERE source_id = p_source_id AND name = v_name
                    ))
          ) relations;
        RETURN v_result;
    END IF;
    RAISE EXCEPTION 'unsupported storage-v2 intelligence command';
END
$$;

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
    v_remaining BIGINT;
    v_query JSONB := p_query;
    v_value JSONB;
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
    IF p_command IN ('card', 'layers') AND p_query -> 'limit' IS NOT NULL
       AND p_query -> 'limit' <> 'null'::JSONB THEN
        IF jsonb_typeof(p_query -> 'limit') <> 'number'
           OR (p_query ->> 'limit') !~ '^[0-9]{1,3}$' THEN
            RAISE EXCEPTION 'intelligence limit must be an integer from 1 to 200'
                USING ERRCODE = '22023';
        END IF;
        v_remaining := (p_query ->> 'limit')::BIGINT;
        IF v_remaining < 1 OR v_remaining > 200 THEN
            RAISE EXCEPTION 'intelligence limit must be an integer from 1 to 200'
                USING ERRCODE = '22023';
        END IF;
    END IF;
    FOR v_source IN
        SELECT source.id, source.is_test FROM sources source
         WHERE (p_source_id IS NULL OR source.id = p_source_id)
           AND (p_include_test OR NOT source.is_test)
         ORDER BY source.id
    LOOP
        IF storage_v2_can_access_source(v_source.id, 'read') THEN
            IF v_remaining = 0 THEN
                -- Preserve authorized source envelopes without reading another
                -- card collection after the request-wide budget is exhausted.
                v_value := '[]'::JSONB;
            ELSE
                IF v_remaining IS NOT NULL THEN
                    v_query := jsonb_set(p_query, '{limit}', to_jsonb(v_remaining));
                END IF;
                v_value := storage_v2_intelligence_command(
                    v_source.id, 'current', p_command, v_query
                );
                IF v_remaining IS NOT NULL THEN
                    v_remaining := v_remaining - jsonb_array_length(v_value);
                END IF;
            END IF;
            v_results := v_results || jsonb_build_array(jsonb_build_object(
                'source_id', v_source.id, 'value', v_value
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
