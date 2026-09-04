-- Migration 041: make structural-card persistence one atomic database call
--
-- The release-candidate writer previously made four round trips per parsed
-- symbol. This wrapper preserves the established validation and immutable
-- collision behavior while committing occurrence, analysis, and card state as
-- one statement.

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
BEGIN
    IF p_output_sha256 IS NULL OR octet_length(p_output_sha256) <> 32 THEN
        RAISE EXCEPTION 'valid structural-card output digest required';
    END IF;

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
