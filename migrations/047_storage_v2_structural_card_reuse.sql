-- Migration 047: reuse complete structural cards without speculative inserts
--
-- The candidate writer visits every symbol even when its artifact and analysis
-- are unchanged. A complete, source-authorized bundle can be returned through
-- indexed reads. Incomplete state and every mismatch retain the established
-- atomic writer, including its collision and concurrent-insert checks.

CREATE OR REPLACE FUNCTION storage_v2_put_structural_card_bundle(
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
    p_source_span JSONB,
    p_analysis_profile_id TEXT,
    p_output_sha256 BYTEA,
    p_generic_card JSONB,
    p_domain_fields JSONB,
    p_field_provenance JSONB
) RETURNS storage_v2_symbol_occurrence
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_symbol_occurrence storage_v2_symbol_occurrence;
    v_analysis storage_v2_intelligence_analysis;
    v_identity BYTEA;
    v_structural BYTEA;
    v_unknown_domain JSONB := '{"layer":"unknown","side_effect":"unknown","resource":"unknown","delegation_target":"unknown"}'::JSONB;
    v_normalized JSONB;
BEGIN
    IF p_output_sha256 IS NULL OR octet_length(p_output_sha256) <> 32 THEN
        RAISE EXCEPTION 'valid structural-card output digest required';
    END IF;

    IF storage_v2_can_access_source(p_source_id, 'write')
       AND p_source_id IS NOT NULL AND p_artifact_version_id IS NOT NULL
       AND p_occurrence_id IS NOT NULL
       AND p_symbol_key <> '' AND p_language <> '' AND p_symbol_kind <> ''
       AND p_qualified_name <> '' AND p_analysis_profile_id <> ''
       AND p_structure IS NOT NULL AND p_source_span IS NOT NULL
       AND jsonb_typeof(p_generic_card) = 'object'
       AND jsonb_typeof(p_domain_fields) = 'object'
       AND p_domain_fields <@ v_unknown_domain
       AND p_field_provenance = '{}'::JSONB THEN
        v_identity := storage_v2_hash_parts('mainrag.symbol.v1', ARRAY[
            int8send(p_source_id), convert_to(p_symbol_key, 'UTF8')
        ]);
        v_structural := storage_v2_hash_parts('mainrag.symbol-occurrence.v1', ARRAY[
            convert_to(COALESCE(p_signature, ''), 'UTF8'),
            convert_to(COALESCE(p_documentation, ''), 'UTF8'),
            convert_to(COALESCE(p_visibility, ''), 'UTF8'),
            convert_to(p_structure::TEXT, 'UTF8'), convert_to(p_source_span::TEXT, 'UTF8')
        ]);
        v_normalized := jsonb_build_object(
            'generic', p_generic_card, 'domain', v_unknown_domain,
            'provenance', p_field_provenance,
            'analysis_profile_id', p_analysis_profile_id,
            'domain_profile_id', NULL, 'domain_profile_version', NULL
        );

        SELECT symbol_occurrence.* INTO v_symbol_occurrence
          FROM (
              SELECT stored_symbol.* FROM storage_v2_symbol stored_symbol
               WHERE stored_symbol.source_id = p_source_id AND stored_symbol.symbol_key = p_symbol_key
              OFFSET 0
          ) symbol
          JOIN LATERAL (
              SELECT stored_occurrence.* FROM storage_v2_symbol_occurrence stored_occurrence
               WHERE stored_occurrence.symbol_id = symbol.id
                 AND stored_occurrence.artifact_version_id = p_artifact_version_id
                 AND stored_occurrence.structural_sha256 = v_structural
              -- Keep source filtering outside this complete immutable key.
              -- OFFSET 0 is a pull-up barrier, not a result or version cap.
              OFFSET 0
          ) symbol_occurrence ON TRUE
          JOIN artifact_version artifact ON artifact.id = symbol_occurrence.artifact_version_id
          JOIN occurrence source_occurrence ON source_occurrence.id = p_occurrence_id
          JOIN LATERAL (
              SELECT stored_analysis.* FROM storage_v2_intelligence_analysis stored_analysis
               WHERE stored_analysis.symbol_occurrence_id = symbol_occurrence.id
                 AND stored_analysis.analysis_profile_id = p_analysis_profile_id
              OFFSET 0
          ) analysis ON TRUE
          JOIN LATERAL (
              SELECT stored_card.* FROM storage_v2_symbol_card stored_card
               WHERE stored_card.symbol_occurrence_id = symbol_occurrence.id
                 AND stored_card.analysis_profile_id = p_analysis_profile_id
              OFFSET 0
          ) card ON TRUE
         WHERE symbol.source_id = p_source_id AND symbol.symbol_key = p_symbol_key
           AND symbol.identity_sha256 = v_identity
           AND (symbol.language, symbol.symbol_kind, symbol.qualified_name)
               = (p_language, p_symbol_kind, p_qualified_name)
           AND symbol_occurrence.source_id = p_source_id
           AND symbol_occurrence.occurrence_id = p_occurrence_id
           AND (symbol_occurrence.signature, symbol_occurrence.documentation,
                symbol_occurrence.visibility, symbol_occurrence.structure, symbol_occurrence.source_span)
               IS NOT DISTINCT FROM (p_signature, p_documentation, p_visibility, p_structure, p_source_span)
           AND artifact.source_id = p_source_id
           AND source_occurrence.source_id = p_source_id
           AND source_occurrence.artifact_version_id = p_artifact_version_id
           AND analysis.status = 'complete' AND analysis.output_sha256 = p_output_sha256
           AND card.domain_profile_id IS NULL AND card.domain_profile_version IS NULL
           AND card.generic_card = p_generic_card AND card.domain_fields = v_unknown_domain
           AND card.field_provenance = p_field_provenance
           AND card.normalized_output = v_normalized
           AND card.output_sha256 = digest(convert_to(v_normalized::TEXT, 'UTF8'), 'sha256');
        IF FOUND THEN RETURN v_symbol_occurrence; END IF;
    END IF;

    -- Preserve the original writer for cold, incomplete, conflicting, and
    -- concurrent states. A non-visible concurrent winner reaches its existing
    -- unique-conflict readback, never an unverified shortcut.
    SELECT * INTO STRICT v_symbol_occurrence
      FROM storage_v2_put_symbol_occurrence(
          p_source_id, p_artifact_version_id, p_occurrence_id, p_symbol_key,
          p_language, p_symbol_kind, p_qualified_name, p_signature,
          p_documentation, p_visibility, p_structure, p_source_span
      );
    SELECT * INTO STRICT v_analysis
      FROM storage_v2_begin_intelligence_analysis(
          v_symbol_occurrence.id, p_analysis_profile_id
      );
    IF v_analysis.status = 'pending' THEN
        PERFORM storage_v2_finish_intelligence_analysis(
            v_symbol_occurrence.id, p_analysis_profile_id, p_output_sha256, NULL
        );
    ELSIF v_analysis.status <> 'complete'
       OR v_analysis.output_sha256 IS DISTINCT FROM p_output_sha256 THEN
        RAISE EXCEPTION 'complete intelligence analysis output differs from the structural card'
            USING ERRCODE = '22000';
    END IF;
    PERFORM storage_v2_put_symbol_card(
        v_symbol_occurrence.id, p_analysis_profile_id, p_generic_card,
        p_domain_fields, p_field_provenance, NULL, NULL
    );
    RETURN v_symbol_occurrence;
END
$$;

REVOKE EXECUTE ON FUNCTION storage_v2_put_structural_card_bundle(
    BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT,
    JSONB, JSONB, TEXT, BYTEA, JSONB, JSONB, JSONB
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_put_structural_card_bundle(
    BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT,
    JSONB, JSONB, TEXT, BYTEA, JSONB, JSONB, JSONB
) TO mainrag;
