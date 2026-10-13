-- Retire one named cached vector block after complete canonical-vector equality.
-- Logical rows and owners remain identical. No physical compaction runs here.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';
DO $guard$
BEGIN
 IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user)
    OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'lexical conversion migration requires local administrative operator' USING ERRCODE='42501';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 IF EXISTS(SELECT 1 FROM pg_class WHERE oid IN ('storage_v2_compact_lexical_block'::REGCLASS,
    'storage_v2_derived_lexical_block'::REGCLASS) AND (relowner<>'mainrag_v2_frontier_owner'::REGROLE
    OR NOT relrowsecurity OR NOT relforcerowsecurity))
    OR NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_compact_lexical_block'::REGCLASS
      AND tgname='storage_v2_compact_lexical_immutable' AND tgenabled='O' AND tgtype=27
      AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE)
    OR strpos(pg_get_viewdef('storage_v2_lexical_block_all'::REGCLASS),
        'storage_v2_derived_lexical_vectors')=0 THEN
  RAISE EXCEPTION 'retained lexical representation or immutable boundary differs';
 END IF;
END $guard$;

-- Audit identities have no FK: they do not create a new native-GC retention root.
CREATE TABLE storage_v2_lexical_conversion_receipt (
 occurrence_id BIGINT NOT NULL,block_order BIGINT NOT NULL,
 operation_id UUID NOT NULL,manifest_sha256 BYTEA NOT NULL CHECK(octet_length(manifest_sha256)=32),
 source_id BIGINT NOT NULL,artifact_version_id BIGINT NOT NULL,document_id BIGINT NOT NULL,
 materialization_sha256 BYTEA NOT NULL CHECK(octet_length(materialization_sha256)=32),
 original_payload_sha256 BYTEA NOT NULL CHECK(octet_length(original_payload_sha256)=32),
 derived_payload_sha256 BYTEA NOT NULL CHECK(octet_length(derived_payload_sha256)=32),
 body_bytes BIGINT NOT NULL,segment_count INTEGER NOT NULL CHECK(segment_count BETWEEN 1 AND 64),
 original_datum_bytes BIGINT NOT NULL,derived_datum_bytes BIGINT NOT NULL,
 PRIMARY KEY(occurrence_id,block_order)
);
CREATE TABLE storage_v2_lexical_conversion_intent (
 occurrence_id BIGINT NOT NULL,block_order BIGINT NOT NULL,transaction_id XID8 NOT NULL,
 original_payload_sha256 BYTEA NOT NULL,derived_payload_sha256 BYTEA NOT NULL,
 PRIMARY KEY(occurrence_id,block_order)
);
CREATE TABLE storage_v2_lexical_conversion_function_contract (
 signature TEXT PRIMARY KEY,definition_sha256 BYTEA NOT NULL,owner_oid OID NOT NULL,acl_sha256 BYTEA NOT NULL
);
CREATE TABLE storage_v2_lexical_conversion_catalog_contract (
 relation_oid OID PRIMARY KEY,identity_sha256 BYTEA NOT NULL
);
CREATE TABLE storage_v2_lexical_conversion_contract_seal (
 singleton BOOLEAN PRIMARY KEY CHECK(singleton),function_count INTEGER NOT NULL,
 catalog_count INTEGER NOT NULL,contract_sha256 BYTEA NOT NULL CHECK(octet_length(contract_sha256)=32)
);
REVOKE ALL ON storage_v2_lexical_conversion_receipt,storage_v2_lexical_conversion_intent,
 storage_v2_lexical_conversion_function_contract,storage_v2_lexical_conversion_catalog_contract,
 storage_v2_lexical_conversion_contract_seal FROM PUBLIC,mainrag;
CREATE TRIGGER storage_v2_lexical_conversion_receipt_immutable BEFORE UPDATE OR DELETE
 ON storage_v2_lexical_conversion_receipt FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
CREATE TRIGGER storage_v2_lexical_conversion_seal_immutable BEFORE UPDATE OR DELETE
 ON storage_v2_lexical_conversion_contract_seal FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();

