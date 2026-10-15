-- Persist exact completed derived-mask verification; never cache an assumption.
-- A miss retains every original canonical reconstruction and comparison.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SELECT storage_v2_require_lexical_conversion_operator();

LOCK TABLE storage_v2_lexical_conversion_function_contract,storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;
CREATE TEMP TABLE mask177_before AS
 SELECT pg_get_functiondef(oid) AS definition,to_jsonb(p)-'prosrc'-'provolatile' AS authority,
        provolatile,
        (SELECT jsonb_object_agg(x.oid::REGPROCEDURE::TEXT,encode(sha256(convert_to(pg_get_functiondef(x.oid),'UTF8')),'hex')) FROM pg_proc x WHERE x.pronamespace='public'::REGNAMESPACE AND x.prokind='f') AS functions,
        (SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c) AS pins,
        (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c) AS relations,
        (SELECT to_jsonb(c) FROM storage_v2_lexical_conversion_contract_seal c WHERE singleton) AS seal
 FROM pg_proc p
 WHERE oid='storage_v2_verify_derived_lexical_masks(bigint)'::REGPROCEDURE;
DO $guard$
BEGIN
 IF (SELECT encode(sha256(convert_to(definition,'UTF8')),'hex') FROM mask177_before)
       IS DISTINCT FROM 'b714012ee76962f4119b8cd8f3dd9e961da9642a50b427951fc2715600dedd9a'
    OR (SELECT provolatile FROM mask177_before)<>'s'
    OR (SELECT proowner FROM pg_proc WHERE oid='storage_v2_verify_derived_lexical_masks(bigint)'::REGPROCEDURE)
       <>'mainrag_v2_frontier_owner'::REGROLE
    OR to_regclass('public.storage_v2_derived_mask_verification_proof') IS NOT NULL
    OR pg_has_role('mainrag','mainrag_v2_frontier_owner','MEMBER') THEN
  RAISE EXCEPTION 'derived mask verification predecessor or private namespace differs';
 END IF;
END $guard$;

-- No foreign keys: this disposable proof must never retain a data owner for GC.
-- Only digests and identity numbers are stored; canonical text/vectors are not.
-- Hits hash the actual canonical UTF8 bytes; tuple addresses alone are not proof.
-- Verifier, helper, locale/configuration and server-version drift invalidate hits.
CREATE TABLE storage_v2_derived_mask_verification_proof (
 occurrence_id BIGINT NOT NULL,source_id BIGINT NOT NULL,artifact_version_id BIGINT NOT NULL,
 document_id BIGINT NOT NULL,canonical_identity_sha256 BYTEA NOT NULL CHECK(octet_length(canonical_identity_sha256)=32),
 block_manifest_sha256 BYTEA NOT NULL CHECK(octet_length(block_manifest_sha256)=32),
 verifier_sha256 BYTEA NOT NULL CHECK(octet_length(verifier_sha256)=32),
 canonical_body_sha256 BYTEA NOT NULL CHECK(octet_length(canonical_body_sha256)=32),
 block_count BIGINT NOT NULL CHECK(block_count>0),verified_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(occurrence_id,verifier_sha256,canonical_identity_sha256,block_manifest_sha256)
);
ALTER TABLE storage_v2_derived_mask_verification_proof OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON storage_v2_derived_mask_verification_proof FROM PUBLIC,mainrag;
ALTER TABLE storage_v2_derived_mask_verification_proof ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_derived_mask_verification_proof FORCE ROW LEVEL SECURITY;
CREATE POLICY derived_mask_proof_owner ON storage_v2_derived_mask_verification_proof
 TO mainrag_v2_frontier_owner USING(TRUE) WITH CHECK(TRUE);
CREATE TRIGGER storage_v2_derived_mask_proof_immutable BEFORE UPDATE OR DELETE
 ON storage_v2_derived_mask_verification_proof FOR EACH ROW
 EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();

