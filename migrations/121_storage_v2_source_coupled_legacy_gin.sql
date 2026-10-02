-- Apply source restrictions in the same GIN probe before positional rechecks.
-- Replace only the known owned index. No payload, generation or pointer changes.
BEGIN;
CREATE EXTENSION IF NOT EXISTS btree_gin WITH SCHEMA public;
DO $guard$
DECLARE definition TEXT; valid BOOLEAN;
BEGIN
 IF (SELECT extnamespace FROM pg_extension WHERE extname='btree_gin') <> 'public'::regnamespace THEN
  RAISE EXCEPTION 'copied lexical GIN extension namespace differs';
 END IF;
 IF EXISTS (SELECT 1 FROM pg_class WHERE oid IN (
   'public.storage_v2_legacy_lexical_segment'::regclass,
   'public.idx_storage_v2_legacy_lexical_segment_fts'::regclass)
   AND relowner <> 'mainrag_v2_frontier_owner'::regrole) THEN
  RAISE EXCEPTION 'owned copied lexical index authority differs';
 END IF;
 SELECT pg_get_indexdef(indexrelid),indisvalid AND indisready
   INTO definition,valid FROM pg_index
  WHERE indexrelid='public.idx_storage_v2_legacy_lexical_segment_fts'::REGCLASS;
 IF valid IS DISTINCT FROM TRUE OR definition IS NULL OR definition NOT IN (
  'CREATE INDEX idx_storage_v2_legacy_lexical_segment_fts ON public.storage_v2_legacy_lexical_segment USING gin (fts_vector)',
  'CREATE INDEX idx_storage_v2_legacy_lexical_segment_fts ON public.storage_v2_legacy_lexical_segment USING gin (source_id, fts_vector)'
 ) THEN RAISE EXCEPTION 'owned copied lexical index identity differs'; END IF;
 IF definition='CREATE INDEX idx_storage_v2_legacy_lexical_segment_fts ON public.storage_v2_legacy_lexical_segment USING gin (fts_vector)' THEN
  DROP INDEX public.idx_storage_v2_legacy_lexical_segment_fts;
  CREATE INDEX idx_storage_v2_legacy_lexical_segment_fts
    ON public.storage_v2_legacy_lexical_segment USING GIN(source_id,fts_vector);
 END IF;
END $guard$;
COMMIT;
