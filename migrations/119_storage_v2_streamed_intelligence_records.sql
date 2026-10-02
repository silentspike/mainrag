-- Stream every ordered intelligence record without a source-wide JSON datum.
-- The v1 exporter remains unchanged. Its native JSONB record serialization,
-- collection order, fields, source authorization and generation scope are reused.
BEGIN;
DO $guard$ BEGIN
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_export_intelligence(bigint,text,text)'::regprocedure),'UTF8')),'hex') <> '9943f95e412412bb4d24611dd03140282feec4c062a99523d9968780d68e80d3' THEN
  RAISE EXCEPTION 'intelligence export definition differs';
 END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_export_intelligence(bigint,text,text)'::regprocedure) <> 'mainrag'::regrole THEN
  RAISE EXCEPTION 'intelligence export authority differs';
 END IF;

 IF to_regprocedure('public.storage_v2_intelligence_export_records(bigint,text,text)') IS NOT NULL THEN
  IF EXISTS (SELECT 1 FROM pg_proc routine
    WHERE routine.oid='public.storage_v2_intelligence_export_records(bigint,text,text)'::regprocedure
      AND (encode(sha256(convert_to(routine.prosrc,'UTF8')),'hex') <> '8395bd37764a1747fc6ecb9d58f6e53a67bd2aa10f5516c1bbeb127804068e80'
       OR routine.proowner <> 'mainrag'::regrole
       OR routine.prolang <> (SELECT oid FROM pg_language WHERE lanname='plpgsql')
       OR routine.provolatile <> 's' OR NOT routine.prosecdef OR routine.proisstrict
       OR routine.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, public','row_security=off']
       OR routine.prorettype <> 'record'::regtype OR NOT routine.proretset)) THEN
   RAISE EXCEPTION 'streamed intelligence export identity differs';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
    aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission
    WHERE routine.oid='public.storage_v2_intelligence_export_records(bigint,text,text)'::regprocedure
      AND (permission.grantee <> 'mainrag'::regrole OR permission.privilege_type <> 'EXECUTE'
        OR (permission.is_grantable AND permission.grantee <> routine.proowner))) THEN
   RAISE EXCEPTION 'streamed intelligence export authority differs';
  END IF;
 END IF;
