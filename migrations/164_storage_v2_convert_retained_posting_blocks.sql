-- Convert exact retained flat/compact postings to canonical byte locators.
-- This does not rebuild sources or change retained materialization identities.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';

DO $predecessor$
DECLARE expected RECORD;
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user) THEN
  RAISE EXCEPTION 'retained posting migration requires the local schema operator' USING ERRCODE='42501';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 FOR expected IN SELECT * FROM (VALUES
  ('storage_v2_reject_sealed_posting_insert()','3a761a57a194aebdebdcf4fc4b536009cc8d6b5755c43477e68762e42a2e167f'),
  ('storage_v2_validate_byte_posting_insert()','98325bf891c26853fb4f83cf758afef766315475ee29040ecfea92d61634eb9d')
 ) pinned(signature,body_sha256) LOOP
  IF encode(sha256(convert_to((SELECT prosrc FROM pg_proc WHERE oid=expected.signature::REGPROCEDURE),'UTF8')),'hex')
       IS DISTINCT FROM expected.body_sha256 THEN
   RAISE EXCEPTION 'retained posting predecessor differs: %',expected.signature;
  END IF;
 END LOOP;
END $predecessor$;

-- Neither table creates an extra live-document retention root. Historical
-- receipts survive legitimate GC, but a missing replay target always fails.
CREATE TABLE storage_v2_retained_posting_conversion_receipt (
 document_id BIGINT PRIMARY KEY CHECK(document_id>0),
 operation_id UUID NOT NULL,
 manifest_sha256 BYTEA NOT NULL CHECK(octet_length(manifest_sha256)=32),
 materialization_sha256 BYTEA NOT NULL CHECK(octet_length(materialization_sha256)=32),
 original_representation TEXT NOT NULL CHECK(original_representation IN ('FLAT','COMPACT')),
 original_pairs_sha256 BYTEA NOT NULL CHECK(octet_length(original_pairs_sha256)=32),
 byte_payload_sha256 BYTEA NOT NULL CHECK(octet_length(byte_payload_sha256)=32),
 document_identity_sha256 BYTEA NOT NULL CHECK(octet_length(document_identity_sha256)=32),
 posting_count BIGINT NOT NULL CHECK(posting_count>0),
 block_count BIGINT NOT NULL CHECK(block_count>0),
 text_bytes BIGINT NOT NULL CHECK(text_bytes BETWEEN 1 AND 8388608)
);
CREATE TABLE storage_v2_retained_posting_conversion_intent (
 document_id BIGINT PRIMARY KEY,
 operation_id UUID NOT NULL,
 transaction_id XID8 NOT NULL,
 materialization_sha256 BYTEA NOT NULL,
 original_representation TEXT NOT NULL CHECK(original_representation IN ('FLAT','COMPACT')),
 old_row_hashes BYTEA[] NOT NULL,
 new_row_hashes BYTEA[] NOT NULL
);
REVOKE ALL ON storage_v2_retained_posting_conversion_receipt,
 storage_v2_retained_posting_conversion_intent FROM PUBLIC,mainrag;
CREATE TRIGGER storage_v2_retained_posting_receipt_immutable BEFORE UPDATE OR DELETE
 ON storage_v2_retained_posting_conversion_receipt FOR EACH ROW
 EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();

CREATE FUNCTION storage_v2_retained_posting_run_sha256(p_run BIGINT)
RETURNS BYTEA LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
 SELECT sha256(convert_to(jsonb_build_object('run',to_jsonb(r),'generation',to_jsonb(g))::TEXT,'UTF8'))
 FROM storage_v2_ingest_run r JOIN source_generation g ON g.id=r.generation_id WHERE r.id=p_run
$$;
REVOKE ALL ON FUNCTION storage_v2_retained_posting_run_sha256(BIGINT) FROM PUBLIC,mainrag;