CREATE FUNCTION storage_v2_guard_converted_lexical_delete() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE intent storage_v2_lexical_conversion_intent; derived storage_v2_derived_lexical_block;
BEGIN
 IF TG_OP<>'DELETE' OR inet_client_addr() IS NOT NULL OR current_setting('role') NOT IN ('none',session_user)
    OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'storage-v2 retrieval projections are immutable' USING ERRCODE='42501';
 END IF;
 SELECT * INTO intent FROM storage_v2_lexical_conversion_intent
  WHERE occurrence_id=OLD.occurrence_id AND block_order=OLD.block_order;
 SELECT * INTO derived FROM storage_v2_derived_lexical_block
  WHERE occurrence_id=OLD.occurrence_id AND block_order=OLD.block_order;
 IF intent.transaction_id IS DISTINCT FROM pg_current_xact_id()
    OR intent.original_payload_sha256 IS DISTINCT FROM sha256(convert_to(to_jsonb(OLD)::TEXT,'UTF8'))
    OR intent.derived_payload_sha256 IS DISTINCT FROM sha256(convert_to(to_jsonb(derived)::TEXT,'UTF8')) THEN
  RAISE EXCEPTION 'verified same-transaction lexical transition required';
 END IF;
 RETURN OLD;
END $$;
REVOKE ALL ON FUNCTION storage_v2_guard_converted_lexical_delete() FROM PUBLIC,mainrag;
DROP TRIGGER storage_v2_compact_lexical_immutable ON storage_v2_compact_lexical_block;
CREATE TRIGGER storage_v2_compact_lexical_immutable BEFORE UPDATE OR DELETE
 ON storage_v2_compact_lexical_block FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_converted_lexical_delete();

