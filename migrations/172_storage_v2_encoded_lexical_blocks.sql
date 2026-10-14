-- Optional lossless block-local vector storage. Existing row types and audit
-- payloads are unchanged. This migration creates only empty new indexes.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';
SELECT storage_v2_require_lexical_conversion_operator();
DO $writer_boundary$
BEGIN
 LOCK TABLE storage_v2_ingest_run,source_generation,logical_source,occurrence IN SHARE MODE NOWAIT;
 LOCK TABLE storage_v2_compact_lexical_block,storage_v2_derived_lexical_block,
  storage_v2_lexical_segment IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 IF EXISTS(SELECT 1 FROM pg_locks l JOIN logical_source s ON
   l.classid::BIGINT=((hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)>>32)&4294967295)
   AND l.objid::BIGINT=(hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)&4294967295)
   WHERE l.locktype='advisory' AND l.granted AND l.objsubid=1 AND l.pid<>pg_backend_pid()) THEN
  RAISE EXCEPTION 'encoded lexical installation requires the quiescent source writer boundary';
 END IF;
END $writer_boundary$;

DO $codec_predecessor$
DECLARE helper RECORD;
BEGIN
 IF getdatabaseencoding()<>'UTF8' THEN RAISE EXCEPTION 'encoded lexical requires UTF8 database'; END IF;
 FOR helper IN SELECT * FROM (VALUES
  ('storage_v2_lexical_codec_u32(bytea,integer)','79d2bebf98b237f57585b4a489f0cc0e25a56ac82a9f7774c7f91e6098dfa249'),
  ('storage_v2_encode_lexical_vectors(tsvector[])','abf0d26513f1be013996a14de06f8a8748f172f68db812b37c3c078c0a14424c'),
  ('storage_v2_decode_lexical_vectors(bytea)','055f576dcf2e10e0b462b18927bec9e8a735ba7bd8bff224eaedb8037d51d327'),
  ('storage_v2_lexical_codec_exact_lexemes(bytea)','8770942280a8e0c638ddaf44167a5887e9dc810356a5c35b3422c292d224e92a')
 ) expected(signature,body_sha256) LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_proc p WHERE p.oid=helper.signature::REGPROCEDURE
   AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')=helper.body_sha256
   AND p.proowner='mainrag_v2_lexical_rank_owner'::REGROLE
   AND p.provolatile='i' AND p.proisstrict AND p.proparallel='s'
   AND NOT p.prosecdef AND NOT p.proleakproof AND p.prosupport=0
   AND p.proconfig=ARRAY['search_path=pg_catalog, public']::TEXT[]
   AND has_function_privilege('mainrag',p.oid,'EXECUTE')
   AND has_function_privilege('mainrag_v2_frontier_owner',p.oid,'EXECUTE')
   AND has_function_privilege('mainrag_v2_presence_owner',p.oid,'EXECUTE')
   AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
    WHERE a.grantee NOT IN (p.proowner,'mainrag'::REGROLE,'mainrag_v2_frontier_owner'::REGROLE,
      'mainrag_v2_presence_owner'::REGROLE) OR a.privilege_type<>'EXECUTE'
      OR (a.is_grantable AND a.grantee<>p.proowner))) THEN
   RAISE EXCEPTION 'encoded lexical codec predecessor differs: %',helper.signature;
  END IF;
 END LOOP;
 -- Also validates the two existing union/key helper bodies and ACLs. Missing
 -- optional predecessor indexes retain fallback and are not an install error.
 PERFORM storage_v2_exact_lexeme_probes_ready();
 IF NOT EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_function_contract
  WHERE signature='storage_v2_exact_lexeme_probes_ready()') THEN
  RAISE EXCEPTION 'encoded lexical requires the original exact-index readiness contract';
 END IF;
END $codec_predecessor$;

CREATE TEMP TABLE lexical172_before_function AS
 SELECT c.*,to_jsonb(p)-'prosrc' AS authority
 FROM storage_v2_lexical_conversion_function_contract c
 JOIN pg_proc p ON p.oid=c.signature::REGPROCEDURE;
CREATE TEMP TABLE lexical172_before_relation AS
 SELECT * FROM storage_v2_lexical_conversion_catalog_contract;
CREATE TEMP TABLE lexical172_before_occurrence_trigger AS
 SELECT oid,to_jsonb(t) AS identity FROM pg_trigger t WHERE tgrelid='occurrence'::REGCLASS;
CREATE TEMP TABLE lexical172_before_view AS
 SELECT pg_get_viewdef('storage_v2_lexical_block_all'::REGCLASS,true) AS definition;

-- Pin completeness is separate from pin correctness: a new unreviewed reader
-- outside the existing lexical contract must not silently keep physical-only semantics.
DO $consumer_inventory$
DECLARE consumer RECORD;
BEGIN
 FOR consumer IN SELECT p.oid::REGPROCEDURE AS signature FROM pg_proc p
 WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f'
 AND p.prosrc ~* '\m(FROM|JOIN)[[:space:]]+(public\.)?storage_v2_compact_lexical_block\M'
 AND NOT EXISTS(SELECT 1 FROM lexical172_before_function c WHERE c.signature=p.oid::REGPROCEDURE::TEXT) LOOP
  RAISE EXCEPTION 'encoded lexical physical consumer is outside predecessor contract: %',consumer.signature;
 END LOOP;
END $consumer_inventory$;

-- The physical destination has its own tuple type. Never add columns to the
-- compact/derived predecessors: to_jsonb(original_row) is a retained witness.
CREATE TABLE storage_v2_encoded_lexical_block (
 occurrence_id BIGINT NOT NULL,source_id BIGINT NOT NULL,
 artifact_version_id BIGINT NOT NULL,block_order BIGINT NOT NULL,
 segment_orders BIGINT[] NOT NULL,text_starts INTEGER[] NOT NULL,
 text_lengths INTEGER[] NOT NULL,text_hashes BYTEA[] NOT NULL,
 context_prefixes TEXT[] NOT NULL,chunk_types TEXT[] NOT NULL,
 fts_payload BYTEA NOT NULL CHECK(octet_length(fts_payload) BETWEEN 12 AND 8388608),
 fingerprints INTEGER[] NOT NULL,
 PRIMARY KEY(occurrence_id,block_order),
 FOREIGN KEY(occurrence_id,source_id,artifact_version_id)
  REFERENCES occurrence(id,source_id,artifact_version_id) ON DELETE RESTRICT,
 CHECK(storage_v2_lexical_block_orders_valid(block_order,segment_orders)),
 CHECK(cardinality(storage_v2_decode_lexical_vectors(fts_payload))=cardinality(segment_orders)),
 CHECK(storage_v2_lexical_block_fingerprints(storage_v2_decode_lexical_vectors(fts_payload))=fingerprints)
);
ALTER TABLE storage_v2_encoded_lexical_block OWNER TO mainrag_v2_frontier_owner;
ALTER TABLE storage_v2_encoded_lexical_block ALTER COLUMN fts_payload SET COMPRESSION lz4;
ALTER TABLE storage_v2_encoded_lexical_block ALTER COLUMN text_hashes SET COMPRESSION lz4;
ALTER TABLE storage_v2_encoded_lexical_block ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_encoded_lexical_block FORCE ROW LEVEL SECURITY;
CREATE POLICY storage_v2_encoded_lexical_source ON storage_v2_encoded_lexical_block
 USING(storage_v2_can_access_source(source_id,'read'))
 WITH CHECK(storage_v2_can_access_source(source_id,'write'));
