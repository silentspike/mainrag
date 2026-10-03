-- Expand matching payloads directly instead of probing every source occurrence.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='5min';
SET LOCAL maintenance_work_mem='128MB';
DO $$ BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
        RAISE EXCEPTION 'rank binding index installation requires the database administrator';
    END IF;
    IF encode(sha256(convert_to(pg_get_functiondef(
        'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::REGPROCEDURE),'UTF8')),'hex')
        <>'90f14f82ffcff081c4d6896a299f14307dd1f8504224e8df468fb4a63adedabb' THEN
        RAISE EXCEPTION 'unexpected preceding legacy rank reader';
    END IF;
END $$;
CREATE INDEX idx_storage_v2_legacy_rank_binding_payload
    ON storage_v2_legacy_rank_binding(payload_id,source_id,occurrence_id)
    INCLUDE (artifact_version_id,legacy_chunk_id);
ANALYZE storage_v2_legacy_rank_binding;
COMMIT;