CREATE FUNCTION storage_v2_retained_posting_pairs_sha256(p_document BIGINT)
RETURNS BYTEA LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE size BIGINT; result BYTEA;
BEGIN
 SELECT sum(octet_length(term)+8) INTO size FROM (
  SELECT term FROM storage_v2_search_posting WHERE document_id=p_document
  UNION ALL SELECT term FROM storage_v2_compact_posting_block b CROSS JOIN LATERAL unnest(b.terms) term
   WHERE b.document_id=p_document) complete;
 IF size IS NULL OR size>33554432 THEN
  RAISE EXCEPTION 'bounded nonempty original posting payload required';
 END IF;
 SELECT sha256(convert_to(coalesce(jsonb_agg(jsonb_build_array(term,frequency)
   ORDER BY term COLLATE "C",frequency),'[]'::JSONB)::TEXT,'UTF8'))
 INTO result
 FROM (
  SELECT term,term_frequency AS frequency FROM storage_v2_search_posting WHERE document_id=p_document
  UNION ALL
  SELECT pair.term,pair.frequency FROM storage_v2_compact_posting_block b
   CROSS JOIN LATERAL unnest(b.terms,b.term_frequencies) pair(term,frequency)
   WHERE b.document_id=p_document
 ) pairs;
 RETURN result;
END
$$;
REVOKE ALL ON FUNCTION storage_v2_retained_posting_pairs_sha256(BIGINT) FROM PUBLIC,mainrag;

CREATE FUNCTION storage_v2_retained_byte_posting_payload_sha256(p_document BIGINT)
RETURNS BYTEA LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
 SELECT sha256(convert_to(coalesce(jsonb_agg(to_jsonb(b) ORDER BY b.block_order),'[]'::JSONB)::TEXT,'UTF8'))
 FROM storage_v2_byte_posting_block b WHERE b.document_id=p_document
$$;
REVOKE ALL ON FUNCTION storage_v2_retained_byte_posting_payload_sha256(BIGINT) FROM PUBLIC,mainrag;

CREATE FUNCTION storage_v2_retained_posting_document_identity_sha256(p_document BIGINT)
RETURNS BYTEA LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
 SELECT sha256(convert_to((to_jsonb(d)-ARRAY['fts_simple','fts_simple_derived',
   'fts_simple_fingerprints','exact_identifiers','exact_identifiers_derived'])::TEXT,'UTF8'))
 FROM storage_v2_search_document d WHERE d.id=p_document
$$;
REVOKE ALL ON FUNCTION storage_v2_retained_posting_document_identity_sha256(BIGINT) FROM PUBLIC,mainrag;