CREATE FUNCTION storage_v2_require_lexical_conversion_operator() RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE pinned RECORD;seal storage_v2_lexical_conversion_contract_seal;observed BYTEA;
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user) OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'lexical conversion requires local administrative operator' USING ERRCODE='42501';
 END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('storage-v2-posting-conversion-v1',0)) THEN
  RAISE EXCEPTION 'another storage maintenance operator owns the writer boundary';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 SELECT * INTO seal FROM storage_v2_lexical_conversion_contract_seal WHERE singleton;
 SELECT sha256(convert_to(jsonb_build_object(
   'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),
   'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8')) INTO observed;
 IF seal.contract_sha256 IS DISTINCT FROM observed
    OR seal.function_count IS DISTINCT FROM (SELECT count(*)::INTEGER FROM storage_v2_lexical_conversion_function_contract)
    OR seal.catalog_count IS DISTINCT FROM (SELECT count(*)::INTEGER FROM storage_v2_lexical_conversion_catalog_contract) THEN
  RAISE EXCEPTION 'lexical conversion reader/catalog contract is incomplete';
 END IF;
 FOR pinned IN SELECT * FROM storage_v2_lexical_conversion_function_contract LOOP
  IF (SELECT proowner FROM pg_proc WHERE oid=pinned.signature::REGPROCEDURE) IS DISTINCT FROM pinned.owner_oid
     OR sha256(convert_to(pg_get_functiondef(pinned.signature::REGPROCEDURE),'UTF8')) IS DISTINCT FROM pinned.definition_sha256
     OR (SELECT sha256(convert_to(coalesce(proacl::TEXT,''),'UTF8')) FROM pg_proc
       WHERE oid=pinned.signature::REGPROCEDURE) IS DISTINCT FROM pinned.acl_sha256 THEN
   RAISE EXCEPTION 'lexical conversion reader/verifier authority changed: %',pinned.signature;
  END IF;
 END LOOP;
 FOR pinned IN SELECT * FROM storage_v2_lexical_conversion_catalog_contract LOOP
  IF storage_v2_posting_conversion_relation_sha256(pinned.relation_oid::REGCLASS)
       IS DISTINCT FROM pinned.identity_sha256 THEN
   RAISE EXCEPTION 'lexical conversion representation/catalog changed';
  END IF;
 END LOOP;
END $$;
REVOKE ALL ON FUNCTION storage_v2_require_lexical_conversion_operator() FROM PUBLIC,mainrag;

CREATE FUNCTION storage_v2_convert_retained_lexical_block(
 p_operation UUID,p_manifest BYTEA,p_occurrence BIGINT,p_block BIGINT,
 p_original_payload BYTEA,p_materialization BYTEA,p_max_body_bytes BIGINT,p_statement_budget_ms INTEGER,
 p_retained_run BIGINT DEFAULT NULL,p_retained_run_sha256 BYTEA DEFAULT NULL
) RETURNS TABLE(disposition TEXT,segment_count INTEGER,original_datum_bytes BIGINT,derived_datum_bytes BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE old_block storage_v2_compact_lexical_block; new_block storage_v2_derived_lexical_block;
 receipt storage_v2_lexical_conversion_receipt; document RECORD;
 run storage_v2_ingest_run; generation source_generation; frozen_sha BYTEA;
 statement_ms NUMERIC;lock_ms NUMERIC;scratch_bytes BIGINT;body TEXT;bytes BYTEA;body_characters INTEGER;body_length INTEGER;
 boundary INTEGER;v_utf8_window TEXT;window_bytes INTEGER;window_characters INTEGER;
 window_character_start INTEGER:=1;window_byte_start INTEGER:=1;anchors JSONB:='{}'::JSONB;
 byte_starts INTEGER[];byte_lengths INTEGER[];vectors TSVECTOR[];hashes BYTEA[];
 masks BIT(128)[];first_terms TEXT[];first_ordinals SMALLINT[];
BEGIN
 PERFORM storage_v2_require_lexical_conversion_operator();
 IF p_operation IS NULL OR octet_length(p_manifest) IS DISTINCT FROM 32
    OR p_occurrence IS NULL OR p_occurrence<=0 OR p_block IS NULL OR p_block<0
    OR octet_length(p_original_payload) IS DISTINCT FROM 32 OR octet_length(p_materialization) IS DISTINCT FROM 32
    OR p_max_body_bytes IS NULL OR p_max_body_bytes NOT BETWEEN 1 AND 8388608
    OR p_statement_budget_ms IS NULL OR p_statement_budget_ms NOT BETWEEN 100 AND 30000
    OR (p_retained_run IS NULL) IS DISTINCT FROM (p_retained_run_sha256 IS NULL)
    OR (p_retained_run IS NOT NULL AND (p_retained_run<=0 OR octet_length(p_retained_run_sha256) IS DISTINCT FROM 32)) THEN
  RAISE EXCEPTION 'exact named lexical block and bounded conversion identities required';
 END IF;
 statement_ms:=extract(epoch FROM current_setting('statement_timeout')::INTERVAL)*1000;
 lock_ms:=extract(epoch FROM current_setting('lock_timeout')::INTERVAL)*1000;
 IF statement_ms<=0 OR statement_ms>p_statement_budget_ms OR lock_ms<=0 OR lock_ms>3000 THEN
  RAISE EXCEPTION 'prearmed statement/lock deadlines required';
 END IF;
 -- Freeze writer admission and all target representations before reading payloads.
 LOCK TABLE storage_v2_ingest_run,source_generation,logical_source IN SHARE MODE NOWAIT;
 LOCK TABLE storage_v2_compact_lexical_block,storage_v2_derived_lexical_block,
   storage_v2_lexical_segment IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 LOCK TABLE occurrence,artifact_version,storage_v2_search_document,storage_v2_search_view_document
   IN SHARE MODE NOWAIT;
 IF EXISTS(SELECT 1 FROM storage_v2_ingest_run WHERE status='building'
             AND (p_retained_run IS NULL OR id<>p_retained_run)) THEN
  RAISE EXCEPTION 'unbound building run prevents lexical conversion';
 END IF;
 IF p_retained_run IS NOT NULL THEN
  SELECT * INTO STRICT run FROM storage_v2_ingest_run WHERE id=p_retained_run FOR SHARE NOWAIT;
  SELECT * INTO STRICT generation FROM source_generation WHERE id=run.generation_id FOR SHARE NOWAIT;
  frozen_sha:=sha256(convert_to(jsonb_build_object('run',to_jsonb(run),'generation',to_jsonb(generation))::TEXT,'UTF8'));
  IF run.status<>'building' OR frozen_sha IS DISTINCT FROM p_retained_run_sha256 THEN
   RAISE EXCEPTION 'retained inert run metadata differs from frozen owner state';
  END IF;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_locks l JOIN logical_source s ON
       l.classid::BIGINT=((hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)>>32)&4294967295)
       AND l.objid::BIGINT=(hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)&4294967295)
       WHERE l.locktype='advisory' AND l.granted AND l.objsubid=1 AND l.pid<>pg_backend_pid())
    OR EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
       AND state<>'idle' AND query ~* '(insert|update|delete|merge|copy|create[[:space:]]+index|reindex|vacuum|cluster|refresh[[:space:]]+materialized|storage_v2_materialize_reader_metadata)') THEN
  RAISE EXCEPTION 'lexical conversion requires writer-free controlled maintenance';
 END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('mainrag.lexical-segment:'||p_occurrence::TEXT,0)) THEN
  RAISE EXCEPTION 'lexical occurrence writer owns the boundary';
 END IF;
 SELECT o.source_id,d.id AS document_id,d.materialization_sha256,octet_length(d.search_text) AS body_bytes,
        o.artifact_version_id INTO document
  FROM occurrence o JOIN artifact_version a ON a.id=o.artifact_version_id AND a.source_id=o.source_id
  JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0
  JOIN storage_v2_search_document d ON d.id=b.document_id AND d.component_kind='node' AND d.node_id=a.content_root_node_id
  WHERE o.id=p_occurrence;
 IF NOT FOUND OR document.materialization_sha256 IS DISTINCT FROM p_materialization
    OR document.body_bytes>p_max_body_bytes OR storage_v2_can_access_source(document.source_id,'write') IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'exact authorized canonical lexical document required';
 END IF;
 SELECT * INTO receipt FROM storage_v2_lexical_conversion_receipt
  WHERE occurrence_id=p_occurrence AND block_order=p_block;
 IF FOUND THEN
  SELECT * INTO new_block FROM storage_v2_derived_lexical_block WHERE occurrence_id=p_occurrence AND block_order=p_block;
  IF NOT FOUND OR EXISTS(SELECT 1 FROM storage_v2_compact_lexical_block WHERE occurrence_id=p_occurrence AND block_order=p_block)
     OR receipt.operation_id IS DISTINCT FROM p_operation OR receipt.manifest_sha256 IS DISTINCT FROM p_manifest
     OR receipt.original_payload_sha256 IS DISTINCT FROM p_original_payload
     OR receipt.materialization_sha256 IS DISTINCT FROM p_materialization OR receipt.document_id<>document.document_id
     OR receipt.source_id<>document.source_id OR receipt.artifact_version_id<>document.artifact_version_id
     OR receipt.derived_payload_sha256 IS DISTINCT FROM sha256(convert_to(to_jsonb(new_block)::TEXT,'UTF8')) THEN
   RAISE EXCEPTION 'lexical conversion replay requires exact live destination and original manifest';
  END IF;
  RETURN QUERY SELECT 'REPLAY'::TEXT,receipt.segment_count,receipt.original_datum_bytes,receipt.derived_datum_bytes;
  RETURN;
 END IF;
 SELECT * INTO old_block FROM storage_v2_compact_lexical_block
  WHERE occurrence_id=p_occurrence AND block_order=p_block FOR UPDATE NOWAIT;
 IF NOT FOUND OR cardinality(old_block.segment_orders) NOT BETWEEN 1 AND 64 THEN
  RAISE EXCEPTION 'exact unconverted retained lexical block required';
 END IF;
 -- Canonical-body admission alone cannot bound weighted context or repeated
 -- segments. Bound the complete stored row and total reconstruction scratch
 -- before JSON hashing, anchor work or any weighted text parsing. Four bytes
 -- per character conservatively bounds UTF8 canonical slices, including overlap.
 SELECT coalesce(sum(octet_length(prefix)::BIGINT+octet_length(kind)),0)
  INTO scratch_bytes FROM unnest(old_block.context_prefixes,old_block.chunk_types) s(prefix,kind);
 scratch_bytes:=scratch_bytes+(SELECT coalesce(sum(pg_column_size(value)::BIGINT),0)
    FROM unnest(old_block.fts_vectors) vector(value))
   +(SELECT coalesce(sum(length::BIGINT)*4,0) FROM unnest(old_block.text_lengths) s(length));
 IF pg_column_size(old_block)>8388608 OR scratch_bytes>8388608 THEN
  RAISE EXCEPTION 'retained lexical payload/scratch admission exceeded' USING ERRCODE='54000';
 END IF;
 IF old_block.source_id<>document.source_id OR old_block.artifact_version_id<>document.artifact_version_id
    OR sha256(convert_to(to_jsonb(old_block)::TEXT,'UTF8')) IS DISTINCT FROM p_original_payload
    OR EXISTS(SELECT 1 FROM storage_v2_derived_lexical_block WHERE occurrence_id=p_occurrence AND block_order=p_block)
    OR EXISTS(SELECT 1 FROM storage_v2_lexical_segment WHERE occurrence_id=p_occurrence
                AND segment_order=ANY(old_block.segment_orders)) THEN
  RAISE EXCEPTION 'exact unconverted retained lexical block required';
 END IF;
 SELECT search_text INTO STRICT body FROM storage_v2_search_document WHERE id=document.document_id;
 bytes:=convert_to(body,'UTF8');body_length:=octet_length(bytes);body_characters:=char_length(body);
 IF EXISTS(SELECT 1 FROM unnest(old_block.text_starts,old_block.text_lengths) s(start,length)
    WHERE start<1 OR length<1 OR start::BIGINT+length-1>body_characters) THEN
  RAISE EXCEPTION 'retained lexical character boundaries differ';
 END IF;
 -- Walk disjoint bounded UTF8 windows once. Never rescan global character
 -- prefixes for each segment. The map contains at most 128 boundaries.
 window_bytes:=0;window_characters:=0;v_utf8_window:='';
 FOR boundary IN SELECT DISTINCT character_boundary FROM (
   SELECT unnest(old_block.text_starts) AS character_boundary UNION ALL
   SELECT start+length FROM unnest(old_block.text_starts,old_block.text_lengths) s(start,length)
 ) positions ORDER BY character_boundary LOOP
  WHILE boundary>window_character_start+window_characters LOOP
   window_byte_start:=window_byte_start+window_bytes;
   window_character_start:=window_character_start+window_characters;
   window_bytes:=LEAST(32768,body_length-window_byte_start+1);
   WHILE window_bytes>0 AND window_byte_start+window_bytes<=body_length
     AND get_byte(bytes,window_byte_start+window_bytes-1) BETWEEN 128 AND 191 LOOP
    window_bytes:=window_bytes-1;
   END LOOP;
   IF window_bytes<=0 THEN RAISE EXCEPTION 'canonical UTF8 boundary walk is incomplete'; END IF;
   v_utf8_window:=convert_from(substring(bytes FROM window_byte_start FOR window_bytes),'UTF8');
   window_characters:=char_length(v_utf8_window);
  END LOOP;
  anchors:=anchors||jsonb_build_object(boundary::TEXT,
    window_byte_start+octet_length(substring(v_utf8_window FROM 1 FOR boundary-window_character_start)));
 END LOOP;
 SELECT array_agg((anchors->>start::TEXT)::INTEGER ORDER BY ordinal),
        array_agg((anchors->>(start+length)::TEXT)::INTEGER-(anchors->>start::TEXT)::INTEGER ORDER BY ordinal)
  INTO byte_starts,byte_lengths FROM unnest(old_block.text_starts,old_block.text_lengths)
    WITH ORDINALITY s(start,length,ordinal);
 WITH pieces AS MATERIALIZED (
  SELECT ordinal,convert_from(substring(bytes FROM start FOR length),'UTF8') AS text,prefix,kind
   FROM unnest(byte_starts,byte_lengths,old_block.context_prefixes,old_block.chunk_types)
    WITH ORDINALITY s(start,length,prefix,kind,ordinal)
 ) SELECT array_agg(sha256(convert_to(text,'UTF8')) ORDER BY ordinal),
          array_agg(setweight(to_tsvector('simple',text COLLATE "default"),'A')
            ||setweight(to_tsvector('simple',prefix),'B')||setweight(to_tsvector('simple',kind),'C') ORDER BY ordinal)
   INTO hashes,vectors FROM pieces;
 IF hashes IS DISTINCT FROM old_block.text_hashes OR vectors IS DISTINCT FROM old_block.fts_vectors
    OR storage_v2_lexical_block_fingerprints(vectors) IS DISTINCT FROM old_block.fingerprints THEN
  RAISE EXCEPTION 'retained lexical vectors or canonical hashes differ';
 END IF;
 SELECT array_agg(storage_v2_lexical_segment_mask(storage_v2_posting_fingerprints(tsvector_to_array(value))) ORDER BY ordinal)
  INTO masks FROM unnest(old_block.fts_vectors) WITH ORDINALITY vector(value,ordinal);
 SELECT cache.terms,cache.ordinals INTO first_terms,first_ordinals FROM storage_v2_lexical_first_term_cache(old_block.fts_vectors) cache;
 INSERT INTO storage_v2_derived_lexical_block(occurrence_id,source_id,artifact_version_id,block_order,
  segment_orders,text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,fingerprints,
  text_byte_starts,text_byte_lengths,segment_masks,first_terms,first_term_ordinals)
 VALUES(old_block.occurrence_id,old_block.source_id,old_block.artifact_version_id,old_block.block_order,
  old_block.segment_orders,old_block.text_starts,old_block.text_lengths,old_block.text_hashes,
  old_block.context_prefixes,old_block.chunk_types,old_block.fingerprints,byte_starts,byte_lengths,masks,first_terms,first_ordinals)
 RETURNING * INTO new_block;
 INSERT INTO storage_v2_lexical_conversion_intent VALUES(p_occurrence,p_block,pg_current_xact_id(),
  p_original_payload,sha256(convert_to(to_jsonb(new_block)::TEXT,'UTF8')));
 DELETE FROM storage_v2_compact_lexical_block WHERE occurrence_id=p_occurrence AND block_order=p_block;
 DELETE FROM storage_v2_lexical_conversion_intent WHERE occurrence_id=p_occurrence AND block_order=p_block;
 INSERT INTO storage_v2_lexical_conversion_receipt VALUES(p_occurrence,p_block,p_operation,p_manifest,
  old_block.source_id,old_block.artifact_version_id,document.document_id,p_materialization,p_original_payload,
  sha256(convert_to(to_jsonb(new_block)::TEXT,'UTF8')),body_length,cardinality(old_block.segment_orders),
  pg_column_size(old_block),pg_column_size(new_block)) RETURNING * INTO receipt;
 RETURN QUERY SELECT 'CONVERTED'::TEXT,receipt.segment_count,receipt.original_datum_bytes,receipt.derived_datum_bytes;
