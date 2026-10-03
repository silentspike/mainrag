-- Keep whole-scope token statistics on narrow fixed-width index entries.
-- No precomputed score or changed corpus population is introduced.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='5min';
SET LOCAL maintenance_work_mem='128MB';
DO $$ BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
        RAISE EXCEPTION 'reader statistics index installation requires the database administrator';
    END IF;
    IF (SELECT relowner FROM pg_class WHERE oid='storage_v2_search_document'::REGCLASS)<>'mainrag'::REGROLE
       OR (SELECT relowner FROM pg_class WHERE oid='storage_v2_search_view_document'::REGCLASS)<>'mainrag'::REGROLE THEN
        RAISE EXCEPTION 'reader statistics index authority differs';
    END IF;
END $$;
CREATE INDEX idx_storage_v2_search_document_token_stats
    ON storage_v2_search_document(id) INCLUDE(token_count);
CREATE INDEX idx_storage_v2_search_view_document_stats
    ON storage_v2_search_view_document(view_id,ordinal) INCLUDE(document_id,role_weight);
ANALYZE storage_v2_search_document;
ANALYZE storage_v2_search_view_document;
COMMIT;