CREATE POLICY storage_v2_encoded_lexical_rank_reader ON storage_v2_encoded_lexical_block
 FOR SELECT TO mainrag_v2_lexical_rank_owner USING(TRUE);
CREATE POLICY storage_v2_encoded_lexical_presence_reader ON storage_v2_encoded_lexical_block
 FOR SELECT TO mainrag_v2_presence_owner USING(TRUE);
REVOKE ALL ON storage_v2_encoded_lexical_block FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_encoded_lexical_block
 TO mainrag,mainrag_v2_lexical_rank_owner,mainrag_v2_presence_owner;
CREATE INDEX idx_storage_v2_encoded_lexical_source
 ON storage_v2_encoded_lexical_block(source_id,occurrence_id);
CREATE INDEX idx_storage_v2_encoded_lexical_fingerprint
 ON storage_v2_encoded_lexical_block USING GIN(fingerprints) WITH(fastupdate=off);
CREATE INDEX idx_storage_v2_encoded_lexical_exact_lexemes
 ON storage_v2_encoded_lexical_block USING GIN(
 storage_v2_compact_lexical_exact_lexemes(storage_v2_decode_lexical_vectors(fts_payload))) WITH(fastupdate=off);
CREATE FUNCTION storage_v2_guard_encoded_lexical_mutation() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
BEGIN
 IF TG_OP='DELETE' AND inet_client_addr() IS NULL
  AND current_setting('role') IN ('none',session_user)
  AND (SELECT rolsuper FROM pg_roles WHERE rolname=session_user) AND storage_v2_is_admin()
  AND EXISTS(SELECT 1 FROM storage_v2_encoded_lexical_intent i
   JOIN storage_v2_compact_lexical_block c USING(occurrence_id,block_order)
   WHERE i.occurrence_id=OLD.occurrence_id AND i.block_order=OLD.block_order
    AND i.transaction_id=pg_current_xact_id() AND i.is_restore
    AND i.encoded_payload_sha256=sha256(convert_to(to_jsonb(OLD)::TEXT,'UTF8'))
    AND i.original_payload_sha256=sha256(convert_to(to_jsonb(c)::TEXT,'UTF8')))
  THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'storage-v2 retrieval projections are immutable' USING ERRCODE='42501';
END $$;
REVOKE ALL ON FUNCTION storage_v2_guard_encoded_lexical_mutation() FROM PUBLIC,mainrag;
CREATE TRIGGER storage_v2_encoded_lexical_immutable BEFORE UPDATE OR DELETE
 ON storage_v2_encoded_lexical_block FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_encoded_lexical_mutation();

CREATE VIEW storage_v2_compact_lexical_block_all WITH(security_invoker=true) AS
 SELECT occurrence_id,source_id,artifact_version_id,block_order,segment_orders,
 text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,fts_vectors,fingerprints
 FROM storage_v2_compact_lexical_block
 UNION ALL
 SELECT occurrence_id,source_id,artifact_version_id,block_order,segment_orders,
 text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,
 storage_v2_decode_lexical_vectors(fts_payload),fingerprints FROM storage_v2_encoded_lexical_block;
ALTER VIEW storage_v2_compact_lexical_block_all OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON storage_v2_compact_lexical_block_all FROM PUBLIC,mainrag;
GRANT SELECT ON storage_v2_compact_lexical_block_all
 TO mainrag,mainrag_v2_lexical_rank_owner,mainrag_v2_presence_owner;

-- Private ledger and transient intent deliberately have no foreign keys.
-- Historic proof must not become a new GC retention root.
CREATE TABLE storage_v2_encoded_lexical_receipt (
 occurrence_id BIGINT NOT NULL,block_order BIGINT NOT NULL,
 operation_id UUID NOT NULL,manifest_sha256 BYTEA NOT NULL CHECK(octet_length(manifest_sha256)=32),
 source_id BIGINT NOT NULL,artifact_version_id BIGINT NOT NULL,document_id BIGINT NOT NULL,
 materialization_sha256 BYTEA NOT NULL CHECK(octet_length(materialization_sha256)=32),
 original_payload_sha256 BYTEA NOT NULL CHECK(octet_length(original_payload_sha256)=32),
 encoded_payload_sha256 BYTEA NOT NULL CHECK(octet_length(encoded_payload_sha256)=32),
 original_stored_bytes BIGINT NOT NULL,encoded_stored_bytes BIGINT NOT NULL,
 audit_bytes BIGINT NOT NULL,index_growth_bytes BIGINT NOT NULL,
 PRIMARY KEY(occurrence_id,block_order),
 CHECK(encoded_stored_bytes+audit_bytes+index_growth_bytes<original_stored_bytes)
);
CREATE TABLE storage_v2_encoded_lexical_intent (
 occurrence_id BIGINT NOT NULL,block_order BIGINT NOT NULL,transaction_id XID8 NOT NULL,
 original_payload_sha256 BYTEA NOT NULL,encoded_payload_sha256 BYTEA NOT NULL,
 is_restore BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(occurrence_id,block_order)
);
REVOKE ALL ON storage_v2_encoded_lexical_receipt,storage_v2_encoded_lexical_intent
 FROM PUBLIC,mainrag,mainrag_v2_frontier_owner,mainrag_v2_lexical_rank_owner,mainrag_v2_presence_owner;