END $$;
REVOKE ALL ON FUNCTION storage_v2_convert_retained_lexical_block(UUID,BYTEA,BIGINT,BIGINT,BYTEA,BYTEA,BIGINT,INTEGER,BIGINT,BYTEA) FROM PUBLIC,mainrag;

-- Pin every actual physical consumer, verifier and identity/authority helper.
-- New migration installation is reviewed by its exact package; later drift
-- fails closed before any conversion. No reader definition is rewritten.
INSERT INTO storage_v2_lexical_conversion_function_contract
 SELECT oid::REGPROCEDURE::TEXT,sha256(convert_to(pg_get_functiondef(oid),'UTF8')),proowner,
        sha256(convert_to(coalesce(proacl::TEXT,''),'UTF8'))
 FROM pg_proc WHERE pronamespace='public'::REGNAMESPACE AND prokind='f' AND (
  prosrc ~ '\mstorage_v2_(compact_lexical_block|derived_lexical_block|lexical_block_all|lexical_segment_all)\M'
  OR proname IN ('storage_v2_verify_generation','storage_v2_requalify_generation','storage_v2_verify_lexical_segments',
   'storage_v2_verify_lexical_segment_page','storage_v2_verify_derived_lexical_masks','storage_v2_derived_lexical_vectors',
   'storage_v2_lexical_block_fingerprints','storage_v2_lexical_first_term_cache','storage_v2_lexical_segment_mask',
   'storage_v2_posting_fingerprint','storage_v2_posting_fingerprints','storage_v2_can_access_source',
   'storage_v2_reject_document_mutation','storage_v2_materialize_reader_metadata','storage_v2_invalidate_reader_metadata',
   'storage_v2_guard_converted_lexical_delete','storage_v2_require_lexical_conversion_operator',
   'storage_v2_convert_retained_lexical_block'));
INSERT INTO storage_v2_lexical_conversion_catalog_contract
 SELECT oid,storage_v2_posting_conversion_relation_sha256(oid::REGCLASS) FROM pg_class WHERE oid IN (
  'storage_v2_compact_lexical_block'::REGCLASS,'storage_v2_derived_lexical_block'::REGCLASS,
  'storage_v2_lexical_segment'::REGCLASS,'storage_v2_lexical_block_all'::REGCLASS,'storage_v2_lexical_segment_all'::REGCLASS,
  'occurrence'::REGCLASS,'artifact_version'::REGCLASS,'storage_v2_search_document'::REGCLASS,
  'storage_v2_search_view_document'::REGCLASS,'logical_source'::REGCLASS,'source_generation'::REGCLASS);
INSERT INTO storage_v2_lexical_conversion_contract_seal
 SELECT TRUE,(SELECT count(*)::INTEGER FROM storage_v2_lexical_conversion_function_contract),
   (SELECT count(*)::INTEGER FROM storage_v2_lexical_conversion_catalog_contract),
   sha256(convert_to(jsonb_build_object(
    'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),
    'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'));
COMMIT;
