-- Bind an explicitly selected filesystem cut to its original full build
-- witness. A qualification label cannot substitute for the snapshot manifest.
DO $migration$
DECLARE
    v_signature REGPROCEDURE := 'storage_v2_qualify_release_candidate(uuid,bigint,bigint,text,text,text,text,text,jsonb)'::REGPROCEDURE;
    v_definition TEXT := pg_get_functiondef(v_signature);
    v_anchor TEXT := '    SELECT active_generation_id INTO v_active_generation_id';
    v_guard TEXT := $guard$    IF p_adapter_profile_id LIKE 'mainrag.fs-release-candidate.v4.btrfs-cut-v1.%' THEN
        IF jsonb_typeof(v_generation.witness->'filesystem_cut') IS DISTINCT FROM 'object'
           OR p_manifest #> '{source_snapshot_review,build_filesystem_cut}'
                IS DISTINCT FROM v_generation.witness->'filesystem_cut'
           OR ((p_manifest #> '{source_snapshot_review,filesystem_cut}') - 'cut')
                IS DISTINCT FROM ((v_generation.witness->'filesystem_cut') - 'cut')
           OR p_manifest #>> '{source_snapshot_review,filesystem_cut,cut,source_root_sha256}'
                IS DISTINCT FROM v_generation.witness #>> '{filesystem_cut,cut,source_root_sha256}'
           OR p_manifest #>> '{source_snapshot_review,adapter_profile_id}'
                IS DISTINCT FROM p_adapter_profile_id
           OR p_manifest #>> '{source_snapshot_review,source_watermark_sha256}'
                IS DISTINCT FROM p_source_watermark_sha256
           OR p_manifest #>> '{source_snapshot_review,source_root_sha256}'
                IS DISTINCT FROM v_generation.witness #>> '{filesystem_cut,cut,source_root_sha256}'
           OR (v_generation.witness #>> '{filesystem_cut,item_count}')::BIGINT
                IS DISTINCT FROM v_generation.item_count
           OR v_generation.witness #>> '{filesystem_cut,fixture_sha256}'
                IS DISTINCT FROM v_generation.witness->>'fixture_sha256' THEN
            RAISE EXCEPTION 'filesystem cut qualification differs from the full build witness';
        END IF;
    ELSIF v_generation.witness ? 'filesystem_cut'
          OR p_manifest #> '{source_snapshot_review,filesystem_cut}' IS NOT NULL THEN
        RAISE EXCEPTION 'filesystem cut has no selected adapter profile';
    END IF;

$guard$;
BEGIN
    IF strpos(v_definition,v_guard)>0 THEN RETURN; END IF;
    IF (length(v_definition)-length(replace(v_definition,v_anchor,'')))/length(v_anchor)<>1 THEN
        RAISE EXCEPTION 'qualification definition differs before filesystem cut binding';
    END IF;
    EXECUTE replace(v_definition,v_anchor,v_guard||v_anchor);
END
$migration$;
