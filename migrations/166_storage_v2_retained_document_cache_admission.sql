-- Named SQL161 cache maintenance and SQL138 identifier derivation with an
-- exactly frozen inert run. Retained roots, search text and readers stay intact.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';
DO $predecessor$
DECLARE pinned RECORD;
BEGIN
 PERFORM storage_v2_require_lexical_conversion_operator();
 FOR pinned IN SELECT * FROM (VALUES
  ('storage_v2_convert_document_caches(uuid,bytea,bigint[],bytea[],bigint,integer)','d7f9913be448b95b9479d8c652e60a1fdb3f5091a0257b9290ddacf8b2079fa5'),
  ('storage_v2_reject_document_mutation()','13a936c176da7dc2b619053a791ceead5f3bd2d38147a9bbbde783d88c31a369'),
  ('storage_v2_document_word_identifiers(bigint)','012f41ffd96c0389aa795ad8670bb9362e2dc81174886fe21898febff2dd47ba'),
  ('storage_v2_retained_posting_run_sha256(bigint)','9e14edd85f43d0e71598d3e39a8af1cc35cd1d8a430dd0c8350dffad3caa04d1'),
  ('storage_v2_retained_posting_document_identity_sha256(bigint)','2bffd2432dbea77a54fed5959166fb7cbcb5c617252991c5812a00df6dd161b8')
 ) expected(signature,definition_sha256) LOOP
  IF encode(sha256(convert_to(pg_get_functiondef(pinned.signature::REGPROCEDURE),'UTF8')),'hex')
      IS DISTINCT FROM pinned.definition_sha256 THEN
   RAISE EXCEPTION 'retained cache predecessor differs: %',pinned.signature;
  END IF;
 END LOOP;
END $predecessor$;

-- No foreign key: a historic audit receipt is not a native GC retention root.
-- Only beneficial conversions create a receipt. Unmodified skips are observed
-- in the operator's private journal, never charged an additional database row.
CREATE TABLE storage_v2_retained_document_cache_receipt (
 document_id BIGINT PRIMARY KEY CHECK(document_id>0),
 operation_id UUID NOT NULL,
 manifest_sha256 BYTEA NOT NULL CHECK(octet_length(manifest_sha256)=32),
 materialization_sha256 BYTEA NOT NULL CHECK(octet_length(materialization_sha256)=32),
 retained_run BIGINT,
 retained_run_sha256 BYTEA,
 document_identity_sha256 BYTEA NOT NULL CHECK(octet_length(document_identity_sha256)=32),
 original_cache_sha256 BYTEA NOT NULL CHECK(octet_length(original_cache_sha256)=32),
 cache_payload_sha256 BYTEA NOT NULL CHECK(octet_length(cache_payload_sha256)=32),
 original_stored_cache_bytes BIGINT NOT NULL CHECK(original_stored_cache_bytes>=0),
 stored_cache_bytes BIGINT NOT NULL CHECK(stored_cache_bytes>=0),
 audit_bytes BIGINT NOT NULL CHECK(audit_bytes>0),
 fingerprint_index_bytes BIGINT NOT NULL CHECK(fingerprint_index_bytes>=0),
 fts_changed BOOLEAN NOT NULL,
 identifiers_changed BOOLEAN NOT NULL,
 original_seal_present BOOLEAN NOT NULL,
 sql161_receipt_sha256 BYTEA,
 text_bytes BIGINT NOT NULL CHECK(text_bytes BETWEEN 0 AND 8388608),
 CHECK((retained_run IS NULL)=(retained_run_sha256 IS NULL)),
 CHECK(retained_run IS NULL OR (retained_run>0 AND octet_length(retained_run_sha256)=32)),
 CHECK(sql161_receipt_sha256 IS NULL OR octet_length(sql161_receipt_sha256)=32),
 CHECK(fts_changed OR identifiers_changed),
 CHECK(stored_cache_bytes+audit_bytes+fingerprint_index_bytes<original_stored_cache_bytes)
);
REVOKE ALL ON storage_v2_retained_document_cache_receipt FROM PUBLIC,mainrag;
CREATE TRIGGER storage_v2_retained_document_cache_receipt_immutable BEFORE UPDATE OR DELETE
 ON storage_v2_retained_document_cache_receipt FOR EACH ROW
 EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();

