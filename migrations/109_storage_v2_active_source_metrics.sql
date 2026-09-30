-- Active source inspection reads generation membership, never legacy caches.
-- Physical paths are counted once even when one file has several fragments.
CREATE OR REPLACE FUNCTION storage_v2_active_source_metrics(
    p_manifest_sha256 TEXT,
    p_source_id BIGINT,
    p_include_test BOOLEAN DEFAULT FALSE
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_generation source_generation;
    v_result JSONB;
BEGIN
    PERFORM storage_v2_require_complete_active_set(p_manifest_sha256);
    PERFORM storage_v2_require_test_scope(p_source_id, p_include_test);
    v_generation := storage_v2_resolve_generation(p_source_id, 'current');
    WITH visible_artifact AS MATERIALIZED (
        SELECT artifact.id, artifact.byte_length, artifact.witness,
               item.item_key
          FROM generation_item_version membership
          JOIN artifact_version artifact ON artifact.id=membership.artifact_version_id
           AND artifact.source_id=membership.source_id
           AND artifact.item_id=membership.source_item_id
          JOIN source_item item ON item.id=membership.source_item_id
           AND item.source_id=membership.source_id
         WHERE membership.source_id=p_source_id
           AND membership.valid_from_seq<=v_generation.generation_seq
           AND (membership.valid_to_seq IS NULL
                OR membership.valid_to_seq>v_generation.generation_seq)
    ), visible_occurrence AS MATERIALIZED (
        SELECT occurrence_row.id, occurrence_row.view_id
          FROM visible_artifact artifact
          JOIN occurrence occurrence_row ON occurrence_row.artifact_version_id=artifact.id
           AND occurrence_row.source_id=p_source_id
    ), visible_symbol AS MATERIALIZED (
        SELECT symbol_occurrence.id
          FROM visible_occurrence occurrence_row
          JOIN storage_v2_symbol_occurrence symbol_occurrence
            ON symbol_occurrence.occurrence_id=occurrence_row.id
           AND symbol_occurrence.source_id=p_source_id
    )
    SELECT jsonb_build_object(
        'read_path', 'storage_v2_active',
        'activation_manifest_sha256', p_manifest_sha256,
        'source_id', p_source_id,
        'generation_id', v_generation.id,
        'generation_seq', v_generation.generation_seq,
        'last_synced', v_generation.sealed_at,
        'file_count', (SELECT COUNT(DISTINCT COALESCE(witness->>'path',item_key))
                         FROM visible_artifact),
        'item_count', (SELECT COUNT(*) FROM visible_artifact),
        'total_size', (SELECT COALESCE(SUM(byte_length),0) FROM visible_artifact),
        'view_count', (SELECT COUNT(DISTINCT view_id) FROM visible_occurrence),
        'symbol_count', (SELECT COUNT(*) FROM visible_symbol),
        'call_count', (
            SELECT COUNT(*) FROM (
                SELECT edge.id FROM visible_symbol symbol_row
                  JOIN storage_v2_call_edge edge ON edge.caller_occurrence_id=symbol_row.id
                   AND edge.source_id=p_source_id
                UNION ALL
                SELECT unresolved.id FROM visible_symbol symbol_row
                  JOIN storage_v2_unresolved_call unresolved
                    ON unresolved.caller_occurrence_id=symbol_row.id
                   AND unresolved.source_id=p_source_id
            ) visible_call
        )
    ) INTO v_result;
    RETURN v_result;
END
$$;

-- Match the existing checked active-state reader owner. A fixture schema may
-- be admin-owned while production tables are API-owned; hard-coding the API
-- owner would make row_security=off fail against a different table owner.
DO $$
DECLARE v_owner NAME;
BEGIN
    SELECT pg_get_userbyid(proowner) INTO STRICT v_owner FROM pg_proc
     WHERE oid='storage_v2_active_source_state(text,bigint,boolean)'::regprocedure;
    EXECUTE format('ALTER FUNCTION storage_v2_active_source_metrics(TEXT,BIGINT,BOOLEAN) OWNER TO %I',v_owner);
END
$$;
REVOKE ALL ON FUNCTION storage_v2_active_source_metrics(TEXT,BIGINT,BOOLEAN) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_active_source_metrics(TEXT,BIGINT,BOOLEAN) TO mainrag;