-- Finalize only the actual index charge while the exact conversion intent
-- still owns this transaction. Both ledger tuple versions are costed below.
CREATE FUNCTION storage_v2_encoded_lexical_receipt_guard() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
BEGIN
 IF TG_OP='UPDATE' AND inet_client_addr() IS NULL
  AND current_setting('role') IN ('none',session_user)
  AND (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
  AND storage_v2_is_admin() AND OLD.index_growth_bytes=0 AND NEW.index_growth_bytes>=0
  AND (to_jsonb(OLD)-'index_growth_bytes')=(to_jsonb(NEW)-'index_growth_bytes')
  AND EXISTS(SELECT 1 FROM storage_v2_encoded_lexical_intent i
   WHERE i.occurrence_id=OLD.occurrence_id AND i.block_order=OLD.block_order
   AND i.transaction_id=pg_current_xact_id() AND NOT i.is_restore
   AND i.original_payload_sha256=OLD.original_payload_sha256
   AND i.encoded_payload_sha256=OLD.encoded_payload_sha256) THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'encoded lexical audit is immutable' USING ERRCODE='42501';
END $$;
REVOKE ALL ON FUNCTION storage_v2_encoded_lexical_receipt_guard() FROM PUBLIC,mainrag;
CREATE TRIGGER storage_v2_encoded_lexical_receipt_immutable BEFORE UPDATE OR DELETE
 ON storage_v2_encoded_lexical_receipt FOR EACH ROW EXECUTE FUNCTION storage_v2_encoded_lexical_receipt_guard();

CREATE TABLE storage_v2_encoded_lexical_restore_receipt (
 occurrence_id BIGINT NOT NULL,block_order BIGINT NOT NULL,operation_id UUID NOT NULL,
 manifest_sha256 BYTEA NOT NULL CHECK(octet_length(manifest_sha256)=32),
 original_payload_sha256 BYTEA NOT NULL CHECK(octet_length(original_payload_sha256)=32),
 encoded_payload_sha256 BYTEA NOT NULL CHECK(octet_length(encoded_payload_sha256)=32),
 restored_stored_bytes BIGINT NOT NULL,PRIMARY KEY(occurrence_id,block_order)
);
REVOKE ALL ON storage_v2_encoded_lexical_restore_receipt
 FROM PUBLIC,mainrag,mainrag_v2_frontier_owner,mainrag_v2_lexical_rank_owner,mainrag_v2_presence_owner;
CREATE TRIGGER storage_v2_encoded_lexical_restore_receipt_immutable BEFORE UPDATE OR DELETE
 ON storage_v2_encoded_lexical_restore_receipt FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();

CREATE FUNCTION storage_v2_encoded_lexical_insert_guard() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE intent storage_v2_encoded_lexical_intent;
BEGIN
 IF inet_client_addr() IS NOT NULL OR current_setting('role') NOT IN ('none',session_user)
  OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
  OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'encoded lexical insertion requires named administrative transition' USING ERRCODE='42501';
 END IF;
 SELECT * INTO intent FROM storage_v2_encoded_lexical_intent
 WHERE occurrence_id=NEW.occurrence_id AND block_order=NEW.block_order;
 IF intent.transaction_id IS DISTINCT FROM pg_current_xact_id() OR intent.is_restore
  OR intent.encoded_payload_sha256 IS DISTINCT FROM sha256(convert_to(to_jsonb(NEW)::TEXT,'UTF8'))
  OR NOT EXISTS(SELECT 1 FROM storage_v2_compact_lexical_block c
   WHERE c.occurrence_id=NEW.occurrence_id AND c.block_order=NEW.block_order
   AND sha256(convert_to(to_jsonb(c)::TEXT,'UTF8'))=intent.original_payload_sha256) THEN
  RAISE EXCEPTION 'encoded lexical insertion lacks exact same-transaction original';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION storage_v2_encoded_lexical_insert_guard() FROM PUBLIC,mainrag;
CREATE TRIGGER storage_v2_encoded_lexical_insert BEFORE INSERT ON storage_v2_encoded_lexical_block
 FOR EACH ROW EXECUTE FUNCTION storage_v2_encoded_lexical_insert_guard();

-- Base-table INSERT remains the fresh writer. Exact replay against an encoded
-- owner is a no-op, followed by the existing complete collision check through
-- the logical view. Different values cannot recreate a second owner.
CREATE FUNCTION storage_v2_compact_lexical_encoded_replay_guard() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE existing storage_v2_compact_lexical_block;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('mainrag.lexical-segment:'||NEW.occurrence_id::TEXT,0));
 SELECT c.* INTO existing FROM storage_v2_compact_lexical_block_all c
 WHERE c.occurrence_id=NEW.occurrence_id AND c.block_order=NEW.block_order
 AND EXISTS(SELECT 1 FROM storage_v2_encoded_lexical_block e
  WHERE e.occurrence_id=NEW.occurrence_id AND e.block_order=NEW.block_order);
 IF FOUND THEN
  IF (existing.source_id,existing.artifact_version_id,existing.segment_orders,
      existing.text_starts,existing.text_lengths,existing.text_hashes,
      existing.context_prefixes,existing.chunk_types,existing.fts_vectors) IS DISTINCT FROM
     (NEW.source_id,NEW.artifact_version_id,NEW.segment_orders,NEW.text_starts,
      NEW.text_lengths,NEW.text_hashes,NEW.context_prefixes,NEW.chunk_types,NEW.fts_vectors)
   OR storage_v2_can_access_source(NEW.source_id,'write') IS DISTINCT FROM TRUE THEN
   RAISE EXCEPTION 'encoded lexical replay identity collision' USING ERRCODE='42501';
  END IF;
  IF EXISTS(SELECT 1 FROM storage_v2_encoded_lexical_intent i
   WHERE i.occurrence_id=NEW.occurrence_id AND i.block_order=NEW.block_order
    AND i.transaction_id=pg_current_xact_id() AND i.is_restore
    AND i.original_payload_sha256=sha256(convert_to((to_jsonb(NEW)||jsonb_build_object(
     'fingerprints',storage_v2_lexical_block_fingerprints(NEW.fts_vectors)))::TEXT,'UTF8'))) THEN
   RETURN NEW;
  END IF;
  RETURN NULL;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION storage_v2_compact_lexical_encoded_replay_guard() FROM PUBLIC,mainrag;
CREATE TRIGGER storage_v2_compact_lexical_encoded_replay BEFORE INSERT ON storage_v2_compact_lexical_block
 FOR EACH ROW EXECUTE FUNCTION storage_v2_compact_lexical_encoded_replay_guard();

-- Only audited read clauses change. Old physical tuple declarations, INSERT,
-- DELETE, immutable guards and the old compact-to-derived converter stay.
DO $readers$
DECLARE routine RECORD; definition TEXT; patched TEXT; marker TEXT; changed INTEGER;
 allowed TEXT[]:=ARRAY[
 'storage_v2_put_lexical_segments_located(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[])',
 'storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)',
 'storage_v2_verify_lexical_segment_page(bigint,bigint,integer)',
 'storage_v2_put_lexical_segments_document_private(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[],bigint,bytea,integer,integer)'];
BEGIN
 FOR routine IN SELECT p.*,c.definition_sha256 AS predecessor FROM pg_proc p
 JOIN lexical172_before_function c ON c.signature=p.oid::REGPROCEDURE::TEXT
 WHERE p.pronamespace='public'::REGNAMESPACE
 AND p.prosrc ~* '\m(FROM|JOIN)[[:space:]]+(public\.)?storage_v2_compact_lexical_block\M' LOOP
  IF routine.oid='storage_v2_convert_retained_lexical_block(uuid,bytea,bigint,bigint,bytea,bytea,bigint,integer,bigint,bytea)'::REGPROCEDURE
   THEN CONTINUE; END IF;
  IF NOT routine.oid::REGPROCEDURE::TEXT=ANY(allowed) THEN
   RAISE EXCEPTION 'encoded lexical has an unreviewed physical reader: %',routine.oid::REGPROCEDURE;
  END IF;
  -- Only these four pinned routines have physical read clauses at170.
  -- INSERT targets stay physical; any compact DELETE/UPDATE/MERGE would make
  -- the generic read-marker replacement unsafe, so fail instead of patching it.
  IF routine.prosrc ~* '\m(DELETE[[:space:]]+FROM|UPDATE|MERGE[[:space:]]+INTO)[[:space:]]+(public\.)?storage_v2_compact_lexical_block\M' THEN
   RAISE EXCEPTION 'encoded lexical audited reader unexpectedly mutates original blocks';
  END IF;
  definition:=pg_get_functiondef(routine.oid);
  IF sha256(convert_to(definition,'UTF8')) IS DISTINCT FROM routine.predecessor THEN
   RAISE EXCEPTION 'encoded lexical reader changed after predecessor inventory';
  END IF;
  patched:=regexp_replace(definition,'\m(FROM|JOIN)([[:space:]]+)(public\.)?storage_v2_compact_lexical_block\M',
   '\1\2public.storage_v2_compact_lexical_block_all','gi');
  IF patched=definition THEN RAISE EXCEPTION 'encoded lexical reader marker missing'; END IF;
  EXECUTE patched;
  IF (SELECT to_jsonb(p)-'prosrc' FROM pg_proc p WHERE p.oid=routine.oid)
    IS DISTINCT FROM (SELECT authority FROM lexical172_before_function WHERE signature=routine.oid::REGPROCEDURE::TEXT) THEN
   RAISE EXCEPTION 'encoded lexical reader authority changed';
  END IF;
  UPDATE storage_v2_lexical_conversion_function_contract SET definition_sha256=
   sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8'))
   WHERE signature=routine.oid::REGPROCEDURE::TEXT AND definition_sha256=routine.predecessor;
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>1 THEN RAISE EXCEPTION 'encoded lexical reader predecessor CAS failed'; END IF;
 END LOOP;
 -- Existing union shape, owner, ACL and security_invoker remain identical.
 definition:=pg_get_viewdef('storage_v2_lexical_block_all'::REGCLASS,true);
 marker:='FROM storage_v2_compact_lexical_block';
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'encoded lexical block view predecessor differs'; END IF;
 EXECUTE 'CREATE OR REPLACE VIEW public.storage_v2_lexical_block_all WITH(security_invoker=true) AS '
   ||replace(definition,marker,'FROM storage_v2_compact_lexical_block_all AS storage_v2_compact_lexical_block');
 -- The original derived transition is unchanged after this additional branch.
 definition:=pg_get_functiondef('storage_v2_guard_converted_lexical_delete()'::REGPROCEDURE);
 marker:=' SELECT * INTO intent FROM storage_v2_lexical_conversion_intent';
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'encoded lexical delete predecessor differs'; END IF;
 patched:=replace(definition,marker,$branch$
 IF EXISTS(SELECT 1 FROM storage_v2_encoded_lexical_intent i
  JOIN storage_v2_encoded_lexical_block e USING(occurrence_id,block_order)
  WHERE i.occurrence_id=OLD.occurrence_id AND i.block_order=OLD.block_order
   AND i.transaction_id=pg_current_xact_id() AND NOT i.is_restore
   AND i.original_payload_sha256=sha256(convert_to(to_jsonb(OLD)::TEXT,'UTF8'))
   AND i.encoded_payload_sha256=sha256(convert_to(to_jsonb(e)::TEXT,'UTF8'))
   AND storage_v2_decode_lexical_vectors(e.fts_payload)=OLD.fts_vectors
   AND (e.source_id,e.artifact_version_id,e.segment_orders,e.text_starts,e.text_lengths,
        e.text_hashes,e.context_prefixes,e.chunk_types,e.fingerprints) IS NOT DISTINCT FROM
       (OLD.source_id,OLD.artifact_version_id,OLD.segment_orders,OLD.text_starts,OLD.text_lengths,
        OLD.text_hashes,OLD.context_prefixes,OLD.chunk_types,OLD.fingerprints)) THEN RETURN OLD; END IF;
$branch$||marker);
 EXECUTE patched;
 UPDATE storage_v2_lexical_conversion_function_contract SET definition_sha256=
 sha256(convert_to(pg_get_functiondef(signature::REGPROCEDURE),'UTF8'))
 WHERE signature='storage_v2_guard_converted_lexical_delete()' AND definition_sha256=
 (SELECT definition_sha256 FROM lexical172_before_function WHERE signature='storage_v2_guard_converted_lexical_delete()');
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'encoded lexical delete guard predecessor CAS failed'; END IF;
END $readers$;