CREATE FUNCTION storage_v2_retained_document_cache_payload_sha256(p_document BIGINT)
RETURNS BYTEA LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE result BYTEA; stored_bytes BIGINT;
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user)
    OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'retained cache hash requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 SELECT coalesce(pg_column_size(d.fts_simple),0)::BIGINT+coalesce(pg_column_size(d.fts_simple_fingerprints),0)
    +pg_column_size(d.exact_identifiers) INTO stored_bytes FROM storage_v2_search_document d WHERE d.id=p_document;
 IF stored_bytes>8388608 THEN
  RAISE EXCEPTION 'retained stored cache admission exceeded' USING ERRCODE='54000';
 END IF;
 SELECT sha256(convert_to(jsonb_build_object('fts_simple',d.fts_simple::TEXT,
   'fts_simple_derived',d.fts_simple_derived,'fts_simple_fingerprints',d.fts_simple_fingerprints,
   'exact_identifiers',d.exact_identifiers,'exact_identifiers_derived',d.exact_identifiers_derived)::TEXT,'UTF8'))
  INTO result FROM storage_v2_search_document d WHERE d.id=p_document;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION storage_v2_retained_document_cache_payload_sha256(BIGINT) FROM PUBLIC,mainrag;

-- Derived from the pinned SQL161 admission, document identity, budgets and
-- original receipt transition. The original six-argument function is unchanged.
CREATE FUNCTION storage_v2_convert_retained_document_caches(
 p_operation UUID,p_manifest BYTEA,p_documents BIGINT[],p_materializations BYTEA[],
 p_max_body_bytes BIGINT,p_statement_budget_ms INTEGER,
 p_retained_run BIGINT DEFAULT NULL,p_retained_run_sha256 BYTEA DEFAULT NULL
) RETURNS TABLE(document_id BIGINT,disposition TEXT,original_cache_sha256 BYTEA,
 cache_payload_sha256 BYTEA,document_identity_sha256 BYTEA,original_stored_cache_bytes BIGINT,
 stored_cache_bytes BIGINT,audit_bytes BIGINT,text_bytes BIGINT,fingerprint_index_bytes BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $convert$
DECLARE item RECORD; old_document storage_v2_search_document;
 receipt storage_v2_retained_document_cache_receipt; proposed storage_v2_retained_document_cache_receipt;
 old_receipt storage_v2_document_cache_conversion_receipt;
 vector TSVECTOR; identifiers TEXT[]; original_vector_bytes BIGINT; stripped_vector_bytes BIGINT;
 total_bytes BIGINT:=0; count_documents INTEGER:=0; statement_ms NUMERIC; lock_ms NUMERIC;
 fts_change BOOLEAN; identifier_change BOOLEAN; seal_present BOOLEAN; legacy_receipt_sha BYTEA;
 attempted_fts BOOLEAN; budget_refusal BOOLEAN; audit_rows INTEGER; extra_audit BIGINT;
 started TIMESTAMPTZ:=clock_timestamp();
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user)
    OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'retained cache conversion requires the local administrative operator' USING ERRCODE='42501';
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
    OR p_statement_budget_ms IS NULL OR p_statement_budget_ms NOT BETWEEN 100 AND 30000
    OR (p_retained_run IS NULL) IS DISTINCT FROM (p_retained_run_sha256 IS NULL)
    OR (p_retained_run IS NOT NULL AND (p_retained_run<=0 OR octet_length(p_retained_run_sha256) IS DISTINCT FROM 32)) THEN
  RAISE EXCEPTION 'exact named documents, cache identities and bounded budgets required';
 END IF;
 statement_ms:=extract(epoch FROM current_setting('statement_timeout')::INTERVAL)*1000;
 lock_ms:=extract(epoch FROM current_setting('lock_timeout')::INTERVAL)*1000;
 IF statement_ms<=0 OR statement_ms>p_statement_budget_ms OR lock_ms<=0 OR lock_ms>1000 THEN
  RAISE EXCEPTION 'prearmed statement/lock deadlines required';
 END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('storage-v2-posting-conversion-v1',0)) THEN
  RAISE EXCEPTION 'another storage maintenance operator owns the writer boundary';
 END IF;
 PERFORM storage_v2_require_lexical_conversion_operator();
 LOCK TABLE storage_v2_ingest_run,source_generation,logical_source IN SHARE MODE NOWAIT;
 LOCK TABLE storage_v2_search_document IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 LOCK TABLE storage_v2_search_posting,storage_v2_compact_posting_block,storage_v2_byte_posting_block
  IN SHARE MODE NOWAIT;
 LOCK TABLE storage_v2_document_postings_seal IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 IF EXISTS(SELECT 1 FROM storage_v2_ingest_run r WHERE r.status='building'
       AND (p_retained_run IS NULL OR r.id<>p_retained_run))
    OR (p_retained_run IS NOT NULL AND (NOT EXISTS(SELECT 1 FROM storage_v2_ingest_run r
       JOIN source_generation g ON g.id=r.generation_id WHERE r.id=p_retained_run
       AND r.status='building' AND g.status='building')
       OR storage_v2_retained_posting_run_sha256(p_retained_run) IS DISTINCT FROM p_retained_run_sha256))
    OR EXISTS(SELECT 1 FROM pg_locks l JOIN logical_source s ON
       l.classid::BIGINT=((hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)>>32)&4294967295)
       AND l.objid::BIGINT=(hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)&4294967295)
       WHERE l.locktype='advisory' AND l.granted AND l.objsubid=1 AND l.pid<>pg_backend_pid())
    OR EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
       AND state<>'idle' AND query ~* '(insert|update|delete|merge|copy|create[[:space:]]+index|reindex|vacuum|cluster|refresh[[:space:]]+materialized|storage_v2_materialize_reader_metadata)')
    OR EXISTS(SELECT 1 FROM pg_stat_progress_create_index WHERE datid=(SELECT oid FROM pg_database WHERE datname=current_database())
       AND pid<>pg_backend_pid())
    OR EXISTS(SELECT 1 FROM storage_v2_retained_posting_conversion_intent)
    OR EXISTS(SELECT 1 FROM storage_v2_posting_conversion_intent)
    OR EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_intent) THEN
  RAISE EXCEPTION 'retained cache conversion requires exact quiescent frozen-run maintenance';
 END IF;
 FOR item IN SELECT d.id,d.materialization_sha256,octet_length(d.search_text)::BIGINT AS bytes,
       expected.identity FROM unnest(p_documents,p_materializations) expected(id,identity)
       JOIN storage_v2_search_document d ON d.id=expected.id ORDER BY d.id FOR UPDATE OF d NOWAIT LOOP
  IF item.materialization_sha256 IS DISTINCT FROM item.identity THEN
   RAISE EXCEPTION 'named document materialization identity changed';
  END IF;
  total_bytes:=total_bytes+item.bytes;count_documents:=count_documents+1;
 END LOOP;
 IF count_documents<>cardinality(p_documents) OR total_bytes>p_max_body_bytes THEN
  RAISE EXCEPTION 'named document absent or body-byte admission exceeded';
 END IF;
 FOR item IN SELECT id FROM unnest(p_documents) d(id) ORDER BY id LOOP
  IF extract(epoch FROM clock_timestamp()-started)*1000>=p_statement_budget_ms THEN
   RAISE EXCEPTION 'retained cache batch deadline exceeded' USING ERRCODE='57014';
  END IF;
  -- Fetch stored/compressed datum costs directly, before expanding cached data.
  SELECT coalesce(pg_column_size(d.fts_simple),0)::BIGINT+coalesce(pg_column_size(d.fts_simple_fingerprints),0)
     +pg_column_size(d.exact_identifiers),octet_length(d.search_text),pg_column_size(d.fts_simple)
    INTO original_stored_cache_bytes,text_bytes,original_vector_bytes
    FROM storage_v2_search_document d WHERE d.id=item.id;
  IF original_stored_cache_bytes>8388608 THEN
   RAISE EXCEPTION 'retained stored cache admission exceeded' USING ERRCODE='54000';
  END IF;
  document_id:=item.id;disposition:=NULL;stored_cache_bytes:=original_stored_cache_bytes;audit_bytes:=0;
  fingerprint_index_bytes:=0;
  original_cache_sha256:=storage_v2_retained_document_cache_payload_sha256(item.id);
  cache_payload_sha256:=original_cache_sha256;
  document_identity_sha256:=storage_v2_retained_posting_document_identity_sha256(item.id);
  SELECT * INTO receipt FROM storage_v2_retained_document_cache_receipt r WHERE r.document_id=item.id;
  IF FOUND THEN
   IF receipt.operation_id IS DISTINCT FROM p_operation OR receipt.manifest_sha256 IS DISTINCT FROM p_manifest
      OR receipt.materialization_sha256 IS DISTINCT FROM p_materializations[array_position(p_documents,item.id)]
      OR receipt.retained_run IS DISTINCT FROM p_retained_run
      OR receipt.retained_run_sha256 IS DISTINCT FROM p_retained_run_sha256
      OR receipt.document_identity_sha256 IS DISTINCT FROM document_identity_sha256
      OR receipt.cache_payload_sha256 IS DISTINCT FROM cache_payload_sha256
      OR receipt.stored_cache_bytes IS DISTINCT FROM stored_cache_bytes
      OR (receipt.fts_changed AND receipt.sql161_receipt_sha256 IS DISTINCT FROM
          (SELECT sha256(convert_to(to_jsonb(r)::TEXT,'UTF8')) FROM storage_v2_document_cache_conversion_receipt r
            WHERE r.document_id=item.id))
      OR (receipt.identifiers_changed AND NOT EXISTS(SELECT 1 FROM storage_v2_document_postings_seal s
          WHERE s.document_id=item.id AND s.materialization_sha256=receipt.materialization_sha256)) THEN
    RAISE EXCEPTION 'retained cache replay requires its exact manifest, context and live final payload';
   END IF;
   original_cache_sha256:=receipt.original_cache_sha256;
   original_stored_cache_bytes:=receipt.original_stored_cache_bytes;audit_bytes:=receipt.audit_bytes;
   fingerprint_index_bytes:=receipt.fingerprint_index_bytes;
   disposition:='REPLAY';RETURN NEXT;CONTINUE;
  END IF;
  SELECT * INTO STRICT old_document FROM storage_v2_search_document d WHERE d.id=item.id;
  fts_change:=old_document.fts_simple_derived IS FALSE AND old_document.fts_simple IS NOT NULL;
  identifiers:=NULL;identifier_change:=FALSE;
  IF cardinality(old_document.exact_identifiers)>0 THEN
   identifiers:=storage_v2_document_word_identifiers(item.id);
   identifier_change:=old_document.exact_identifiers IS NOT DISTINCT FROM identifiers;
  END IF;
  IF NOT fts_change AND NOT identifier_change THEN
   disposition:='RETAIN_UNSUPPORTED';RETURN NEXT;CONTINUE;
  END IF;
  SELECT * INTO old_receipt FROM storage_v2_document_cache_conversion_receipt r WHERE r.document_id=item.id;
  IF fts_change AND FOUND THEN
   RAISE EXCEPTION 'original SQL161 receipt contradicts the unconverted cache';
  END IF;
  SELECT EXISTS(SELECT 1 FROM storage_v2_document_postings_seal s WHERE s.document_id=item.id
     AND s.materialization_sha256=old_document.materialization_sha256) INTO seal_present;
  LOOP
  budget_refusal:=FALSE;attempted_fts:=FALSE;fingerprint_index_bytes:=0;
  BEGIN
   extra_audit:=0;audit_rows:=1;legacy_receipt_sha:=NULL;
   IF fts_change THEN
    -- The unchanged SQL161 trigger proves OLD equals canonical content while
    -- accepting strip(OLD). Reuse OLD's vector here: no second body tokenizer.
    vector:=old_document.fts_simple;attempted_fts:=TRUE;
    PERFORM set_config('mainrag.document_cache_document',item.id::TEXT,TRUE);
    UPDATE storage_v2_search_document d SET fts_simple=strip(vector),fts_simple_derived=TRUE,
      fts_simple_fingerprints=storage_v2_lexical_block_fingerprints(ARRAY[vector]) WHERE d.id=item.id;
    PERFORM set_config('mainrag.document_cache_document','',TRUE);attempted_fts:=FALSE;
    SELECT pg_column_size(d.fts_simple) INTO stripped_vector_bytes FROM storage_v2_search_document d WHERE d.id=item.id;
    -- Model96 bytes per new GIN fingerprint for an unshared key, TID and
    -- amortized tuple/page overhead. Shared keys/compressed lists can cost
    -- less; this is a conservative admission model, not a physical upper bound.
    -- Existing full-FTS lexeme keys are retained; no saving credit is inferred
    -- from removal of identifier keys or future relation compaction.
    SELECT 96::BIGINT*cardinality(d.fts_simple_fingerprints) INTO fingerprint_index_bytes
     FROM storage_v2_search_document d WHERE d.id=item.id;
    INSERT INTO storage_v2_document_cache_conversion_receipt VALUES(item.id,p_operation,p_manifest,
      old_document.materialization_sha256,sha256(tsvectorsend(vector)),sha256(tsvectorsend(strip(vector))),
      text_bytes,original_vector_bytes,stripped_vector_bytes);
    SELECT pg_column_size(r),sha256(convert_to(to_jsonb(r)::TEXT,'UTF8')) INTO extra_audit,legacy_receipt_sha
      FROM storage_v2_document_cache_conversion_receipt r WHERE r.document_id=item.id;
    audit_rows:=audit_rows+1;
   END IF;
   IF identifier_change THEN
    IF NOT seal_present THEN
     PERFORM storage_v2_seal_document_postings(item.id);
     extra_audit:=extra_audit+(SELECT pg_column_size(s) FROM storage_v2_document_postings_seal s WHERE s.document_id=item.id);
     audit_rows:=audit_rows+1;
    END IF;
    -- The unchanged SQL138 guard independently checks complete sealed postings.
    UPDATE storage_v2_search_document d SET exact_identifiers_derived=TRUE,exact_identifiers=ARRAY[]::TEXT[]
     WHERE d.id=item.id;
   END IF;
   SELECT coalesce(pg_column_size(d.fts_simple),0)::BIGINT+coalesce(pg_column_size(d.fts_simple_fingerprints),0)
     +pg_column_size(d.exact_identifiers) INTO stored_cache_bytes FROM storage_v2_search_document d WHERE d.id=item.id;
   cache_payload_sha256:=storage_v2_retained_document_cache_payload_sha256(item.id);
   IF document_identity_sha256 IS DISTINCT FROM storage_v2_retained_posting_document_identity_sha256(item.id) THEN
    RAISE EXCEPTION 'retained cache conversion changed immutable document identity';
   END IF;
   proposed:=ROW(item.id,p_operation,p_manifest,old_document.materialization_sha256,
      p_retained_run,p_retained_run_sha256,document_identity_sha256,original_cache_sha256,cache_payload_sha256,
      original_stored_cache_bytes,stored_cache_bytes,1,fingerprint_index_bytes,fts_change,identifier_change,seal_present,
      legacy_receipt_sha,text_bytes)::storage_v2_retained_document_cache_receipt;
   -- Charge every new stored audit datum plus a conservative128-byte tuple/key
   -- allowance per new audit row. This is not allocated-file reclamation.
   audit_bytes:=pg_column_size(proposed)+extra_audit+128::BIGINT*audit_rows;
   IF stored_cache_bytes+audit_bytes+fingerprint_index_bytes>=original_stored_cache_bytes THEN
    RAISE EXCEPTION 'retained cache has no net stored datum saving' USING ERRCODE='P1660';
   END IF;
   proposed.audit_bytes:=audit_bytes;
   INSERT INTO storage_v2_retained_document_cache_receipt SELECT (proposed).*;
  EXCEPTION
   WHEN SQLSTATE 'P1660' THEN budget_refusal:=TRUE;
   WHEN raise_exception THEN
    IF NOT attempted_fts OR SQLERRM<>'storage-v2 retrieval projections are immutable' THEN RAISE;END IF;
    disposition:='RETAIN_VECTOR_MISMATCH';budget_refusal:=TRUE;
  END;
  IF budget_refusal AND disposition IS DISTINCT FROM 'RETAIN_VECTOR_MISMATCH'
     AND fts_change AND identifier_change THEN
   -- A rejected joint change must not prevent beneficial identifier-only
   -- removal. The preceding subtransaction restored the original full FTS,
   -- shared receipt and seal; this fallback does not tokenize content again.
   fts_change:=FALSE;CONTINUE;
  END IF;
  EXIT;
  END LOOP;
  IF budget_refusal THEN
   -- The subtransaction rolled back both updates and every prospective audit.
   stored_cache_bytes:=original_stored_cache_bytes;audit_bytes:=0;fingerprint_index_bytes:=0;
   cache_payload_sha256:=original_cache_sha256;
   IF storage_v2_retained_document_cache_payload_sha256(item.id) IS DISTINCT FROM original_cache_sha256
      OR storage_v2_retained_posting_document_identity_sha256(item.id) IS DISTINCT FROM document_identity_sha256
      OR EXISTS(SELECT 1 FROM storage_v2_retained_document_cache_receipt r WHERE r.document_id=item.id) THEN
    RAISE EXCEPTION 'retained cache refused transition did not restore its original state';
   END IF;
   IF disposition IS DISTINCT FROM 'RETAIN_VECTOR_MISMATCH' THEN disposition:='RETAIN_NO_SAVINGS';END IF;
  ELSE disposition:='CONVERTED';END IF;
  RETURN NEXT;
 END LOOP;
END $convert$;
REVOKE ALL ON FUNCTION storage_v2_convert_retained_document_caches(UUID,BYTEA,BIGINT[],BYTEA[],BIGINT,INTEGER,BIGINT,BYTEA)
 FROM PUBLIC,mainrag;
COMMENT ON TABLE storage_v2_retained_document_cache_receipt IS
 'Successful net-beneficial cache transitions only; immutable original/final cache and document identities, no live-document GC root. Audit bytes include all newly created receipts/seals and tuple/key allowances; fingerprint_index_bytes is a conservative96-byte-per-key GIN allowance, not an allocated-file measurement. Physical compaction is separate.';
DO $unchanged_contracts$ BEGIN
 PERFORM storage_v2_require_lexical_conversion_operator();
END $unchanged_contracts$;
COMMIT;
