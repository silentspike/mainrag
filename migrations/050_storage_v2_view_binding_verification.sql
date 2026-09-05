-- Migration 050: verify component bindings, not distinct identity cardinalities.
-- Multiple views may share a search document; composed views may bind several.
-- Keep both public counts and check every visible component's actual identity.

CREATE OR REPLACE FUNCTION storage_v2_shadow_source_state(
    p_source_id BIGINT,
    p_generation_selector TEXT,
    p_include_test BOOLEAN DEFAULT FALSE
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
    v_active_generation_id BIGINT;
    v_result JSONB;
BEGIN
    PERFORM storage_v2_require_test_scope(p_source_id, p_include_test);
    v_generation := storage_v2_resolve_generation(p_source_id, p_generation_selector);
    SELECT active_generation_id INTO v_active_generation_id
      FROM logical_source WHERE id = p_source_id;
    WITH visible_membership AS (
        SELECT membership.source_item_id, membership.artifact_version_id
          FROM generation_item_version membership
         WHERE membership.source_id = p_source_id
           AND membership.valid_from_seq <= v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq > v_generation.generation_seq)
    ), visible_run_item AS (
        SELECT item.source_item_id, item.artifact_version_id,
               item.occurrence_id, item.content_identity_sha256,
               item.analysis_profile_id
          FROM storage_v2_ingest_run run
          JOIN storage_v2_ingest_run_item item ON item.run_id = run.id
         WHERE run.source_id = p_source_id
           AND run.generation_id = v_generation.id
    ), visible_occurrence AS (
        SELECT occurrence_row.id, occurrence_row.view_id
          FROM visible_membership membership
          JOIN visible_run_item item
            ON item.source_item_id = membership.source_item_id
           AND item.artifact_version_id = membership.artifact_version_id
          JOIN occurrence occurrence_row ON occurrence_row.id = item.occurrence_id
         WHERE occurrence_row.source_id = p_source_id
    ), visible_view AS MATERIALIZED (
        SELECT DISTINCT view_id FROM visible_occurrence
    ), visible_component AS MATERIALIZED (
        SELECT component.* FROM visible_view view_row
          JOIN view_component component ON component.view_id = view_row.view_id
    ), visible_binding AS MATERIALIZED (
        SELECT binding.* FROM visible_view view_row
          JOIN storage_v2_search_view_document binding ON binding.view_id = view_row.view_id
    ), search_binding_error AS (
        -- Probe real indexed relations, not another materialized visible CTE.
        -- Source/generation correlation can underestimate visible rows; repeated
        -- CTE scans would then make this completeness check quadratic.
        SELECT component.view_id, component.ordinal
          FROM visible_component component
          LEFT JOIN LATERAL (
              SELECT candidate.document_id FROM storage_v2_search_view_document candidate
               WHERE candidate.view_id = component.view_id AND candidate.ordinal = component.ordinal
               OFFSET 0
          ) binding ON TRUE
          LEFT JOIN LATERAL (
              SELECT candidate.* FROM storage_v2_search_document candidate
               WHERE candidate.id = binding.document_id
               OFFSET 0
          ) document ON TRUE
         WHERE document.id IS NULL
            OR document.component_kind IS DISTINCT FROM component.component_kind
            OR document.body_id IS DISTINCT FROM component.body_id
            OR document.node_id IS DISTINCT FROM component.node_id
        UNION ALL
        SELECT binding.view_id, binding.ordinal
          FROM visible_binding binding
          LEFT JOIN LATERAL (
              SELECT candidate.view_id FROM view_component candidate
               WHERE candidate.view_id = binding.view_id AND candidate.ordinal = binding.ordinal
               OFFSET 0
          ) component ON TRUE
         WHERE component.view_id IS NULL
    )
    SELECT jsonb_build_object(
        'source_id', p_source_id,
        'generation_id', v_generation.id,
        'generation_seq', v_generation.generation_seq,
        'status', v_generation.status,
        'declared_item_count', v_generation.item_count,
        'is_active', v_active_generation_id IS NOT DISTINCT FROM v_generation.id,
        'active_generation_id', v_active_generation_id,
        'item_count', (SELECT COUNT(*) FROM visible_membership),
        'occurrence_count', (SELECT COUNT(*) FROM visible_occurrence),
        'view_count', (SELECT COUNT(*) FROM visible_view),
        'search_document_count', (SELECT COUNT(DISTINCT document_id) FROM visible_binding),
        'unbound_view_count', (
            SELECT COUNT(*) FROM visible_view view_row
             WHERE NOT EXISTS (
                       SELECT 1 FROM view_component component
                        WHERE component.view_id = view_row.view_id OFFSET 0
                   ) OR NOT EXISTS (
                       SELECT 1 FROM storage_v2_search_view_document binding
                        WHERE binding.view_id = view_row.view_id OFFSET 0
                   )
        ),
        'search_binding_error_count', (SELECT COUNT(*) FROM search_binding_error),
        'packed_body_count', (
            SELECT COUNT(DISTINCT body.id)
              FROM visible_membership membership
              JOIN artifact_version artifact ON artifact.id=membership.artifact_version_id
              JOIN content_node node ON node.id=artifact.content_root_node_id
              JOIN content_body body ON body.id=node.body_id
             WHERE body.pack_id IS NOT NULL
        ),
        'published_pack_count', (
            SELECT COUNT(DISTINCT pack.id)
              FROM visible_membership membership
              JOIN artifact_version artifact ON artifact.id=membership.artifact_version_id
              JOIN content_node node ON node.id=artifact.content_root_node_id
              JOIN content_body body ON body.id=node.body_id
              JOIN content_pack pack ON pack.id=body.pack_id
             WHERE pack.status='published'
        ),
        'symbol_count', (
            SELECT COUNT(*) FROM storage_v2_symbol_occurrence symbol_occurrence
             JOIN visible_occurrence occurrence_row
               ON occurrence_row.id = symbol_occurrence.occurrence_id
        ),
        'analysis_incomplete_count', (
            SELECT COUNT(*) FROM visible_membership membership
             WHERE NOT EXISTS (
                SELECT 1 FROM visible_run_item item
                  JOIN storage_v2_analysis_cache analysis
                    ON analysis.content_identity_sha256 = item.content_identity_sha256
                   AND analysis.analysis_profile_id = item.analysis_profile_id
                 WHERE item.source_item_id = membership.source_item_id
                   AND item.artifact_version_id = membership.artifact_version_id
                   AND analysis.status = 'complete'
             )
        ),
        'source_watermark_sha256', (
            SELECT semantic_manifest_sha256 FROM storage_v2_ingest_run
             WHERE generation_id=v_generation.id AND source_id=p_source_id
        ),
        'verification_manifest_sha256', v_generation.verification_manifest_sha256
    ) INTO v_result;
    RETURN v_result;
END
$$;
