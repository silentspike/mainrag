-- Convert only named, exactly equivalent retained document caches.
-- This changes representation, never source/generation/materialization identity.
-- Datum-byte differences are not physical savings. No relation rewrite runs here.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';
DO $predecessor$
DECLARE pinned RECORD;
BEGIN
 IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user) THEN
  RAISE EXCEPTION 'document-cache migration requires the local schema operator' USING ERRCODE='42501';
 END IF;
 FOR pinned IN SELECT * FROM (VALUES
  ('storage_v2_reject_document_mutation()','32033d1fa9792a88a1295d677366e56f4f4398dc5a07318785870bd48f361ef0'),
  ('storage_v2_safe_tsvector(text)','7771c09323f941c958ad01486cc02a8d62a8579d214c461ace40228302385308'),
  ('storage_v2_logical_document_fts(tsvector,boolean,text)','c299e4ce09ac4f925e7f5c3975766c4900465006e1b95084b0060cbb4a35bf10'),
  ('storage_v2_lexical_block_fingerprints(tsvector[])','8d665e5456ff55e489cb2a86ce17728d5ce7e821a722a1de9d878bece5924a1f'),
  ('storage_v2_plain_document_fingerprints(text)','7edec7bd864842feb3e7d3f23c419e230fec9c2b370088a0c969716520663f19')
 ) expected(signature,body_sha256) LOOP
  IF encode(sha256(convert_to((SELECT prosrc FROM pg_proc
        WHERE oid=pinned.signature::REGPROCEDURE),'UTF8')),'hex')
        IS DISTINCT FROM pinned.body_sha256 THEN
   RAISE EXCEPTION 'document-cache predecessor body differs: %',pinned.signature;
  END IF;
 END LOOP;
 IF NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
    WHERE p.oid='storage_v2_reject_document_mutation()'::REGPROCEDURE AND r.rolsuper
      AND p.prosecdef AND p.proconfig=ARRAY['search_path=pg_catalog, public','row_security=off']::TEXT[])
    OR NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_search_document'::REGCLASS
    AND tgname='storage_v2_search_document_immutable' AND tgenabled='O' AND tgtype=27
    AND tgfoid='storage_v2_reject_document_mutation()'::REGPROCEDURE)
    OR (SELECT attgenerated FROM pg_attribute WHERE attrelid='storage_v2_search_document'::REGCLASS
          AND attname='fts_simple')<>'' THEN
  RAISE EXCEPTION 'document-cache immutable/ordinary-column boundary differs';
 END IF;
 -- Existing posting consumers/guards/ACLs must still match their installed contract.
 PERFORM storage_v2_posting_conversion_require_operator();
END $predecessor$;

-- Keep the indexed cache predicate available for position-free plain queries.
-- A positional-free cache cannot decide complete phrase/negation semantics.
-- Complex derived predicates therefore use the existing full-vector branch.
DO $rank_readers$
DECLARE signature TEXT; definition TEXT; alias_name TEXT; marker TEXT;
        changed BOOLEAN; fallback TEXT;
BEGIN
 FOREACH signature IN ARRAY ARRAY[
  'storage_v2_source_segment_ranks(bigint[],text)',
  'storage_v2_source_segment_ranks_precise(bigint[],text)',
  'storage_v2_source_segment_rank_candidates(bigint[],text)',
  'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'
 ] LOOP
  definition:=pg_get_functiondef(signature::REGPROCEDURE); changed:=FALSE;
  IF (SELECT proowner FROM pg_proc WHERE oid=signature::REGPROCEDURE)
       <>'mainrag_v2_frontier_owner'::REGROLE
     OR (SELECT prosecdef FROM pg_proc WHERE oid=signature::REGPROCEDURE) IS DISTINCT FROM TRUE THEN
   RAISE EXCEPTION 'document-cache rank authority differs: %',signature;
  END IF;
  FOREACH alias_name IN ARRAY ARRAY['document','candidate'] LOOP
   marker:=alias_name||'.fts_simple@@v_query';
   IF strpos(definition,marker)>0 THEN
    fallback:=alias_name||'.fts_simple_derived AND ('||alias_name||'.fts_simple IS NULL OR public.storage_v2_plain_document_fingerprints(p_query) IS NULL) AND CASE WHEN';
    IF strpos(definition,fallback)=0 OR strpos(definition,'public.storage_v2_safe_tsvector('||alias_name||'.search_text)@@v_query')=0 THEN
     RAISE EXCEPTION 'document-cache exact rank fallback differs: %',signature;
    END IF;
    definition:=replace(definition,marker,
     '(('||alias_name||'.fts_simple_derived IS FALSE OR public.storage_v2_plain_document_fingerprints(p_query) IS NOT NULL) AND '||marker||')');
    changed:=TRUE;
   END IF;
  END LOOP;
  IF NOT changed THEN RAISE EXCEPTION 'document-cache rank predicate absent: %',signature; END IF;
  EXECUTE definition;
 END LOOP;
 -- Supported phrase readers must reconstruct full positional vectors.
 FOREACH signature IN ARRAY ARRAY[
  'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
  'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
 ] LOOP
  definition:=pg_get_functiondef(signature::REGPROCEDURE);
  IF strpos(definition,'storage_v2_logical_document_fts(document.fts_simple,document.fts_simple_derived,document.search_text)')=0 THEN
   RAISE EXCEPTION 'document-cache phrase reader lacks original logical vector: %',signature;
  END IF;
 END LOOP;