CREATE FUNCTION storage_v2_require_encoded_lexical_quiescence(
 p_statement_budget_ms INTEGER,p_retained_run BIGINT,p_retained_run_sha256 BYTEA,p_occurrence BIGINT
) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE statement_ms NUMERIC;lock_ms NUMERIC;
BEGIN
 PERFORM storage_v2_require_lexical_conversion_operator();
 -- Native tsvectorsend uses client encoding. Prearm one fixed wire encoding
 -- for both sides of the equality; the MLV1 codec itself remains independent.
 IF current_setting('client_encoding')<>'UTF8' THEN
  RAISE EXCEPTION 'encoded lexical native-wire proof requires prearmed UTF8 client encoding' USING ERRCODE='22023';
 END IF;
 IF p_statement_budget_ms IS NULL OR p_statement_budget_ms NOT BETWEEN 100 AND 30000
  OR p_occurrence IS NULL OR p_occurrence<=0
  OR (p_retained_run IS NULL) IS DISTINCT FROM (p_retained_run_sha256 IS NULL)
  OR (p_retained_run IS NOT NULL AND (p_retained_run<=0 OR octet_length(p_retained_run_sha256) IS DISTINCT FROM 32)) THEN
  RAISE EXCEPTION 'bounded encoded lexical writer identities required';
 END IF;
 statement_ms:=extract(epoch FROM current_setting('statement_timeout')::INTERVAL)*1000;
 lock_ms:=extract(epoch FROM current_setting('lock_timeout')::INTERVAL)*1000;
 IF statement_ms<=0 OR statement_ms>p_statement_budget_ms OR lock_ms<=0 OR lock_ms>3000 THEN
  RAISE EXCEPTION 'prearmed statement/lock deadlines required';
 END IF;
 LOCK TABLE storage_v2_ingest_run,source_generation,logical_source IN SHARE MODE NOWAIT;
 LOCK TABLE storage_v2_compact_lexical_block,storage_v2_encoded_lexical_block,
  storage_v2_derived_lexical_block,storage_v2_lexical_segment,
  storage_v2_encoded_lexical_receipt,storage_v2_encoded_lexical_restore_receipt IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 LOCK TABLE occurrence,artifact_version,storage_v2_search_document,storage_v2_search_view_document
  IN SHARE MODE NOWAIT;
 IF EXISTS(SELECT 1 FROM storage_v2_ingest_run WHERE status='building'
  AND (p_retained_run IS NULL OR id<>p_retained_run))
  OR (p_retained_run IS NOT NULL AND (storage_v2_retained_posting_run_sha256(p_retained_run)
      IS DISTINCT FROM p_retained_run_sha256 OR NOT EXISTS(SELECT 1 FROM storage_v2_ingest_run
       WHERE id=p_retained_run AND status='building'))) THEN
  RAISE EXCEPTION 'unbound or changed building run prevents lexical encoding';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_locks l JOIN logical_source s ON
   l.classid::BIGINT=((hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)>>32)&4294967295)
   AND l.objid::BIGINT=(hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)&4294967295)
   WHERE l.locktype='advisory' AND l.granted AND l.objsubid=1 AND l.pid<>pg_backend_pid())
  OR EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
   AND state<>'idle' AND query ~* '(insert|update|delete|merge|copy|create[[:space:]]+index|reindex|vacuum|cluster|refresh[[:space:]]+materialized|storage_v2_materialize_reader_metadata)') THEN
  RAISE EXCEPTION 'lexical encoding requires writer-free controlled maintenance';
 END IF;
 IF NOT pg_try_advisory_xact_lock(hashtextextended('mainrag.lexical-segment:'||p_occurrence::TEXT,0)) THEN
  RAISE EXCEPTION 'lexical occurrence writer owns the boundary';
 END IF;
END $$;
REVOKE ALL ON FUNCTION storage_v2_require_encoded_lexical_quiescence(INTEGER,BIGINT,BYTEA,BIGINT)
 FROM PUBLIC,mainrag,mainrag_v2_frontier_owner,mainrag_v2_lexical_rank_owner,mainrag_v2_presence_owner;

