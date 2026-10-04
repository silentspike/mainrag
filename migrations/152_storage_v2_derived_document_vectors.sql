-- Retain existing full-text caches; derive new large-document vectors lazily.
-- Full-document fingerprints only prune candidates. Exact FTS remains decisive.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
DO $guard$
DECLARE expected RECORD;
BEGIN
 FOR expected IN SELECT * FROM (VALUES
  ('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)','38725587fcf12f29edae1cba366f992d0f2dbdb805e6c84105bf4cf79fb869fb','mainrag'),
  ('storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)','508540e37b76d8e4087d083b31180a2e0aa7d319ee33af0147cd5bab8c15f204','mainrag'),
  ('storage_v2_source_segment_ranks(bigint[],text)','f402b2b02482d1c6bf81041eac7fce0efba7a682e30600dca6838d7b7fbf201e','mainrag_v2_frontier_owner'),
  ('storage_v2_source_segment_ranks_precise(bigint[],text)','4accbbb78501ad1ddb446c7a8caa06ae71d84f890cc038019a43170fab15d5d6','mainrag_v2_frontier_owner'),
  ('storage_v2_source_segment_rank_candidates(bigint[],text)','222144217e845dc5c199c3d8e62d1dc32940cb3109abe5b50226232f50ef4a06','mainrag_v2_frontier_owner'),
  ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])','daa94241e895ce401a612936f751a8d8f98f0e03156c384ac1ba08f298ad46c2','mainrag_v2_frontier_owner'),
  ('storage_v2_reject_document_mutation()','44f26d98af35ef443fbbd44e8d5888600349e2fadd6117106651ed463f3ea096','@administrator')) x(signature,sha256,owner_name) LOOP
  IF encode(sha256(convert_to(pg_get_functiondef(expected.signature::REGPROCEDURE),'UTF8')),'hex')
       IS DISTINCT FROM expected.sha256
     OR (CASE WHEN expected.owner_name='@administrator'
             THEN NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
                              WHERE p.oid=expected.signature::REGPROCEDURE AND r.rolsuper)
             ELSE (SELECT proowner FROM pg_proc WHERE oid=expected.signature::REGPROCEDURE)
                   <>expected.owner_name::REGROLE END) THEN
   RAISE EXCEPTION 'derived document predecessor definition or authority differs: %',expected.signature;
  END IF;
 END LOOP;
 IF (SELECT attgenerated FROM pg_attribute WHERE attrelid='storage_v2_search_document'::REGCLASS
      AND attname='fts_simple')<>'s' THEN
  RAISE EXCEPTION 'derived document preceding vector representation differs';
 END IF;
END $guard$;

-- DROP EXPRESSION retains every existing value and its current GIN index.
-- These metadata additions do not backfill or rewrite retained documents.
ALTER TABLE storage_v2_search_document ALTER COLUMN fts_simple DROP EXPRESSION;
ALTER TABLE storage_v2_search_document
    ADD COLUMN fts_simple_derived BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN fts_simple_fingerprints INTEGER[];
CREATE INDEX idx_storage_v2_derived_document_fingerprint
    ON storage_v2_search_document USING GIN(fts_simple_fingerprints)
    WHERE fts_simple_derived;

CREATE FUNCTION storage_v2_logical_document_fts(p_stored TSVECTOR,p_derived BOOLEAN,p_text TEXT)
RETURNS TSVECTOR LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path=pg_catalog,public AS $$
 SELECT CASE WHEN p_derived THEN storage_v2_safe_tsvector(p_text) ELSE p_stored END