DO $cache$
DECLARE original TEXT;patched TEXT;old_marker TEXT;new_marker TEXT;changed INTEGER;new_hash BYTEA;expected JSONB;actual JSONB;pin RECORD;
identity_query TEXT:=$identity$
 SELECT d.id AS document_id,sha256(record_send(ROW(
  o.id,o.source_id,o.artifact_version_id,o.view_id,
  a.id,a.content_root_node_id,a.expected_content_hash,a.byte_length,
  n.id,n.digest_schema,n.domain,n.node_type,n.logical_length,n.body_id,n.node_digest,
  b.id,b.digest_algorithm,b.digest,b.logical_length,
  d.id,d.profile_id,d.component_kind,d.body_id,d.node_id,d.materialization_sha256,
  d.xmin::TEXT,d.ctid::TEXT,sha256(convert_to(d.search_text,'UTF8'))))) AS identity_sha256
 FROM public.occurrence o
 JOIN public.artifact_version a ON a.id=o.artifact_version_id AND a.source_id=o.source_id
 JOIN public.content_node n ON n.id=a.content_root_node_id
 LEFT JOIN public.content_body b ON b.id=n.body_id
 JOIN public.storage_v2_search_view_document binding ON binding.view_id=o.view_id AND binding.ordinal=0
 JOIN public.storage_v2_search_document d ON d.id=binding.document_id
  AND d.component_kind='node' AND d.node_id=a.content_root_node_id
 WHERE o.id=p_occurrence_id
$identity$;
manifest_query TEXT:=$manifest$
 SELECT count(*),sha256(coalesce(string_agg(sha256(record_send(stored)),''::BYTEA ORDER BY stored.block_order),''::BYTEA))
 FROM public.storage_v2_derived_lexical_block stored WHERE stored.occurrence_id=p_occurrence_id
$manifest$;
version_query TEXT:=$version$
 SELECT sha256(record_send(ROW(
  pg_get_functiondef('public.storage_v2_verify_derived_lexical_masks(bigint)'::REGPROCEDURE),
  pg_get_functiondef('public.storage_v2_lexical_first_term_cache(tsvector[])'::REGPROCEDURE),
  pg_get_functiondef('public.storage_v2_lexical_segment_mask(integer[])'::REGPROCEDURE),
  pg_get_functiondef('public.storage_v2_posting_fingerprints(text[])'::REGPROCEDURE),
  current_setting('server_version_num'),getdatabaseencoding(),
  (SELECT jsonb_build_array(to_jsonb(c),to_jsonb(p)) FROM pg_ts_config c
     JOIN pg_ts_parser p ON p.oid=c.cfgparser WHERE c.oid='pg_catalog.simple'::REGCONFIG),
  (SELECT jsonb_build_array(datlocprovider,datlocale,datcollate,datctype,datcollversion)
     FROM pg_database WHERE datname=current_database()),
  (SELECT jsonb_agg(jsonb_build_array(to_jsonb(m),to_jsonb(d),to_jsonb(t)) ORDER BY m.maptokentype,m.mapseqno)
     FROM pg_ts_config_map m JOIN pg_ts_dict d ON d.oid=m.mapdict
     JOIN pg_ts_template t ON t.oid=d.dicttemplate WHERE m.mapcfg='pg_catalog.simple'::REGCONFIG))))