CREATE FUNCTION storage_v2_encode_retained_lexical_block(
 p_operation UUID,p_manifest BYTEA,p_occurrence BIGINT,p_block BIGINT,
 p_original_payload BYTEA,p_materialization BYTEA,p_original_stored_bytes BIGINT,
 p_statement_budget_ms INTEGER,p_retained_run BIGINT DEFAULT NULL,p_retained_run_sha256 BYTEA DEFAULT NULL
) RETURNS TABLE(disposition TEXT,original_stored_bytes BIGINT,encoded_stored_bytes BIGINT,
 audit_bytes BIGINT,index_growth_bytes BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $convert$
DECLARE original storage_v2_compact_lexical_block; destination storage_v2_encoded_lexical_block;
 receipt storage_v2_encoded_lexical_receipt; proposed storage_v2_encoded_lexical_receipt;
 document RECORD; statement_ms NUMERIC;lock_ms NUMERIC; stored_before BIGINT;
 index_before BIGINT;index_after BIGINT;payload BYTEA;vectors TSVECTOR[];expanded_bytes BIGINT;
BEGIN
 PERFORM storage_v2_require_lexical_conversion_operator();
 IF p_operation IS NULL OR octet_length(p_manifest) IS DISTINCT FROM 32
  OR p_occurrence IS NULL OR p_occurrence<=0 OR p_block IS NULL OR p_block<0
  OR octet_length(p_original_payload) IS DISTINCT FROM 32 OR octet_length(p_materialization) IS DISTINCT FROM 32
  OR p_original_stored_bytes IS NULL OR p_original_stored_bytes<=0
  OR p_statement_budget_ms IS NULL OR p_statement_budget_ms NOT BETWEEN 100 AND 30000
  OR (p_retained_run IS NULL) IS DISTINCT FROM (p_retained_run_sha256 IS NULL)
  OR (p_retained_run IS NOT NULL AND (p_retained_run<=0 OR octet_length(p_retained_run_sha256) IS DISTINCT FROM 32)) THEN
  RAISE EXCEPTION 'exact bounded named lexical codec identities required';
 END IF;
 PERFORM storage_v2_require_encoded_lexical_quiescence(
  p_statement_budget_ms,p_retained_run,p_retained_run_sha256,p_occurrence);
 SELECT o.source_id,o.artifact_version_id,d.id AS document_id,d.materialization_sha256 INTO document
 FROM occurrence o JOIN artifact_version a ON a.id=o.artifact_version_id AND a.source_id=o.source_id
 JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0
 JOIN storage_v2_search_document d ON d.id=b.document_id AND d.component_kind='node'
  AND d.node_id=a.content_root_node_id WHERE o.id=p_occurrence;
 IF NOT FOUND OR document.materialization_sha256 IS DISTINCT FROM p_materialization
  OR storage_v2_can_access_source(document.source_id,'write') IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'exact authorized immutable lexical owner required';
 END IF;
 SELECT r.* INTO receipt FROM storage_v2_encoded_lexical_receipt r
 WHERE r.occurrence_id=p_occurrence AND r.block_order=p_block;
 IF FOUND THEN
  SELECT * INTO destination FROM storage_v2_encoded_lexical_block
   WHERE occurrence_id=p_occurrence AND block_order=p_block;
  IF NOT FOUND OR EXISTS(SELECT 1 FROM storage_v2_compact_lexical_block
    WHERE occurrence_id=p_occurrence AND block_order=p_block)
   OR receipt.operation_id IS DISTINCT FROM p_operation OR receipt.manifest_sha256 IS DISTINCT FROM p_manifest
   OR receipt.original_payload_sha256 IS DISTINCT FROM p_original_payload
   OR receipt.materialization_sha256 IS DISTINCT FROM p_materialization
   OR receipt.original_stored_bytes IS DISTINCT FROM p_original_stored_bytes
   OR (receipt.source_id,receipt.artifact_version_id,receipt.document_id) IS DISTINCT FROM
      (document.source_id,document.artifact_version_id,document.document_id)
   OR receipt.encoded_payload_sha256 IS DISTINCT FROM sha256(convert_to(to_jsonb(destination)::TEXT,'UTF8')) THEN
   RAISE EXCEPTION 'lexical encoding replay requires exact destination and original manifest';
  END IF;
  RETURN QUERY SELECT 'REPLAY'::TEXT,receipt.original_stored_bytes,receipt.encoded_stored_bytes,
   receipt.audit_bytes,receipt.index_growth_bytes; RETURN;
 END IF;
 -- Direct table-column size keeps each existing TOAST compression visible.
 SELECT 32::BIGINT+pg_column_size(c.segment_orders)+pg_column_size(c.text_starts)+pg_column_size(c.text_lengths)
  +pg_column_size(c.text_hashes)+pg_column_size(c.context_prefixes)+pg_column_size(c.chunk_types)
  +pg_column_size(c.fts_vectors)+pg_column_size(c.fingerprints) INTO stored_before
 FROM storage_v2_compact_lexical_block c WHERE c.occurrence_id=p_occurrence AND c.block_order=p_block;
 IF stored_before IS DISTINCT FROM p_original_stored_bytes THEN
  RAISE EXCEPTION 'original stored column costs changed';
 END IF;
 SELECT * INTO STRICT original FROM storage_v2_compact_lexical_block
 WHERE occurrence_id=p_occurrence AND block_order=p_block FOR UPDATE NOWAIT;
 -- Optional codec envelope, not a source/data limit. Refuse before the
 -- array-element expansion and before any encoder call. Historical formats
 -- with more vectors must keep their original representation unchanged.
 IF cardinality(original.fts_vectors)>64 THEN
  RETURN QUERY SELECT 'SKIP_CODEC_ENVELOPE'::TEXT,stored_before,NULL::BIGINT,NULL::BIGINT,NULL::BIGINT; RETURN;
 END IF;
 SELECT sum(pg_column_size(v)::BIGINT) INTO expanded_bytes FROM unnest(original.fts_vectors) v;
 expanded_bytes:=expanded_bytes+(SELECT coalesce(sum(octet_length(prefix)::BIGINT+octet_length(kind)),0)
  FROM unnest(original.context_prefixes,original.chunk_types) metadata(prefix,kind));
 -- Codec limits are optional-format admission. Larger original blocks survive.
 IF expanded_bytes>8388608 OR pg_column_size(original)>8388608 THEN
  RETURN QUERY SELECT 'SKIP_CODEC_ENVELOPE'::TEXT,stored_before,NULL::BIGINT,NULL::BIGINT,NULL::BIGINT; RETURN;
 END IF;
 IF (original.source_id,original.artifact_version_id) IS DISTINCT FROM (document.source_id,document.artifact_version_id)
  OR sha256(convert_to(to_jsonb(original)::TEXT,'UTF8')) IS DISTINCT FROM p_original_payload
  OR EXISTS(SELECT 1 FROM storage_v2_encoded_lexical_block WHERE occurrence_id=p_occurrence AND block_order=p_block)
  OR EXISTS(SELECT 1 FROM storage_v2_derived_lexical_block WHERE occurrence_id=p_occurrence AND block_order=p_block)
  OR EXISTS(SELECT 1 FROM storage_v2_lexical_segment WHERE occurrence_id=p_occurrence AND segment_order=ANY(original.segment_orders)) THEN
  RAISE EXCEPTION 'exact unconverted immutable lexical block required';
 END IF;
 BEGIN
  payload:=storage_v2_encode_lexical_vectors(original.fts_vectors);
 EXCEPTION WHEN SQLSTATE '54000' THEN
  RETURN QUERY SELECT 'SKIP_CODEC_ENVELOPE'::TEXT,stored_before,NULL::BIGINT,NULL::BIGINT,NULL::BIGINT; RETURN;
 END;
 vectors:=storage_v2_decode_lexical_vectors(payload);
 IF vectors IS DISTINCT FROM original.fts_vectors
  OR ARRAY(SELECT tsvectorsend(v) FROM unnest(vectors) WITH ORDINALITY t(v,n) ORDER BY n)
    IS DISTINCT FROM ARRAY(SELECT tsvectorsend(v) FROM unnest(original.fts_vectors) WITH ORDINALITY t(v,n) ORDER BY n)
  OR storage_v2_lexical_block_fingerprints(vectors) IS DISTINCT FROM original.fingerprints THEN
  RAISE EXCEPTION 'encoded lexical native vectors, positions, weights or fingerprints differ';
 END IF;
 destination:=(original.occurrence_id,original.source_id,original.artifact_version_id,original.block_order,
  original.segment_orders,original.text_starts,original.text_lengths,original.text_hashes,
  original.context_prefixes,original.chunk_types,payload,original.fingerprints)::storage_v2_encoded_lexical_block;
 -- A tiny original cannot cover even the fixed destination and audit.
 -- Refuse before writes rather than leaving aborted index/TOAST allocation.
 proposed.occurrence_id:=p_occurrence;proposed.block_order:=p_block;proposed.operation_id:=p_operation;
 proposed.manifest_sha256:=p_manifest;proposed.source_id:=document.source_id;
 proposed.artifact_version_id:=document.artifact_version_id;proposed.document_id:=document.document_id;
 proposed.materialization_sha256:=p_materialization;proposed.original_payload_sha256:=p_original_payload;
 proposed.encoded_payload_sha256:=sha256(convert_to(to_jsonb(destination)::TEXT,'UTF8'));
 proposed.original_stored_bytes:=stored_before;proposed.encoded_stored_bytes:=0;
 proposed.audit_bytes:=0;proposed.index_growth_bytes:=0;
 IF 32+2*pg_column_size(proposed)::BIGINT+128>=stored_before THEN
  RAISE EXCEPTION 'encoded lexical stored costs do not improve original';
 END IF;
 INSERT INTO storage_v2_encoded_lexical_intent VALUES(p_occurrence,p_block,pg_current_xact_id(),p_original_payload,
  sha256(convert_to(to_jsonb(destination)::TEXT,'UTF8')),FALSE);
 index_before:=pg_indexes_size('storage_v2_encoded_lexical_block')+pg_indexes_size('storage_v2_encoded_lexical_receipt');
 INSERT INTO storage_v2_encoded_lexical_block SELECT destination.*;
 SELECT 32::BIGINT+pg_column_size(c.segment_orders)+pg_column_size(c.text_starts)+pg_column_size(c.text_lengths)
  +pg_column_size(c.text_hashes)+pg_column_size(c.context_prefixes)+pg_column_size(c.chunk_types)
  +pg_column_size(c.fts_payload)+pg_column_size(c.fingerprints) INTO proposed.encoded_stored_bytes
 FROM storage_v2_encoded_lexical_block c WHERE c.occurrence_id=p_occurrence AND c.block_order=p_block;
 proposed.occurrence_id:=p_occurrence;proposed.block_order:=p_block;proposed.operation_id:=p_operation;
 proposed.manifest_sha256:=p_manifest;proposed.source_id:=document.source_id;
 proposed.artifact_version_id:=document.artifact_version_id;proposed.document_id:=document.document_id;
 proposed.materialization_sha256:=p_materialization;proposed.original_payload_sha256:=p_original_payload;
 proposed.encoded_payload_sha256:=sha256(convert_to(to_jsonb(destination)::TEXT,'UTF8'));
 proposed.original_stored_bytes:=stored_before;proposed.audit_bytes:=0;proposed.index_growth_bytes:=0;
 -- Cost two small ledger tuple versions and 128 bytes of alignment/page
 -- allowance. This is a datum admission, never a physical reclaim estimate.
 proposed.audit_bytes:=2*pg_column_size(proposed)::BIGINT+128;
 IF proposed.encoded_stored_bytes+proposed.audit_bytes>=stored_before THEN
  RAISE EXCEPTION 'encoded lexical stored costs do not improve original';
 END IF;
 INSERT INTO storage_v2_encoded_lexical_receipt SELECT proposed.*;
 index_after:=pg_indexes_size('storage_v2_encoded_lexical_block')+pg_indexes_size('storage_v2_encoded_lexical_receipt');
 proposed.index_growth_bytes:=greatest(index_after-index_before,0);
 UPDATE storage_v2_encoded_lexical_receipt r SET index_growth_bytes=proposed.index_growth_bytes
 WHERE r.occurrence_id=p_occurrence AND r.block_order=p_block RETURNING r.* INTO receipt;
 -- UPDATE of a non-key cost column may still grow the audit Btree. Recheck
 -- the final actual allocation and refuse the whole transaction on any drift.
 IF pg_indexes_size('storage_v2_encoded_lexical_block')+pg_indexes_size('storage_v2_encoded_lexical_receipt')
    -index_before>receipt.index_growth_bytes THEN
  RAISE EXCEPTION 'encoded lexical final audit index allocation changed';
 END IF;
 DELETE FROM storage_v2_compact_lexical_block WHERE occurrence_id=p_occurrence AND block_order=p_block;
 DELETE FROM storage_v2_encoded_lexical_intent WHERE occurrence_id=p_occurrence AND block_order=p_block;
 RETURN QUERY SELECT 'CONVERTED'::TEXT,receipt.original_stored_bytes,receipt.encoded_stored_bytes,
  receipt.audit_bytes,receipt.index_growth_bytes;
END $convert$;
REVOKE ALL ON FUNCTION storage_v2_encode_retained_lexical_block(UUID,BYTEA,BIGINT,BIGINT,BYTEA,BYTEA,BIGINT,INTEGER,BIGINT,BYTEA)
 FROM PUBLIC,mainrag,mainrag_v2_frontier_owner,mainrag_v2_lexical_rank_owner,mainrag_v2_presence_owner;

-- Inverse transition for an exact package rollback. The original forward
-- audit remains immutable; the separately named inverse audit is retained.
CREATE FUNCTION storage_v2_restore_encoded_lexical_block(
 p_operation UUID,p_manifest BYTEA,p_occurrence BIGINT,p_block BIGINT,
 p_original_payload BYTEA,p_materialization BYTEA,p_max_restore_bytes BIGINT,
 p_statement_budget_ms INTEGER,p_retained_run BIGINT DEFAULT NULL,p_retained_run_sha256 BYTEA DEFAULT NULL
) RETURNS TABLE(disposition TEXT,restored_stored_bytes BIGINT,index_growth_bytes BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE receipt storage_v2_encoded_lexical_receipt; inverse storage_v2_encoded_lexical_restore_receipt;
 original storage_v2_compact_lexical_block; encoded storage_v2_encoded_lexical_block;
 document RECORD; stored BIGINT;index_before BIGINT;growth BIGINT;
BEGIN
 PERFORM storage_v2_require_encoded_lexical_quiescence(
  p_statement_budget_ms,p_retained_run,p_retained_run_sha256,p_occurrence);
 IF p_operation IS NULL OR octet_length(p_manifest) IS DISTINCT FROM 32
  OR p_block IS NULL OR p_block<0 OR octet_length(p_original_payload) IS DISTINCT FROM 32
  OR octet_length(p_materialization) IS DISTINCT FROM 32
  OR p_max_restore_bytes IS NULL OR p_max_restore_bytes NOT BETWEEN 1 AND 33554432 THEN
  RAISE EXCEPTION 'exact named bounded lexical inverse identities required';
 END IF;
 SELECT o.source_id,o.artifact_version_id,d.id AS document_id,d.materialization_sha256 INTO document
 FROM occurrence o JOIN artifact_version a ON a.id=o.artifact_version_id AND a.source_id=o.source_id
 JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0
 JOIN storage_v2_search_document d ON d.id=b.document_id AND d.component_kind='node'
  AND d.node_id=a.content_root_node_id WHERE o.id=p_occurrence;
 SELECT r.* INTO receipt FROM storage_v2_encoded_lexical_receipt r
 WHERE r.occurrence_id=p_occurrence AND r.block_order=p_block;
 IF NOT FOUND OR document.materialization_sha256 IS DISTINCT FROM p_materialization
  OR storage_v2_can_access_source(document.source_id,'write') IS DISTINCT FROM TRUE
  OR (receipt.operation_id,receipt.manifest_sha256,receipt.original_payload_sha256,receipt.materialization_sha256)
    IS DISTINCT FROM (p_operation,p_manifest,p_original_payload,p_materialization)
  OR (receipt.source_id,receipt.artifact_version_id,receipt.document_id) IS DISTINCT FROM
     (document.source_id,document.artifact_version_id,document.document_id) THEN
  RAISE EXCEPTION 'lexical inverse requires original manifest and live immutable owner';
 END IF;
 SELECT * INTO inverse FROM storage_v2_encoded_lexical_restore_receipt
 WHERE occurrence_id=p_occurrence AND block_order=p_block;
 IF FOUND THEN
  SELECT * INTO original FROM storage_v2_compact_lexical_block
   WHERE occurrence_id=p_occurrence AND block_order=p_block;
  IF NOT FOUND OR EXISTS(SELECT 1 FROM storage_v2_encoded_lexical_block
    WHERE occurrence_id=p_occurrence AND block_order=p_block)
   OR (inverse.operation_id,inverse.manifest_sha256,inverse.original_payload_sha256,inverse.encoded_payload_sha256)
     IS DISTINCT FROM (p_operation,p_manifest,p_original_payload,receipt.encoded_payload_sha256)
   OR sha256(convert_to(to_jsonb(original)::TEXT,'UTF8')) IS DISTINCT FROM p_original_payload THEN
   RAISE EXCEPTION 'lexical inverse replay requires exact original destination';
  END IF;
  RETURN QUERY SELECT 'REPLAY_RESTORED'::TEXT,inverse.restored_stored_bytes,0::BIGINT; RETURN;
 END IF;
 SELECT * INTO STRICT encoded FROM storage_v2_encoded_lexical_block
 WHERE occurrence_id=p_occurrence AND block_order=p_block FOR UPDATE NOWAIT;
 IF sha256(convert_to(to_jsonb(encoded)::TEXT,'UTF8')) IS DISTINCT FROM receipt.encoded_payload_sha256
  OR EXISTS(SELECT 1 FROM storage_v2_compact_lexical_block WHERE occurrence_id=p_occurrence AND block_order=p_block) THEN
  RAISE EXCEPTION 'lexical inverse exact encoded payload differs';
 END IF;
 SELECT c.* INTO STRICT original FROM storage_v2_compact_lexical_block_all c
 WHERE c.occurrence_id=p_occurrence AND c.block_order=p_block;
 IF sha256(convert_to(to_jsonb(original)::TEXT,'UTF8')) IS DISTINCT FROM p_original_payload THEN
  RAISE EXCEPTION 'lexical inverse decoded original witness differs';
 END IF;
 INSERT INTO storage_v2_encoded_lexical_intent VALUES(p_occurrence,p_block,pg_current_xact_id(),
  p_original_payload,receipt.encoded_payload_sha256,TRUE);
 index_before:=pg_indexes_size('storage_v2_compact_lexical_block')+
  pg_indexes_size('storage_v2_encoded_lexical_restore_receipt');
 INSERT INTO storage_v2_compact_lexical_block(occurrence_id,source_id,artifact_version_id,block_order,
  segment_orders,text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,fts_vectors)
 VALUES(original.occurrence_id,original.source_id,original.artifact_version_id,original.block_order,
  original.segment_orders,original.text_starts,original.text_lengths,original.text_hashes,
  original.context_prefixes,original.chunk_types,original.fts_vectors);
 SELECT 32::BIGINT+pg_column_size(c.segment_orders)+pg_column_size(c.text_starts)+pg_column_size(c.text_lengths)
  +pg_column_size(c.text_hashes)+pg_column_size(c.context_prefixes)+pg_column_size(c.chunk_types)
  +pg_column_size(c.fts_vectors)+pg_column_size(c.fingerprints) INTO stored
 FROM storage_v2_compact_lexical_block c WHERE c.occurrence_id=p_occurrence AND c.block_order=p_block;
 INSERT INTO storage_v2_encoded_lexical_restore_receipt VALUES(p_occurrence,p_block,p_operation,p_manifest,
  p_original_payload,receipt.encoded_payload_sha256,stored) RETURNING * INTO inverse;
 growth:=greatest(pg_indexes_size('storage_v2_compact_lexical_block')+
  pg_indexes_size('storage_v2_encoded_lexical_restore_receipt')-index_before,0);
 IF stored+pg_column_size(inverse)::BIGINT+128+growth>p_max_restore_bytes THEN
  RAISE EXCEPTION 'lexical inverse stored/index allocation exceeded restore reservation';
 END IF;
 DELETE FROM storage_v2_encoded_lexical_block WHERE occurrence_id=p_occurrence AND block_order=p_block;
 DELETE FROM storage_v2_encoded_lexical_intent WHERE occurrence_id=p_occurrence AND block_order=p_block;
 RETURN QUERY SELECT 'RESTORED'::TEXT,stored,growth;
END $$;
REVOKE ALL ON FUNCTION storage_v2_restore_encoded_lexical_block(UUID,BYTEA,BIGINT,BIGINT,BYTEA,BYTEA,BIGINT,INTEGER,BIGINT,BYTEA)
 FROM PUBLIC,mainrag,mainrag_v2_frontier_owner,mainrag_v2_lexical_rank_owner,mainrag_v2_presence_owner;

DO $contract$
DECLARE previous RECORD; changed INTEGER; signature TEXT; relation REGCLASS;
BEGIN
 -- The new FK changes the referenced occurrence's internal RI triggers.
 -- Prove exactly those two additions, and all previous triggers unchanged,
 -- before admitting that named catalog change.
 IF EXISTS(SELECT 1 FROM lexical172_before_occurrence_trigger b LEFT JOIN pg_trigger g USING(oid)
   WHERE g.oid IS NULL OR to_jsonb(g) IS DISTINCT FROM b.identity)
  OR (SELECT count(*) FROM pg_trigger g WHERE g.tgrelid='occurrence'::REGCLASS
      AND NOT EXISTS(SELECT 1 FROM lexical172_before_occurrence_trigger b WHERE b.oid=g.oid))<>2
  OR EXISTS(SELECT 1 FROM pg_trigger g JOIN pg_proc p ON p.oid=g.tgfoid
    LEFT JOIN pg_constraint c ON c.oid=g.tgconstraint
    WHERE g.tgrelid='occurrence'::REGCLASS
     AND NOT EXISTS(SELECT 1 FROM lexical172_before_occurrence_trigger b WHERE b.oid=g.oid)
     AND (NOT g.tgisinternal OR g.tgenabled<>'O' OR g.tgdeferrable OR g.tginitdeferred
       OR c.conrelid IS DISTINCT FROM 'storage_v2_encoded_lexical_block'::REGCLASS
       OR c.confrelid IS DISTINCT FROM 'occurrence'::REGCLASS OR c.contype IS DISTINCT FROM 'f'
       OR NOT c.convalidated OR c.condeferrable OR c.confdeltype<>'r' OR c.confupdtype<>'a'
       OR p.pronamespace<>'pg_catalog'::REGNAMESPACE
       OR NOT ((g.tgtype=9 AND p.proname='RI_FKey_restrict_del')
            OR (g.tgtype=17 AND p.proname='RI_FKey_noaction_upd')))) THEN
  RAISE EXCEPTION 'encoded lexical occurrence FK trigger delta differs';
 END IF;
 -- A temporary copy of the already-pinned catalog encoder masks ONLY the
 -- proven new RI triggers and replay trigger, and substitutes the witnessed
 -- old view definition. Every other original catalog byte must still match.
 -- No public helper/contract is changed or broadly re-signed by this proof.
 signature:=pg_get_functiondef('storage_v2_posting_conversion_relation_sha256(regclass)'::REGPROCEDURE);
 IF (length(signature)-length(replace(signature,'FUNCTION public.storage_v2_posting_conversion_relation_sha256(','')))
    /length('FUNCTION public.storage_v2_posting_conversion_relation_sha256(')<>1
  OR (length(signature)-length(replace(signature,'WHERE g.tgrelid=r.oid','')))/length('WHERE g.tgrelid=r.oid')<>1
  OR (length(signature)-length(replace(signature,'pg_get_viewdef(r.oid,true)','')))/length('pg_get_viewdef(r.oid,true)')<>1 THEN
  RAISE EXCEPTION 'encoded lexical catalog proof helper boundary differs';
 END IF;
 signature:=replace(signature,'FUNCTION public.storage_v2_posting_conversion_relation_sha256(',
   'FUNCTION pg_temp.lexical172_original_catalog(');
 signature:=replace(signature,'WHERE g.tgrelid=r.oid',$mask$
 WHERE g.tgrelid=r.oid
 AND NOT (g.tgrelid='occurrence'::REGCLASS AND g.tgisinternal
   AND g.tgconstrrelid='storage_v2_encoded_lexical_block'::REGCLASS
   AND g.tgconstraint IN (SELECT c.oid FROM pg_constraint c
     WHERE c.conrelid='storage_v2_encoded_lexical_block'::REGCLASS AND c.confrelid='occurrence'::REGCLASS AND c.contype='f'))
 AND NOT (g.tgrelid='storage_v2_compact_lexical_block'::REGCLASS
   AND g.tgname='storage_v2_compact_lexical_encoded_replay'
   AND g.tgfoid='storage_v2_compact_lexical_encoded_replay_guard()'::REGPROCEDURE
   AND g.tgtype=7 AND g.tgenabled='O' AND NOT g.tgisinternal)
$mask$);
 signature:=replace(signature,'pg_get_viewdef(r.oid,true)',
   '(CASE WHEN r.oid=''storage_v2_lexical_block_all''::REGCLASS THEN (SELECT definition FROM pg_temp.lexical172_before_view) ELSE pg_get_viewdef(r.oid,true) END)');
 EXECUTE signature;
 FOR previous IN SELECT * FROM lexical172_before_relation LOOP
  IF pg_temp.lexical172_original_catalog(previous.relation_oid::REGCLASS)
    IS DISTINCT FROM previous.identity_sha256 THEN
   RAISE EXCEPTION 'encoded lexical has an unreviewed original catalog delta';
  END IF;
 END LOOP;
 -- Only the replay trigger, logical union and two proved RI triggers advance.

 FOR previous IN SELECT * FROM lexical172_before_relation WHERE relation_oid IN
  ('storage_v2_compact_lexical_block'::REGCLASS,'storage_v2_lexical_block_all'::REGCLASS,'occurrence'::REGCLASS) LOOP
  UPDATE storage_v2_lexical_conversion_catalog_contract SET identity_sha256=
   storage_v2_posting_conversion_relation_sha256(previous.relation_oid::REGCLASS)
   WHERE relation_oid=previous.relation_oid AND identity_sha256=previous.identity_sha256;
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>1 THEN RAISE EXCEPTION 'encoded lexical named catalog predecessor CAS failed'; END IF;
 END LOOP;
 FOR previous IN SELECT * FROM lexical172_before_relation WHERE relation_oid NOT IN
  ('storage_v2_compact_lexical_block'::REGCLASS,'storage_v2_lexical_block_all'::REGCLASS,'occurrence'::REGCLASS) LOOP
  IF storage_v2_posting_conversion_relation_sha256(previous.relation_oid::REGCLASS)
    IS DISTINCT FROM previous.identity_sha256 THEN
   RAISE EXCEPTION 'encoded lexical changed an untouched original relation';
  END IF;
 END LOOP;
 FOREACH relation IN ARRAY ARRAY['storage_v2_encoded_lexical_block'::REGCLASS,
  'storage_v2_compact_lexical_block_all'::REGCLASS,'storage_v2_encoded_lexical_receipt'::REGCLASS,
  'storage_v2_encoded_lexical_intent'::REGCLASS,
  'storage_v2_encoded_lexical_restore_receipt'::REGCLASS] LOOP
  INSERT INTO storage_v2_lexical_conversion_catalog_contract VALUES(relation,
   storage_v2_posting_conversion_relation_sha256(relation));
 END LOOP;
 FOREACH signature IN ARRAY ARRAY['storage_v2_lexical_codec_u32(bytea,integer)',
  'storage_v2_encode_lexical_vectors(tsvector[])','storage_v2_decode_lexical_vectors(bytea)',
  'storage_v2_lexical_codec_exact_lexemes(bytea)','storage_v2_compact_lexical_exact_lexemes(tsvector[])',
  'storage_v2_guard_encoded_lexical_mutation()',
  'storage_v2_encoded_lexical_insert_guard()','storage_v2_compact_lexical_encoded_replay_guard()',
  'storage_v2_encoded_lexical_receipt_guard()',
  'storage_v2_require_encoded_lexical_quiescence(integer,bigint,bytea,bigint)',
  'storage_v2_restore_encoded_lexical_block(uuid,bytea,bigint,bigint,bytea,bytea,bigint,integer,bigint,bytea)',
  'storage_v2_encode_retained_lexical_block(uuid,bytea,bigint,bigint,bytea,bytea,bigint,integer,bigint,bytea)'] LOOP
  INSERT INTO storage_v2_lexical_conversion_function_contract
  SELECT signature,sha256(convert_to(pg_get_functiondef(oid),'UTF8')),proowner,
   sha256(convert_to(coalesce(proacl::TEXT,''),'UTF8')) FROM pg_proc WHERE oid=signature::REGPROCEDURE;
 END LOOP;
 -- All untouched dependencies must still equal the original seal entries.
 FOR previous IN SELECT b.* FROM lexical172_before_function b WHERE b.signature NOT IN
  (SELECT c.signature FROM storage_v2_lexical_conversion_function_contract c
   JOIN lexical172_before_function b USING(signature) WHERE c.definition_sha256<>b.definition_sha256) LOOP
  IF sha256(convert_to(pg_get_functiondef(previous.signature::REGPROCEDURE),'UTF8'))
   IS DISTINCT FROM previous.definition_sha256 THEN RAISE EXCEPTION 'unlisted lexical dependency changed'; END IF;
 END LOOP;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 UPDATE storage_v2_lexical_conversion_contract_seal SET
  function_count=(SELECT count(*)::INTEGER FROM storage_v2_lexical_conversion_function_contract),
  catalog_count=(SELECT count(*)::INTEGER FROM storage_v2_lexical_conversion_catalog_contract),
  contract_sha256=sha256(convert_to(jsonb_build_object(
   'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY c.signature) FROM storage_v2_lexical_conversion_function_contract c),
   'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY c.relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c)
  )::TEXT,'UTF8')) WHERE singleton;
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'encoded lexical requires exactly one original seal'; END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 PERFORM storage_v2_require_lexical_conversion_operator();
END $contract$;
COMMIT;