END $guard$;
CREATE OR REPLACE FUNCTION storage_v2_intelligence_export_records(
 p_source_id BIGINT, p_generation_selector TEXT, p_collection TEXT
) RETURNS TABLE(record_ordinal BIGINT, record_text TEXT)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $function$
DECLARE v_generation source_generation;
BEGIN
 IF p_collection IS NULL OR p_collection NOT IN ('profiles','cards','annotations','entities','relations','call_edges','unresolved_calls','negative_evidence') THEN
  RAISE EXCEPTION 'known intelligence export collection required';
 END IF;
 v_generation := storage_v2_resolve_generation(p_source_id,p_generation_selector);
    IF p_collection = 'profiles' THEN
        RETURN QUERY
        SELECT row_number() OVER (ORDER BY profile_id, profile_version),
               jsonb_build_object(
            'profile_id', profile_id, 'profile_version', profile_version, 'rules', rules
        )::TEXT
          FROM storage_v2_intelligence_profile
          WHERE source_id = p_source_id
         ORDER BY profile_id, profile_version;
    ELSIF p_collection = 'cards' THEN
        RETURN QUERY
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
        SELECT row_number() OVER (ORDER BY visible.symbol_key, card.analysis_profile_id),
               jsonb_build_object(
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
        )::TEXT
          FROM visible_occurrence visible JOIN storage_v2_symbol_card card
            ON card.symbol_occurrence_id = visible.id
         ORDER BY visible.symbol_key, card.analysis_profile_id;
    ELSIF p_collection = 'annotations' THEN
        RETURN QUERY
        SELECT row_number() OVER (ORDER BY stable_symbol.symbol_key, annotation.annotation_type, annotation.value::TEXT),
               jsonb_build_object(
            'symbol_key', stable_symbol.symbol_key, 'annotation_type', annotation.annotation_type,
            'value', annotation.value, 'provenance', annotation.provenance,
            'author_kind', annotation.author_kind, 'profile_id', annotation.profile_id,
            'profile_version', annotation.profile_version,
            'occurrence_item_key', annotation_item.item_key,
            'occurrence_content_hash', annotation_artifact.expected_content_hash,
            'occurrence_structural_sha256', encode(annotation_occurrence.structural_sha256, 'hex'),
            'created_by', annotation.created_by
        )::TEXT
          FROM storage_v2_symbol_annotation annotation
          JOIN storage_v2_symbol stable_symbol ON stable_symbol.id = annotation.symbol_id
          LEFT JOIN storage_v2_symbol_occurrence annotation_occurrence
            ON annotation_occurrence.id = annotation.symbol_occurrence_id
          LEFT JOIN artifact_version annotation_artifact
            ON annotation_artifact.id = annotation_occurrence.artifact_version_id
          LEFT JOIN source_item annotation_item ON annotation_item.id = annotation_artifact.item_id
         WHERE annotation.source_id = p_source_id
         ORDER BY stable_symbol.symbol_key, annotation.annotation_type, annotation.value::TEXT;
    ELSIF p_collection = 'entities' THEN
        RETURN QUERY
        SELECT row_number() OVER (ORDER BY entity.entity_key),
               jsonb_build_object(
            'entity_key', entity.entity_key, 'symbol_key', stable_symbol.symbol_key,
            'name', entity.name, 'entity_type', entity.entity_type, 'payload', entity.payload
        )::TEXT
          FROM storage_v2_intelligence_entity entity
          LEFT JOIN storage_v2_symbol stable_symbol ON stable_symbol.id = entity.symbol_id
         WHERE entity.source_id = p_source_id
         ORDER BY entity.entity_key;
    ELSIF p_collection = 'relations' THEN
        RETURN QUERY
        SELECT row_number() OVER (ORDER BY source_entity.entity_key, target_entity.entity_key, relation.relation_type),
               jsonb_build_object(
            'source_entity_key', source_entity.entity_key,
            'target_entity_key', target_entity.entity_key,
            'relation_type', relation.relation_type, 'evidence', relation.evidence
        )::TEXT
          FROM storage_v2_intelligence_relation relation
          JOIN storage_v2_intelligence_entity source_entity ON source_entity.id = relation.source_entity_id
          JOIN storage_v2_intelligence_entity target_entity ON target_entity.id = relation.target_entity_id
         WHERE relation.source_id = p_source_id
         ORDER BY source_entity.entity_key, target_entity.entity_key, relation.relation_type;
    ELSIF p_collection = 'call_edges' THEN
        RETURN QUERY
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
        SELECT row_number() OVER (ORDER BY caller_visible.symbol_key, callee_symbol.symbol_key, edge.call_kind),
               jsonb_build_object(
            'caller_symbol_key', caller_visible.symbol_key,
            'callee_symbol_key', callee_symbol.symbol_key,
            'call_kind', edge.call_kind, 'evidence', edge.evidence
        )::TEXT
          FROM storage_v2_call_edge edge
          JOIN visible_occurrence caller_visible ON caller_visible.id = edge.caller_occurrence_id
          JOIN storage_v2_symbol callee_symbol ON callee_symbol.id = edge.callee_symbol_id
         WHERE edge.source_id = p_source_id
         ORDER BY caller_visible.symbol_key, callee_symbol.symbol_key, edge.call_kind;
    ELSIF p_collection = 'unresolved_calls' THEN
        RETURN QUERY
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
        SELECT row_number() OVER (ORDER BY caller_visible.symbol_key, unresolved.callee_name, unresolved.call_kind),
               jsonb_build_object(
            'caller_symbol_key', caller_visible.symbol_key, 'callee_name', unresolved.callee_name,
            'call_kind', unresolved.call_kind, 'evidence', unresolved.evidence,
            'candidate_symbol_keys', unresolved.candidate_symbol_keys
        )::TEXT
          FROM storage_v2_unresolved_call unresolved
          JOIN visible_occurrence caller_visible ON caller_visible.id = unresolved.caller_occurrence_id
         WHERE unresolved.source_id = p_source_id
         ORDER BY caller_visible.symbol_key, unresolved.callee_name, unresolved.call_kind;
    ELSIF p_collection = 'negative_evidence' THEN
        RETURN QUERY
        SELECT row_number() OVER (ORDER BY evidence.evidence_key),
               jsonb_build_object(
            'evidence_key', evidence.evidence_key, 'concept', evidence.concept,
            'path_description', evidence.path_description, 'reason', evidence.reason,
            'symbol_keys', evidence.symbol_keys, 'severity', evidence.severity,
            'created_by', evidence.created_by
        )::TEXT
          FROM storage_v2_negative_evidence evidence
          WHERE evidence.source_id = p_source_id
         ORDER BY evidence.evidence_key;
    END IF;
END
$function$;
ALTER FUNCTION storage_v2_intelligence_export_records(BIGINT,TEXT,TEXT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_intelligence_export_records(BIGINT,TEXT,TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_intelligence_export_records(BIGINT,TEXT,TEXT) TO mainrag;
COMMENT ON FUNCTION storage_v2_intelligence_export_records(BIGINT,TEXT,TEXT) IS
 'Complete ordered protected v1 intelligence records; one SQL snapshot can stream every collection and incrementally hash the canonical payload.';
COMMIT;