CREATE FUNCTION storage_v2_retained_posting_delete_guard()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
BEGIN
 IF TG_OP='DELETE' AND TG_TABLE_NAME IN ('storage_v2_search_posting','storage_v2_compact_posting_block')
    AND inet_client_addr() IS NULL
    AND current_setting('role') IN ('none',session_user)
    AND (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    AND storage_v2_is_admin() IS TRUE
    AND EXISTS(SELECT 1 FROM storage_v2_retained_posting_conversion_intent intent
      JOIN storage_v2_search_document document ON document.id=intent.document_id
      WHERE intent.document_id=OLD.document_id AND intent.transaction_id=pg_current_xact_id()
       AND current_setting('mainrag.retained_posting_operation',TRUE)=intent.operation_id::TEXT
       AND intent.materialization_sha256=document.materialization_sha256
       AND intent.original_representation=CASE TG_TABLE_NAME
           WHEN 'storage_v2_search_posting' THEN 'FLAT' ELSE 'COMPACT' END
       AND sha256(convert_to(to_jsonb(OLD)::TEXT,'UTF8'))=ANY(intent.old_row_hashes)) THEN
  RETURN OLD;
 END IF;
 RAISE EXCEPTION 'retained posting state is immutable outside its exact conversion intent';
END $$;
REVOKE ALL ON FUNCTION storage_v2_retained_posting_delete_guard() FROM PUBLIC,mainrag;
DROP TRIGGER storage_v2_search_posting_immutable ON storage_v2_search_posting;
CREATE TRIGGER storage_v2_search_posting_immutable BEFORE UPDATE OR DELETE ON storage_v2_search_posting
 FOR EACH ROW EXECUTE FUNCTION storage_v2_retained_posting_delete_guard();
DROP TRIGGER storage_v2_compact_posting_immutable ON storage_v2_compact_posting_block;
CREATE TRIGGER storage_v2_compact_posting_immutable BEFORE UPDATE OR DELETE ON storage_v2_compact_posting_block
 FOR EACH ROW EXECUTE FUNCTION storage_v2_retained_posting_delete_guard();

-- Preserve the entire existing FLAT->COMPACT guard. The additional byte branch
-- accepts only the complete exact rows owned by this transaction's root intent.
DO $sealed_transition$
DECLARE definition TEXT; marker TEXT:=E'BEGIN\n';
BEGIN
 definition:=pg_get_functiondef('storage_v2_reject_sealed_posting_insert()'::REGPROCEDURE);
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained posting sealed guard boundary differs';
 END IF;
 EXECUTE replace(definition,marker,$allow$BEGIN
 IF TG_TABLE_NAME='storage_v2_byte_posting_block'
    AND inet_client_addr() IS NULL
    AND current_setting('role') IN ('none',session_user)
    AND (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    AND storage_v2_is_admin() IS TRUE
    AND EXISTS(SELECT 1 FROM storage_v2_new_postings)
    AND NOT EXISTS(SELECT 1 FROM storage_v2_new_postings added WHERE NOT EXISTS(
      SELECT 1 FROM storage_v2_retained_posting_conversion_intent intent
       JOIN storage_v2_search_document retained_document ON retained_document.id=intent.document_id
       JOIN storage_v2_document_postings_seal seal ON seal.document_id=retained_document.id
      WHERE intent.document_id=added.document_id AND intent.transaction_id=pg_current_xact_id()
       AND current_setting('mainrag.retained_posting_operation',TRUE)=intent.operation_id::TEXT
       AND intent.materialization_sha256=retained_document.materialization_sha256
       AND seal.materialization_sha256=retained_document.materialization_sha256
       AND sha256(convert_to(to_jsonb(added)::TEXT,'UTF8'))=ANY(intent.new_row_hashes)
       AND cardinality(intent.new_row_hashes)=(SELECT count(*) FROM storage_v2_new_postings all_added
           WHERE all_added.document_id=intent.document_id))) THEN
  RETURN NULL;
 END IF;
$allow$);
END $sealed_transition$;

CREATE FUNCTION storage_v2_convert_retained_postings(
 p_operation UUID,p_manifest BYTEA,p_document BIGINT,p_materialization BYTEA,
 p_original_pairs_sha256 BYTEA,p_max_body_bytes BIGINT,p_statement_budget_ms INTEGER,
 p_retained_run BIGINT DEFAULT NULL,p_retained_run_sha256 BYTEA DEFAULT NULL
) RETURNS TABLE(document_id BIGINT,disposition TEXT,original_representation TEXT,
                posting_count BIGINT,block_count BIGINT,original_pairs_sha256 BYTEA,byte_payload_sha256 BYTEA)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public
SET row_security=off AS $convert$
DECLARE document storage_v2_search_document; receipt storage_v2_retained_posting_conversion_receipt;
 old_terms TEXT[]; old_frequencies BIGINT[]; terms TEXT[]; frequencies BIGINT[];
 starts INTEGER[]; lengths INTEGER[]; old_hashes BYTEA[]; new_hashes BYTEA[];
 body TEXT; bytes BYTEA; body_size BIGINT; token_count BIGINT; identity BYTEA;
 v_rows JSONB; flat BOOLEAN; compact BOOLEAN; budget NUMERIC; lock_budget NUMERIC;
 actual_blocks BIGINT; removed BIGINT; original_bytes BIGINT; started TIMESTAMPTZ:=clock_timestamp();
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user)
    OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'retained posting conversion requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 IF p_operation IS NULL OR p_document IS NULL OR p_document<=0
    OR octet_length(p_manifest) IS DISTINCT FROM 32 OR octet_length(p_materialization) IS DISTINCT FROM 32
    OR octet_length(p_original_pairs_sha256) IS DISTINCT FROM 32
    OR p_max_body_bytes IS NULL OR p_max_body_bytes NOT BETWEEN 1 AND 8388608
    OR p_statement_budget_ms IS NULL OR p_statement_budget_ms NOT BETWEEN 100 AND 30000
    OR (p_retained_run IS NULL)<>(p_retained_run_sha256 IS NULL)
    OR (p_retained_run IS NOT NULL AND (p_retained_run<=0 OR octet_length(p_retained_run_sha256) IS DISTINCT FROM 32)) THEN
  RAISE EXCEPTION 'exact named posting manifest, identities and bounded budgets required';
 END IF;
 budget:=extract(epoch FROM current_setting('statement_timeout')::INTERVAL)*1000;
 lock_budget:=extract(epoch FROM current_setting('lock_timeout')::INTERVAL)*1000;
 IF budget<=0 OR budget>p_statement_budget_ms OR lock_budget<=0 OR lock_budget>3000 THEN
  RAISE EXCEPTION 'prearmed statement/lock deadlines required';
 END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('storage-v2-posting-conversion-v1',0)) THEN
  RAISE EXCEPTION 'another storage maintenance operator owns the writer boundary';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 LOCK TABLE storage_v2_ingest_run,source_generation,logical_source IN SHARE MODE NOWAIT;
 LOCK TABLE storage_v2_search_document IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 LOCK TABLE storage_v2_search_posting,storage_v2_compact_posting_block,storage_v2_byte_posting_block
  IN SHARE ROW EXCLUSIVE MODE NOWAIT;
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
    OR EXISTS(SELECT 1 FROM storage_v2_retained_posting_conversion_intent)
    OR EXISTS(SELECT 1 FROM storage_v2_posting_conversion_intent) THEN
  RAISE EXCEPTION 'retained posting conversion requires an exact quiescent maintenance boundary';
 END IF;
 SELECT octet_length(d.search_text) INTO body_size FROM storage_v2_search_document d
  WHERE d.id=p_document AND d.materialization_sha256=p_materialization FOR UPDATE;
 IF NOT FOUND OR body_size NOT BETWEEN 1 AND p_max_body_bytes THEN
  RAISE EXCEPTION 'named posting document absent, changed or body-byte admission exceeded';
 END IF;
 SELECT * INTO STRICT document FROM storage_v2_search_document d WHERE d.id=p_document;
 identity:=sha256(convert_to((to_jsonb(document)-ARRAY['fts_simple','fts_simple_derived',
   'fts_simple_fingerprints','exact_identifiers','exact_identifiers_derived'])::TEXT,'UTF8'));
 IF NOT EXISTS(SELECT 1 FROM storage_v2_document_postings_seal s
      WHERE s.document_id=p_document AND s.materialization_sha256=p_materialization) THEN
  RAISE EXCEPTION 'retained posting document requires its exact original seal';
 END IF;
 SELECT * INTO receipt FROM storage_v2_retained_posting_conversion_receipt r WHERE r.document_id=p_document;
 IF FOUND THEN
  SELECT count(*),sha256(convert_to(coalesce(jsonb_agg(to_jsonb(b) ORDER BY b.block_order),'[]'::JSONB)::TEXT,'UTF8'))
   INTO actual_blocks,byte_payload_sha256 FROM storage_v2_byte_posting_block b WHERE b.document_id=p_document;
  IF receipt.operation_id IS DISTINCT FROM p_operation OR receipt.manifest_sha256 IS DISTINCT FROM p_manifest
     OR receipt.materialization_sha256 IS DISTINCT FROM p_materialization
     OR receipt.original_pairs_sha256 IS DISTINCT FROM p_original_pairs_sha256
     OR receipt.document_identity_sha256 IS DISTINCT FROM identity
     OR actual_blocks<>receipt.block_count OR byte_payload_sha256 IS DISTINCT FROM receipt.byte_payload_sha256
     OR EXISTS(SELECT 1 FROM storage_v2_search_posting WHERE storage_v2_search_posting.document_id=p_document)
     OR EXISTS(SELECT 1 FROM storage_v2_compact_posting_block WHERE storage_v2_compact_posting_block.document_id=p_document) THEN
   RAISE EXCEPTION 'retained posting replay identity or representation differs';
  END IF;
  document_id:=p_document;disposition:='REPLAY';original_representation:=receipt.original_representation;
  posting_count:=receipt.posting_count;block_count:=receipt.block_count;
  original_pairs_sha256:=receipt.original_pairs_sha256;RETURN NEXT;RETURN;
 END IF;
 flat:=EXISTS(SELECT 1 FROM storage_v2_search_posting p WHERE p.document_id=p_document);
 compact:=EXISTS(SELECT 1 FROM storage_v2_compact_posting_block p WHERE p.document_id=p_document);
 IF flat=compact OR EXISTS(SELECT 1 FROM storage_v2_byte_posting_block p WHERE p.document_id=p_document)
    OR EXISTS(SELECT 1 FROM storage_v2_posting_conversion c WHERE c.document_id=p_document) THEN
  RAISE EXCEPTION 'retained posting conversion requires one unmixed unconverted old representation';
 END IF;
 IF compact AND EXISTS(SELECT 1 FROM storage_v2_compact_posting_block b WHERE b.document_id=p_document
      AND b.fingerprints IS DISTINCT FROM storage_v2_posting_fingerprints(b.terms)) THEN
  RAISE EXCEPTION 'original compact posting fingerprint differs from complete terms';
 END IF;
 SELECT sum(octet_length(term)+8) INTO original_bytes FROM (
  SELECT p.term FROM storage_v2_search_posting p WHERE p.document_id=p_document
  UNION ALL SELECT term FROM storage_v2_compact_posting_block b CROSS JOIN LATERAL unnest(b.terms) term
    WHERE b.document_id=p_document) complete;
 IF original_bytes IS NULL OR original_bytes>33554432 THEN
  RAISE EXCEPTION 'bounded nonempty original posting payload required';
 END IF;
 original_representation:=CASE WHEN flat THEN 'FLAT' ELSE 'COMPACT' END;
 SELECT array_agg(term ORDER BY term COLLATE "C"),array_agg(frequency ORDER BY term COLLATE "C"),
        sha256(convert_to(jsonb_agg(jsonb_build_array(term,frequency) ORDER BY term COLLATE "C",frequency)::TEXT,'UTF8'))
  INTO old_terms,old_frequencies,original_pairs_sha256 FROM (
   SELECT p.term,p.term_frequency AS frequency FROM storage_v2_search_posting p WHERE p.document_id=p_document
   UNION ALL SELECT pair.term,pair.frequency FROM storage_v2_compact_posting_block b
    CROSS JOIN LATERAL unnest(b.terms,b.term_frequencies) pair(term,frequency) WHERE b.document_id=p_document
  ) old_pairs;
 IF original_pairs_sha256 IS DISTINCT FROM p_original_pairs_sha256
    OR cardinality(old_terms)<>(SELECT count(DISTINCT t) FROM unnest(old_terms) t) THEN
  RAISE EXCEPTION 'original posting pairs differ from the frozen exact manifest';
 END IF;
 SELECT array_agg(sha256(convert_to(stored_row::TEXT,'UTF8'))) INTO old_hashes FROM (
  SELECT to_jsonb(p) AS stored_row FROM storage_v2_search_posting p WHERE p.document_id=p_document
  UNION ALL SELECT to_jsonb(b) FROM storage_v2_compact_posting_block b WHERE b.document_id=p_document
 ) stored;
 body:=lower(document.search_text);bytes:=convert_to(body,'UTF8');
 -- Both exact tokenizer classes run once. Positions use normalized UTF-8 bytes;
 -- Unicode lowercasing may change byte length and cannot use original offsets.
 WITH word_parts AS MATERIALIZED (
  SELECT p.parts[2] AS token,1::BIGINT AS word_count,
   coalesce(sum(octet_length(p.parts[1])+octet_length(p.parts[2])) OVER(
    ORDER BY p.n ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0)+octet_length(p.parts[1])+1 AS byte_start
  FROM regexp_matches(body COLLATE "default",'([^[:alnum:]_]*)([[:alnum:]_]+)','g') WITH ORDINALITY p(parts,n)
 ), whitespace_parts AS MATERIALIZED (
  SELECT p.parts[2] AS token,0::BIGINT AS word_count,
   coalesce(sum(octet_length(p.parts[1])+octet_length(p.parts[2])) OVER(
    ORDER BY p.n ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0)+octet_length(p.parts[1])+1 AS byte_start
  FROM regexp_matches(body COLLATE "default",'([[:space:]]*)([^[:space:]]+)','g') WITH ORDINALITY p(parts,n)
 ), searchable AS (
  SELECT * FROM word_parts UNION ALL SELECT * FROM whitespace_parts
   WHERE token !~ '^[[:alnum:]_]+$' AND token ~ '[[:alnum:]_]'
 ), frequency AS (
  SELECT token,count(*)::BIGINT AS frequency,sum(word_count)::BIGINT AS word_count,
   min(byte_start)::INTEGER AS byte_start,octet_length(token) AS byte_length FROM searchable GROUP BY token
 ) SELECT coalesce(sum(word_count),0)::BIGINT,array_agg(token ORDER BY token COLLATE "C"),
   array_agg(frequency ORDER BY token COLLATE "C"),array_agg(byte_start ORDER BY token COLLATE "C"),
   array_agg(byte_length ORDER BY token COLLATE "C") INTO token_count,terms,frequencies,starts,lengths FROM frequency;
 IF (terms,frequencies) IS DISTINCT FROM (old_terms,old_frequencies)
    OR token_count IS DISTINCT FROM document.token_count
    OR EXISTS(SELECT 1 FROM unnest(terms,starts,lengths) t(term,start,length)
      WHERE start<=0 OR start::BIGINT+length-1>octet_length(bytes)
       OR convert_from(substring(bytes FROM start FOR length),'UTF8') COLLATE "default" IS DISTINCT FROM term) THEN
  RAISE EXCEPTION 'old complete posting pairs or token count differ from canonical content';
 END IF;
 IF extract(epoch FROM clock_timestamp()-started)*1000>=p_statement_budget_ms THEN
  RAISE EXCEPTION 'retained posting conversion deadline exceeded' USING ERRCODE='57014';
 END IF;
 WITH blocks AS (
  SELECT p_document AS document_id,(t.n-1)/256 AS block_order,
   array_agg(CASE WHEN octet_length(t.term)<=16 THEN t.term ELSE NULL END ORDER BY t.n) AS cached_terms,
   array_agg(t.start ORDER BY t.n) AS text_byte_starts,array_agg(t.length ORDER BY t.n) AS text_byte_lengths,
   array_agg(t.frequency ORDER BY t.n) AS term_frequencies,
   storage_v2_posting_fingerprints(array_agg(t.term ORDER BY t.n)) AS fingerprints,
   16::SMALLINT AS cache_max_term_bytes
  FROM unnest(terms,frequencies,starts,lengths) WITH ORDINALITY t(term,frequency,start,length,n)
  GROUP BY (t.n-1)/256
 ) SELECT jsonb_agg(to_jsonb(b) ORDER BY b.block_order),
   array_agg(sha256(convert_to(to_jsonb(b)::TEXT,'UTF8')) ORDER BY b.block_order)
  INTO v_rows,new_hashes FROM blocks b;
 INSERT INTO storage_v2_retained_posting_conversion_intent VALUES(p_document,p_operation,pg_current_xact_id(),
  p_materialization,original_representation,old_hashes,new_hashes);
 PERFORM set_config('mainrag.retained_posting_operation',p_operation::TEXT,TRUE);
 IF flat THEN DELETE FROM storage_v2_search_posting p WHERE p.document_id=p_document;
 ELSE DELETE FROM storage_v2_compact_posting_block b WHERE b.document_id=p_document;END IF;
 GET DIAGNOSTICS removed=ROW_COUNT;
 IF removed<>cardinality(old_hashes) THEN RAISE EXCEPTION 'old posting deletion differs from exact intent';END IF;
 INSERT INTO storage_v2_byte_posting_block
 SELECT * FROM jsonb_populate_recordset(NULL::storage_v2_byte_posting_block,v_rows);
 SELECT count(*),sha256(convert_to(jsonb_agg(to_jsonb(b) ORDER BY b.block_order)::TEXT,'UTF8'))
  INTO block_count,byte_payload_sha256 FROM storage_v2_byte_posting_block b WHERE b.document_id=p_document;
 IF block_count<>cardinality(new_hashes) OR byte_payload_sha256 IS DISTINCT FROM sha256(convert_to(v_rows::TEXT,'UTF8')) THEN
  RAISE EXCEPTION 'actual byte posting payload differs from exact conversion';
 END IF;
 posting_count:=cardinality(terms);document_id:=p_document;disposition:='CONVERTED';
 INSERT INTO storage_v2_retained_posting_conversion_receipt VALUES(p_document,p_operation,p_manifest,p_materialization,
  original_representation,original_pairs_sha256,byte_payload_sha256,identity,posting_count,block_count,body_size);
 DELETE FROM storage_v2_retained_posting_conversion_intent i WHERE i.document_id=p_document;
 PERFORM set_config('mainrag.retained_posting_operation','',TRUE);
 RETURN NEXT;
END $convert$;
REVOKE ALL ON FUNCTION storage_v2_convert_retained_postings(UUID,BYTEA,BIGINT,BYTEA,BYTEA,BIGINT,INTEGER,BIGINT,BYTEA)
 FROM PUBLIC,mainrag;

-- Advance only the reviewed guards and catalog objects affected above. Keep
-- the old FLAT->COMPACT and SQL161 maintenance surfaces usable and pinned.
DO $advance_contract$
DECLARE definition TEXT; marker TEXT:=$old$tgfoid='storage_v2_reject_retrieval_mutation()'::regprocedure$old$;
BEGIN
 definition:=pg_get_functiondef('storage_v2_posting_conversion_require_operator()'::REGPROCEDURE);
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1
    OR strpos(definition,'<>23')=0 THEN
  RAISE EXCEPTION 'retained posting maintenance contract boundary differs';
 END IF;
 EXECUTE replace(replace(definition,marker,
  $new$tgfoid IN ('storage_v2_reject_retrieval_mutation()'::regprocedure,'storage_v2_retained_posting_delete_guard()'::regprocedure)$new$),
  '<>23','<>24');
 UPDATE storage_v2_posting_conversion_contract c SET definition_sha256=sha256(convert_to(pg_get_functiondef(c.signature::REGPROCEDURE),'UTF8'))
  WHERE c.signature IN ('storage_v2_reject_sealed_posting_insert()',
                        'storage_v2_posting_conversion_require_operator()');
 INSERT INTO storage_v2_posting_conversion_contract(signature,definition_sha256,owner_oid,acl_sha256)
  SELECT p.oid::REGPROCEDURE::TEXT,sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),
   p.proowner,sha256(convert_to(coalesce(p.proacl::TEXT,''),'UTF8'))
  FROM pg_proc p WHERE p.oid='storage_v2_retained_posting_delete_guard()'::REGPROCEDURE;
 UPDATE storage_v2_posting_conversion_catalog_contract c
  SET identity_sha256=storage_v2_posting_conversion_relation_sha256(c.relation_oid::REGCLASS)
  WHERE c.relation_oid IN ('storage_v2_search_posting'::REGCLASS,'storage_v2_compact_posting_block'::REGCLASS);
 PERFORM storage_v2_posting_conversion_require_operator();
END $advance_contract$;
COMMENT ON FUNCTION storage_v2_convert_retained_postings(UUID,BYTEA,BIGINT,BYTEA,BYTEA,BIGINT,INTEGER,BIGINT,BYTEA) IS
 'Exact retained FLAT/COMPACT to BYTE representation; atomic named document, no source rebuild or physical-saving claim. Root supplies frozen manifest, inactive-writer/package/resource evidence and unknown-outcome reconciliation.';
COMMIT;
