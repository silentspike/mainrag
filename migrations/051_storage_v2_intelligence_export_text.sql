-- Migration 051: serialize ordered intelligence collections without building
-- the complete protected JSONB tree just to discard it for a public digest.
-- Each record still uses native JSONB serialization. Arrays retain their v1
-- ordering and spacing; root keys use JSONB byte-length/binary ordering.
-- Public and protected exports keep the same v1 payload and full SHA-256.
-- This avoids expanded JSONB aggregate state, not the bounded TEXT size limit.

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
    v_payload_text TEXT;
    v_record_counts JSONB;
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
    ), collection AS (
        SELECT 'profiles'::TEXT AS kind, COUNT(*) AS record_count,
               COALESCE('[' || string_agg(jsonb_build_object(
            'profile_id', profile_id, 'profile_version', profile_version, 'rules', rules
        )::TEXT,
                   ', ' ORDER BY profile_id, profile_version) || ']', '[]') AS value
          FROM storage_v2_intelligence_profile
          WHERE source_id = p_source_id
        UNION ALL
        SELECT 'cards'::TEXT AS kind, COUNT(*) AS record_count,
               COALESCE('[' || string_agg(jsonb_build_object(
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
        )::TEXT,
                   ', ' ORDER BY visible.symbol_key, card.analysis_profile_id) || ']', '[]') AS value
          FROM visible_occurrence visible JOIN storage_v2_symbol_card card
            ON card.symbol_occurrence_id = visible.id
        UNION ALL
        SELECT 'annotations'::TEXT AS kind, COUNT(*) AS record_count,
               COALESCE('[' || string_agg(jsonb_build_object(
            'symbol_key', stable_symbol.symbol_key, 'annotation_type', annotation.annotation_type,
            'value', annotation.value, 'provenance', annotation.provenance,
            'author_kind', annotation.author_kind, 'profile_id', annotation.profile_id,
            'profile_version', annotation.profile_version,
            'occurrence_item_key', annotation_item.item_key,
            'occurrence_content_hash', annotation_artifact.expected_content_hash,
            'occurrence_structural_sha256', encode(annotation_occurrence.structural_sha256, 'hex'),
            'created_by', annotation.created_by
        )::TEXT,
                   ', ' ORDER BY stable_symbol.symbol_key, annotation.annotation_type, annotation.value::TEXT) || ']', '[]') AS value
          FROM storage_v2_symbol_annotation annotation
          JOIN storage_v2_symbol stable_symbol ON stable_symbol.id = annotation.symbol_id
          LEFT JOIN storage_v2_symbol_occurrence annotation_occurrence
            ON annotation_occurrence.id = annotation.symbol_occurrence_id
          LEFT JOIN artifact_version annotation_artifact
            ON annotation_artifact.id = annotation_occurrence.artifact_version_id
          LEFT JOIN source_item annotation_item ON annotation_item.id = annotation_artifact.item_id
         WHERE annotation.source_id = p_source_id
        UNION ALL
        SELECT 'entities'::TEXT AS kind, COUNT(*) AS record_count,
               COALESCE('[' || string_agg(jsonb_build_object(
            'entity_key', entity.entity_key, 'symbol_key', stable_symbol.symbol_key,
            'name', entity.name, 'entity_type', entity.entity_type, 'payload', entity.payload
        )::TEXT,
                   ', ' ORDER BY entity.entity_key) || ']', '[]') AS value
          FROM storage_v2_intelligence_entity entity
          LEFT JOIN storage_v2_symbol stable_symbol ON stable_symbol.id = entity.symbol_id
         WHERE entity.source_id = p_source_id
        UNION ALL
        SELECT 'relations'::TEXT AS kind, COUNT(*) AS record_count,
               COALESCE('[' || string_agg(jsonb_build_object(
            'source_entity_key', source_entity.entity_key,
            'target_entity_key', target_entity.entity_key,
            'relation_type', relation.relation_type, 'evidence', relation.evidence
        )::TEXT,
                   ', ' ORDER BY source_entity.entity_key, target_entity.entity_key, relation.relation_type) || ']', '[]') AS value
          FROM storage_v2_intelligence_relation relation
          JOIN storage_v2_intelligence_entity source_entity ON source_entity.id = relation.source_entity_id
          JOIN storage_v2_intelligence_entity target_entity ON target_entity.id = relation.target_entity_id
         WHERE relation.source_id = p_source_id
        UNION ALL
        SELECT 'call_edges'::TEXT AS kind, COUNT(*) AS record_count,
               COALESCE('[' || string_agg(jsonb_build_object(
            'caller_symbol_key', caller_visible.symbol_key,
            'callee_symbol_key', callee_symbol.symbol_key,
            'call_kind', edge.call_kind, 'evidence', edge.evidence
        )::TEXT,
                   ', ' ORDER BY caller_visible.symbol_key, callee_symbol.symbol_key, edge.call_kind) || ']', '[]') AS value
          FROM storage_v2_call_edge edge
          JOIN visible_occurrence caller_visible ON caller_visible.id = edge.caller_occurrence_id
          JOIN storage_v2_symbol callee_symbol ON callee_symbol.id = edge.callee_symbol_id
         WHERE edge.source_id = p_source_id
        UNION ALL
        SELECT 'unresolved_calls'::TEXT AS kind, COUNT(*) AS record_count,
               COALESCE('[' || string_agg(jsonb_build_object(
            'caller_symbol_key', caller_visible.symbol_key, 'callee_name', unresolved.callee_name,
            'call_kind', unresolved.call_kind, 'evidence', unresolved.evidence,
            'candidate_symbol_keys', unresolved.candidate_symbol_keys
        )::TEXT,
                   ', ' ORDER BY caller_visible.symbol_key, unresolved.callee_name, unresolved.call_kind) || ']', '[]') AS value
          FROM storage_v2_unresolved_call unresolved
          JOIN visible_occurrence caller_visible ON caller_visible.id = unresolved.caller_occurrence_id
         WHERE unresolved.source_id = p_source_id
        UNION ALL
        SELECT 'negative_evidence'::TEXT AS kind, COUNT(*) AS record_count,
               COALESCE('[' || string_agg(jsonb_build_object(
            'evidence_key', evidence.evidence_key, 'concept', evidence.concept,
            'path_description', evidence.path_description, 'reason', evidence.reason,
            'symbol_keys', evidence.symbol_keys, 'severity', evidence.severity,
            'created_by', evidence.created_by
        )::TEXT,
                   ', ' ORDER BY evidence.evidence_key) || ']', '[]') AS value
          FROM storage_v2_negative_evidence evidence
          WHERE evidence.source_id = p_source_id
    )
    SELECT '{' || string_agg(to_json(kind)::TEXT || ': ' || value, ', '
               ORDER BY octet_length(kind), kind COLLATE "C") || '}',
           jsonb_object_agg(kind, record_count)
      INTO v_payload_text, v_record_counts
      FROM collection;
    v_protected_payload_sha256 := encode(
        digest(convert_to(v_payload_text, 'UTF8'), 'sha256'), 'hex'
    );
    IF p_redaction = 'public' THEN
        v_payload := jsonb_build_object(
            'record_counts', v_record_counts,
            'protected_payload_sha256', v_protected_payload_sha256
        );
    ELSE
        v_payload := v_payload_text::JSONB;
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