$$;
ALTER FUNCTION storage_v2_logical_document_fts(TSVECTOR,BOOLEAN,TEXT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_logical_document_fts(TSVECTOR,BOOLEAN,TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_logical_document_fts(TSVECTOR,BOOLEAN,TEXT)
    TO mainrag,mainrag_v2_frontier_owner;

CREATE FUNCTION storage_v2_plain_document_fingerprints(p_query TEXT)
RETURNS INTEGER[] LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public AS $$
 SELECT CASE WHEN p_query ~ '^[[:alnum:]_]+([[:space:]]+[[:alnum:]_]+)*$'
                   AND lower(p_query) !~ '(^|[[:space:]])or([[:space:]]|$)'
             THEN storage_v2_posting_fingerprints(tsvector_to_array(to_tsvector('simple',p_query)))
             ELSE NULL END
$$;
ALTER FUNCTION storage_v2_plain_document_fingerprints(TEXT) OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_plain_document_fingerprints(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_plain_document_fingerprints(TEXT)
    TO mainrag,mainrag_v2_frontier_owner;

CREATE FUNCTION storage_v2_prepare_document_vector()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE vector TSVECTOR;
BEGIN
 vector:=storage_v2_safe_tsvector(NEW.search_text);
 NEW.fts_simple_derived:=octet_length(NEW.search_text)>=262144;
 IF NEW.fts_simple_derived THEN
  NEW.fts_simple:=NULL;
  NEW.fts_simple_fingerprints:=storage_v2_lexical_block_fingerprints(ARRAY[vector]);
 ELSE
  NEW.fts_simple:=vector;
  NEW.fts_simple_fingerprints:=NULL;
 END IF;
 RETURN NEW;
END $$;
ALTER FUNCTION storage_v2_prepare_document_vector() OWNER TO mainrag;
REVOKE ALL ON FUNCTION storage_v2_prepare_document_vector() FROM PUBLIC;
CREATE TRIGGER storage_v2_search_document_vector BEFORE INSERT ON storage_v2_search_document
    FOR EACH ROW EXECUTE FUNCTION storage_v2_prepare_document_vector();

-- After DROP EXPRESSION, cached vectors are ordinary immutable values. An
-- identifier-only transition must preserve the cache, flag and fingerprints.
DO $immutable$
DECLARE definition TEXT; marker TEXT:='ARRAY[''exact_identifiers'',''exact_identifiers_derived'',''fts_simple'']';
BEGIN
 definition:=pg_get_functiondef('storage_v2_reject_document_mutation()'::REGPROCEDURE);
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>2 THEN
  RAISE EXCEPTION 'derived document mutation boundary differs';
 END IF;
 EXECUTE replace(definition,marker,'ARRAY[''exact_identifiers'',''exact_identifiers_derived'']');
END $immutable$;

DO $phrase_readers$
DECLARE signature TEXT; definition TEXT; marker TEXT:='document.fts_simple, document.search_text';
BEGIN
 FOREACH signature IN ARRAY ARRAY[
  'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
  'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
 ] LOOP
  definition:=pg_get_functiondef(signature::REGPROCEDURE);
  IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
   RAISE EXCEPTION 'derived document phrase projection boundary differs';
  END IF;
  EXECUTE replace(definition,marker,
   'storage_v2_logical_document_fts(document.fts_simple,document.fts_simple_derived,document.search_text) AS fts_simple, document.search_text');
 END LOOP;
END $phrase_readers$;

DO $rank_readers$
DECLARE signature TEXT; definition TEXT; alias_name TEXT; marker TEXT; replacement TEXT; changed BOOLEAN;
BEGIN
 FOREACH signature IN ARRAY ARRAY[
  'storage_v2_source_segment_ranks(bigint[],text)',
  'storage_v2_source_segment_ranks_precise(bigint[],text)',
  'storage_v2_source_segment_rank_candidates(bigint[],text)',
  'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'
 ] LOOP
  definition:=pg_get_functiondef(signature::REGPROCEDURE);
  changed:=FALSE;
  FOREACH alias_name IN ARRAY ARRAY['document','candidate'] LOOP
   marker:=alias_name||'.fts_simple@@v_query';
   IF strpos(definition,marker)>0 THEN
    replacement:='('||marker||' OR ('||alias_name||'.fts_simple_derived AND '
        ||'CASE WHEN public.storage_v2_plain_document_fingerprints(p_query) IS NULL THEN TRUE '
        ||'ELSE '||alias_name||'.fts_simple_fingerprints @> public.storage_v2_plain_document_fingerprints(p_query) END '
        ||'AND public.storage_v2_safe_tsvector('||alias_name||'.search_text)@@v_query))';
    definition:=replace(definition,marker,replacement);changed:=TRUE;
   END IF;
  END LOOP;
  IF NOT changed THEN RAISE EXCEPTION 'derived document rank predicate boundary differs'; END IF;
  EXECUTE definition;
 END LOOP;
END $rank_readers$;
COMMIT;