END $rank_readers$;

-- Historical audit identities deliberately have no live-document FK. Existing
-- generation/occurrence/view/export roots protect retained documents. Adding an
-- unknown incoming FK would instead make every converted document a permanent
-- native-GC root, even after its actual owners ceased retaining it.
-- The converter locks and validates the live document before writing/replaying
-- this immutable ledger; after legitimate GC, a missing target fails admission.
CREATE TABLE storage_v2_document_cache_conversion_receipt (
 document_id BIGINT PRIMARY KEY CHECK(document_id>0),
 operation_id UUID NOT NULL,
 manifest_sha256 BYTEA NOT NULL CHECK(octet_length(manifest_sha256)=32),
 materialization_sha256 BYTEA NOT NULL CHECK(octet_length(materialization_sha256)=32),
 original_vector_sha256 BYTEA NOT NULL CHECK(octet_length(original_vector_sha256)=32),
 stripped_vector_sha256 BYTEA NOT NULL CHECK(octet_length(stripped_vector_sha256)=32),
 text_bytes BIGINT NOT NULL CHECK(text_bytes>=0),
 original_datum_bytes BIGINT NOT NULL CHECK(original_datum_bytes>=0),
 stripped_datum_bytes BIGINT NOT NULL CHECK(stripped_datum_bytes>=0)
);
COMMENT ON TABLE storage_v2_document_cache_conversion_receipt IS
 'Immutable conversion audit; no retention-root FK. Current/retained content is protected by existing generation/occurrence/view/export roots. Replay requires a live exact document; orphan audit records remain retained after legitimate native GC.';
REVOKE ALL ON storage_v2_document_cache_conversion_receipt FROM PUBLIC,mainrag;
CREATE TRIGGER storage_v2_document_cache_receipt_immutable BEFORE UPDATE OR DELETE
 ON storage_v2_document_cache_conversion_receipt FOR EACH ROW
 EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();