$version$;
BEGIN
 SELECT definition INTO original FROM mask177_before;patched:=original;
 old_marker:='vectors TSVECTOR[]; expected_terms TEXT[]; expected_ordinals SMALLINT[];';
 new_marker:=old_marker||E'\n cache_document RECORD; cache_after RECORD; cache_version BYTEA; cache_manifest BYTEA;'
  ||E'\n cache_manifest_after BYTEA; cache_blocks BIGINT; cache_blocks_after BIGINT;';
 IF (length(patched)-length(replace(patched,old_marker,'')))/length(old_marker)<>1 THEN
  RAISE EXCEPTION 'derived mask verifier declaration marker differs';
 END IF;
 patched:=replace(patched,old_marker,new_marker);
 old_marker:=' IF NOT EXISTS(SELECT 1 FROM public.storage_v2_derived_lexical_block stored'
  ||E'\n       WHERE stored.occurrence_id=p_occurrence_id AND (stored.segment_masks IS NOT NULL OR stored.first_terms IS NOT NULL)) THEN RETURN; END IF;';
 new_marker:=' IF NOT EXISTS(SELECT 1 FROM public.storage_v2_derived_lexical_block stored'
  ||E'\n       WHERE stored.occurrence_id=p_occurrence_id AND (stored.segment_masks IS NOT NULL OR stored.first_terms IS NOT NULL)) THEN'
  ||E'\n  PERFORM set_config(''mainrag.derived_mask177_last_disposition'',''NO_DERIVED_MASKS_REQUIRED'',false); RETURN; END IF;'
  ||E'\n -- One source lock, not one lock per occurrence: finalization visits38079 owners.'
  ||E'\n IF NOT pg_try_advisory_xact_lock(hashtextextended(''mainrag.storage-v2-ingest-source:''||source::TEXT,0)) THEN'
  ||E'\n  RAISE EXCEPTION ''derived mask verification source writer boundary is owned''; END IF;'
  ||E'\n'||replace(identity_query,' FROM public.occurrence o',' INTO cache_document FROM public.occurrence o')||';';
 new_marker:=new_marker||E'\n IF NOT FOUND THEN RAISE EXCEPTION ''authorized canonical lexical document required'' USING ERRCODE=''42501''; END IF;'
  ||E'\n'||replace(manifest_query,' FROM public.storage_v2_derived_lexical_block stored',
    ' INTO cache_blocks,cache_manifest FROM public.storage_v2_derived_lexical_block stored')||';'
  ||E'\n'||version_query||' INTO cache_version;'
  ||E'\n IF EXISTS(SELECT 1 FROM public.storage_v2_derived_mask_verification_proof proof'
  ||E'\n  WHERE proof.occurrence_id=p_occurrence_id AND proof.source_id=source'
  ||E'\n   AND proof.document_id=cache_document.document_id AND proof.verifier_sha256=cache_version'
  ||E'\n   AND proof.canonical_identity_sha256=cache_document.identity_sha256'
  ||E'\n   AND proof.block_manifest_sha256=cache_manifest AND proof.block_count=cache_blocks) THEN'
  ||E'\n  PERFORM set_config(''mainrag.derived_mask177_last_disposition'',''HIT_EXACT_IMMUTABLE_PROOF'',false); RETURN; END IF;';
 IF (length(patched)-length(replace(patched,old_marker,'')))/length(old_marker)<>1 THEN
  RAISE EXCEPTION 'derived mask verifier early proof boundary differs';
 END IF;
 patched:=replace(patched,old_marker,new_marker);
 old_marker:=E' END LOOP;\nEND ';
 new_marker:=E' END LOOP;\n'||replace(identity_query,' FROM public.occurrence o',' INTO cache_after FROM public.occurrence o')||';'
  ||E'\n IF NOT FOUND OR cache_after IS DISTINCT FROM cache_document THEN'
  ||E'\n  RAISE EXCEPTION ''canonical lexical identity changed during exact mask verification''; END IF;'
  ||E'\n'||replace(manifest_query,' FROM public.storage_v2_derived_lexical_block stored',
    ' INTO cache_blocks_after,cache_manifest_after FROM public.storage_v2_derived_lexical_block stored')||';'
  ||E'\n IF cache_blocks_after IS DISTINCT FROM cache_blocks OR cache_manifest_after IS DISTINCT FROM cache_manifest THEN'
  ||E'\n  RAISE EXCEPTION ''derived lexical payload changed during exact mask verification''; END IF;'
  ||E'\n INSERT INTO public.storage_v2_derived_mask_verification_proof'
  ||E'\n (occurrence_id,source_id,artifact_version_id,document_id,canonical_identity_sha256,block_manifest_sha256,verifier_sha256,canonical_body_sha256,block_count)'
  ||E'\n SELECT p_occurrence_id,source,o.artifact_version_id,cache_document.document_id,cache_document.identity_sha256,cache_manifest,cache_version,sha256(bytes),cache_blocks'
  ||E'\n FROM public.occurrence o WHERE o.id=p_occurrence_id ON CONFLICT DO NOTHING;'
  ||E'\n PERFORM set_config(''mainrag.derived_mask177_last_disposition'',''MISS_EXACTLY_VERIFIED_AND_PERSISTED'',false);\nEND ';
 IF (length(patched)-length(replace(patched,old_marker,'')))/length(old_marker)<>1 THEN
  RAISE EXCEPTION 'derived mask verifier final comparison boundary differs';
 END IF;
 patched:=replace(patched,old_marker,new_marker);
 -- Only volatility changes; all authority and original reconstruction stay.
 patched:=replace(patched,' STABLE ',' VOLATILE ');
 EXECUTE patched;
 ALTER FUNCTION storage_v2_verify_derived_lexical_masks(BIGINT) VOLATILE;
 IF (SELECT to_jsonb(p)-'prosrc'-'provolatile' FROM pg_proc p
      WHERE oid='storage_v2_verify_derived_lexical_masks(bigint)'::REGPROCEDURE)
       IS DISTINCT FROM (SELECT authority FROM mask177_before) THEN
  RAISE EXCEPTION 'derived mask cache changed verifier authority';
 END IF;
 new_hash:=sha256(convert_to(pg_get_functiondef('storage_v2_verify_derived_lexical_masks(bigint)'::REGPROCEDURE),'UTF8'));
 UPDATE storage_v2_lexical_conversion_function_contract SET definition_sha256=new_hash
 WHERE signature='storage_v2_verify_derived_lexical_masks(bigint)'
  AND definition_sha256=sha256(convert_to(original,'UTF8'));
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'derived mask verifier lexical predecessor CAS differs'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
  AND tgname='storage_v2_lexical_conversion_seal_immutable' AND tgenabled='O' AND tgtype=27
  AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
  RAISE EXCEPTION 'derived mask proof lexical seal guard differs'; END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 UPDATE storage_v2_lexical_conversion_contract_seal SET contract_sha256=sha256(convert_to(jsonb_build_object(
  'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),
  'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
 WHERE singleton AND to_jsonb(storage_v2_lexical_conversion_contract_seal)=(SELECT seal FROM mask177_before);
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'derived mask proof lexical seal predecessor CAS differs'; END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 FOR pin IN SELECT value FROM jsonb_array_elements((SELECT pins FROM mask177_before)) LOOP
  expected:=pin.value;
  IF expected->>'signature'='storage_v2_verify_derived_lexical_masks(bigint)' THEN
   expected:=jsonb_set(expected,'{definition_sha256}',to_jsonb('\x'||encode(new_hash,'hex'))); END IF;
  SELECT to_jsonb(c) INTO actual FROM storage_v2_lexical_conversion_function_contract c WHERE signature=pin.value->>'signature';
  IF actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'unexpected derived mask proof lexical dependency delta'; END IF;
 END LOOP;
 IF (SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex')) FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f')
  IS DISTINCT FROM jsonb_set((SELECT functions FROM mask177_before),ARRAY['storage_v2_verify_derived_lexical_masks(bigint)'],to_jsonb(encode(new_hash,'hex')))
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c) IS DISTINCT FROM (SELECT relations FROM mask177_before)
  OR (SELECT to_jsonb(c)-'contract_sha256' FROM storage_v2_lexical_conversion_contract_seal c WHERE singleton) IS DISTINCT FROM (SELECT seal-'contract_sha256' FROM mask177_before)
  OR (SELECT count(*) FROM storage_v2_lexical_conversion_function_contract)<>jsonb_array_length((SELECT pins FROM mask177_before)) THEN
  RAISE EXCEPTION 'derived mask proof public functions or lexical payload inventory differs'; END IF;
END $cache$;
SELECT storage_v2_require_lexical_conversion_operator();
DROP TABLE mask177_before;
COMMIT;