-- This extra transition is restricted to a local superuser session operating
-- outside SET ROLE, with the existing administrator identity and exact cache
-- equality. Application grants do not authorize it. Identifier-only changes
-- retain their existing SQL138 guard, including complete sealed-set equality.
DO $transition$
DECLARE definition TEXT; marker TEXT:='BEGIN';
BEGIN
 definition:=pg_get_functiondef('storage_v2_reject_document_mutation()'::REGPROCEDURE);
 IF (length(definition)-length(replace(definition,E'BEGIN\n','')))/length(E'BEGIN\n')<>1 THEN
  RAISE EXCEPTION 'document-cache trigger transition boundary differs';
 END IF;
 EXECUTE replace(definition,E'BEGIN\n',$allow$BEGIN
    IF TG_OP='UPDATE' AND OLD.fts_simple_derived IS FALSE AND NEW.fts_simple_derived IS TRUE
       AND OLD.fts_simple IS NOT NULL
       AND current_setting('role') IN ('none',session_user)
       AND (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
       AND storage_v2_is_admin() IS TRUE
       AND current_setting('mainrag.document_cache_document',TRUE)=OLD.id::TEXT
       AND (to_jsonb(NEW)-ARRAY['fts_simple','fts_simple_derived','fts_simple_fingerprints'])
         =(to_jsonb(OLD)-ARRAY['fts_simple','fts_simple_derived','fts_simple_fingerprints'])
       AND OLD.fts_simple=storage_v2_safe_tsvector(OLD.search_text)
       AND NEW.fts_simple=strip(OLD.fts_simple)
       AND NEW.fts_simple_fingerprints IS NOT DISTINCT FROM
            storage_v2_lexical_block_fingerprints(ARRAY[OLD.fts_simple]) THEN
        RETURN NEW;
    END IF;
$allow$);
END $transition$;

CREATE FUNCTION storage_v2_convert_document_caches(
 p_operation UUID,p_manifest BYTEA,p_documents BIGINT[],p_materializations BYTEA[],
 p_max_body_bytes BIGINT,p_statement_budget_ms INTEGER
) RETURNS TABLE(document_id BIGINT,disposition TEXT,text_bytes BIGINT,
                original_datum_bytes BIGINT,stripped_datum_bytes BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public
SET row_security=off AS $convert$
DECLARE item RECORD; old_document storage_v2_search_document; old_vector TSVECTOR;
        receipt storage_v2_document_cache_conversion_receipt;
        total_bytes BIGINT:=0; count_documents INTEGER:=0;
        statement_ms NUMERIC; lock_ms NUMERIC; started TIMESTAMPTZ:=clock_timestamp();
BEGIN
 IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user)
    OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'document-cache conversion requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 IF p_operation IS NULL OR octet_length(p_manifest) IS DISTINCT FROM 32
    OR p_documents IS NULL OR cardinality(p_documents) NOT BETWEEN 1 AND 16
    OR array_ndims(p_documents) IS DISTINCT FROM 1 OR array_lower(p_documents,1) IS DISTINCT FROM 1
    OR array_position(p_documents,NULL) IS NOT NULL OR 0>=ANY(p_documents)
    OR cardinality(p_documents)<>(SELECT count(DISTINCT id) FROM unnest(p_documents) d(id))
    OR p_materializations IS NULL OR array_ndims(p_materializations) IS DISTINCT FROM 1
    OR array_lower(p_materializations,1) IS DISTINCT FROM 1
    OR cardinality(p_materializations)<>cardinality(p_documents)
    OR EXISTS(SELECT 1 FROM unnest(p_materializations) m(value) WHERE octet_length(value) IS DISTINCT FROM 32)
    OR p_max_body_bytes IS NULL OR p_max_body_bytes NOT BETWEEN 1 AND 8388608
    OR p_statement_budget_ms IS NULL OR p_statement_budget_ms NOT BETWEEN 100 AND 30000 THEN
  RAISE EXCEPTION 'exact named documents, identities and bounded cache budgets required';
 END IF;
 -- PostgreSQL arms statement_timeout before entering the function; setting it
 -- inside this function would not bound the statement already in progress.
 statement_ms:=extract(epoch FROM current_setting('statement_timeout')::INTERVAL)*1000;
 lock_ms:=extract(epoch FROM current_setting('lock_timeout')::INTERVAL)*1000;
 IF statement_ms<=0 OR statement_ms>p_statement_budget_ms OR lock_ms<=0 OR lock_ms>3000 THEN
  RAISE EXCEPTION 'prearmed statement/lock deadlines required';
 END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('storage-v2-posting-conversion-v1',0)) THEN
  RAISE EXCEPTION 'another storage maintenance operator owns the writer boundary';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 -- Freeze admission plus all document writes for this short transaction.
 -- No source rebuild, active-pointer switch or full-body census is performed.
 LOCK TABLE storage_v2_ingest_run,logical_source IN SHARE MODE NOWAIT;
 LOCK TABLE storage_v2_search_document IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 IF EXISTS(SELECT 1 FROM storage_v2_ingest_run WHERE status='building')
    OR EXISTS(SELECT 1 FROM pg_locks l JOIN logical_source s ON
       l.classid::BIGINT=((hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)>>32)&4294967295)
       AND l.objid::BIGINT=(hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)&4294967295)
       WHERE l.locktype='advisory' AND l.granted AND l.objsubid=1 AND l.pid<>pg_backend_pid())
    OR EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
       AND state<>'idle' AND query ~* '(insert|update|delete|merge|copy|create[[:space:]]+index|reindex|vacuum|cluster|refresh[[:space:]]+materialized|storage_v2_materialize_reader_metadata)')
    OR EXISTS(SELECT 1 FROM pg_stat_progress_create_index WHERE datid=(SELECT oid FROM pg_database WHERE datname=current_database()) AND pid<>pg_backend_pid()) THEN
  RAISE EXCEPTION 'document-cache conversion requires writer-free controlled maintenance';
 END IF;
 -- octet_length obtains byte admission without parsing each text or FTS value.
 FOR item IN SELECT d.id,d.materialization_sha256,octet_length(d.search_text)::BIGINT AS bytes,
                   expected.identity
   FROM unnest(p_documents,p_materializations) expected(id,identity)
   JOIN storage_v2_search_document d ON d.id=expected.id ORDER BY d.id FOR UPDATE OF d LOOP
  IF item.materialization_sha256 IS DISTINCT FROM item.identity THEN
   RAISE EXCEPTION 'named document materialization identity changed';
  END IF;
  total_bytes:=total_bytes+item.bytes; count_documents:=count_documents+1;
 END LOOP;
 IF count_documents<>cardinality(p_documents) OR total_bytes>p_max_body_bytes THEN
  RAISE EXCEPTION 'named document absent or body-byte admission exceeded';
 END IF;
 FOR item IN SELECT id FROM unnest(p_documents) d(id) ORDER BY id LOOP
  IF extract(epoch FROM clock_timestamp()-started)*1000>=p_statement_budget_ms THEN
   RAISE EXCEPTION 'document-cache batch deadline exceeded' USING ERRCODE='57014';
  END IF;
  SELECT * INTO STRICT old_document FROM storage_v2_search_document d WHERE d.id=item.id;
  document_id:=item.id; text_bytes:=octet_length(old_document.search_text);
  original_datum_bytes:=pg_column_size(old_document.fts_simple); stripped_datum_bytes:=NULL;
  SELECT * INTO receipt FROM storage_v2_document_cache_conversion_receipt r WHERE r.document_id=item.id;
  IF FOUND THEN
   old_vector:=storage_v2_safe_tsvector(old_document.search_text);
   IF receipt.operation_id IS DISTINCT FROM p_operation OR receipt.manifest_sha256 IS DISTINCT FROM p_manifest
      OR receipt.materialization_sha256 IS DISTINCT FROM old_document.materialization_sha256
      OR old_document.fts_simple_derived IS DISTINCT FROM TRUE
      OR sha256(tsvectorsend(old_document.fts_simple)) IS DISTINCT FROM receipt.stripped_vector_sha256
      OR old_vector IS NULL OR sha256(tsvectorsend(old_vector)) IS DISTINCT FROM receipt.original_vector_sha256
      OR old_document.fts_simple IS DISTINCT FROM strip(old_vector)
      OR old_document.fts_simple_fingerprints IS DISTINCT FROM storage_v2_lexical_block_fingerprints(ARRAY[old_vector]) THEN
    RAISE EXCEPTION 'document-cache replay receipt or representation differs';
   END IF;
   disposition:='REPLAY'; original_datum_bytes:=receipt.original_datum_bytes;
   stripped_datum_bytes:=receipt.stripped_datum_bytes; RETURN NEXT; CONTINUE;
  END IF;
  IF old_document.fts_simple_derived OR old_document.fts_simple IS NULL THEN
   disposition:='RETAIN_UNSUPPORTED'; RETURN NEXT; CONTINUE;
  END IF;
  old_vector:=storage_v2_safe_tsvector(old_document.search_text);
  IF old_vector IS NULL OR old_document.fts_simple IS DISTINCT FROM old_vector THEN
   disposition:='RETAIN_VECTOR_MISMATCH'; RETURN NEXT; CONTINUE;
  END IF;
  PERFORM set_config('mainrag.document_cache_document',item.id::TEXT,TRUE);
  UPDATE storage_v2_search_document d
     SET fts_simple=strip(old_vector),fts_simple_derived=TRUE,
         fts_simple_fingerprints=storage_v2_lexical_block_fingerprints(ARRAY[old_vector])
     WHERE d.id=item.id;
  PERFORM set_config('mainrag.document_cache_document','',TRUE);
  SELECT pg_column_size(d.fts_simple) INTO stripped_datum_bytes
    FROM storage_v2_search_document d WHERE d.id=item.id;
  INSERT INTO storage_v2_document_cache_conversion_receipt VALUES(
    item.id,p_operation,p_manifest,old_document.materialization_sha256,
    sha256(tsvectorsend(old_vector)),sha256(tsvectorsend(strip(old_vector))),
    text_bytes,original_datum_bytes,stripped_datum_bytes);
  disposition:='CONVERTED'; RETURN NEXT;
 END LOOP;
END $convert$;
REVOKE ALL ON FUNCTION storage_v2_convert_document_caches(UUID,BYTEA,BIGINT[],BYTEA[],BIGINT,INTEGER)
 FROM PUBLIC,mainrag;
COMMENT ON FUNCTION storage_v2_convert_document_caches(UUID,BYTEA,BIGINT[],BYTEA[],BIGINT,INTEGER) IS
 'Named exact document-cache conversion; existing SQL138 identifier conversion is separate. Datum bytes exclude physical allocation. Runtime drain/package evidence and later physical compaction remain operator prerequisites.';
COMMIT;
