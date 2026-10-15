-- Share only the derived-mask source lock; preserve the exact177 proof identity.
-- Semantic identity names the audited177 algorithm; implementation identity is
-- checked separately. No existing proof or payload row is rewritten.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SELECT storage_v2_require_lexical_conversion_operator();
LOCK TABLE storage_v2_lexical_conversion_function_contract,storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;
CREATE TEMP TABLE mask179_before AS
 SELECT pg_get_functiondef(oid) AS definition,to_jsonb(p)-'prosrc' AS authority,
  (SELECT jsonb_object_agg(x.oid::REGPROCEDURE::TEXT,encode(sha256(convert_to(pg_get_functiondef(x.oid),'UTF8')),'hex'))
   FROM pg_proc x WHERE x.pronamespace='public'::REGNAMESPACE AND x.prokind='f') AS functions,
  (SELECT jsonb_object_agg(x.oid::REGPROCEDURE::TEXT,to_jsonb(x)-'prosrc')
   FROM pg_proc x WHERE x.pronamespace='public'::REGNAMESPACE AND x.prokind='f') AS authorities,
  (SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c) AS pins,
  (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c) AS relations,
  (SELECT to_jsonb(c) FROM storage_v2_lexical_conversion_contract_seal c WHERE singleton) AS seal
 FROM pg_proc p WHERE oid='storage_v2_verify_derived_lexical_masks(bigint)'::REGPROCEDURE;
DO $guard$
BEGIN
 IF (SELECT encode(sha256(convert_to(definition,'UTF8')),'hex') FROM mask179_before)
     IS DISTINCT FROM '5d6fd919df0d3fe0bc1e40e8252eb15a7b90a32f5dd43a817c0511449f7d0235'
  OR (SELECT provolatile<>'v' OR proowner<>'mainrag_v2_frontier_owner'::REGROLE FROM pg_proc
      WHERE oid='storage_v2_verify_derived_lexical_masks(bigint)'::REGPROCEDURE)
  OR to_regclass('public.storage_v2_mask_verifier_semantic_identity') IS NOT NULL
  OR pg_has_role('mainrag','mainrag_v2_frontier_owner','MEMBER')
 THEN RAISE EXCEPTION 'exact177 verifier and private179 namespace required'; END IF;
END $guard$;
CREATE TABLE storage_v2_mask_verifier_semantic_identity (
 singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
 semantic_definition TEXT NOT NULL,
 semantic_definition_sha256 BYTEA NOT NULL CHECK(octet_length(semantic_definition_sha256)=32),
 semantic_version_sha256 BYTEA NOT NULL CHECK(octet_length(semantic_version_sha256)=32),
 approved_implementation_sha256 BYTEA NOT NULL CHECK(octet_length(approved_implementation_sha256)=32),
 fingerprint_function_sha256 BYTEA NOT NULL CHECK(octet_length(fingerprint_function_sha256)=32),
 CHECK(sha256(convert_to(semantic_definition,'UTF8'))=semantic_definition_sha256)
);
ALTER TABLE storage_v2_mask_verifier_semantic_identity OWNER TO mainrag_v2_frontier_owner;
REVOKE ALL ON storage_v2_mask_verifier_semantic_identity FROM PUBLIC,mainrag;
ALTER TABLE storage_v2_mask_verifier_semantic_identity ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_v2_mask_verifier_semantic_identity FORCE ROW LEVEL SECURITY;
CREATE POLICY mask_semantic_identity_reader ON storage_v2_mask_verifier_semantic_identity
 FOR SELECT TO mainrag_v2_frontier_owner USING(TRUE);
CREATE TRIGGER mask_semantic_identity_immutable BEFORE UPDATE OR DELETE
 ON storage_v2_mask_verifier_semantic_identity FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
CREATE TRIGGER mask_semantic_identity_no_truncate BEFORE TRUNCATE
 ON storage_v2_mask_verifier_semantic_identity FOR EACH STATEMENT EXECUTE FUNCTION storage_v2_reject_retrieval_mutation();
DO $reuse$
DECLARE original TEXT;patched TEXT;old_marker TEXT;new_marker TEXT;changed INTEGER;new_hash BYTEA;
 old_semantic_version BYTEA;checked_semantic_version BYTEA;expected JSONB;actual JSONB;pin RECORD;
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
 SELECT guard_version.verifier_sha256
 FROM public.storage_v2_mask_verifier_semantic_identity semantic
 CROSS JOIN LATERAL (
 SELECT sha256(record_send(ROW(
  semantic.semantic_definition,
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
) guard_version(verifier_sha256)
 WHERE semantic.singleton
  AND sha256(convert_to(semantic.semantic_definition,'UTF8'))=semantic.semantic_definition_sha256
  AND sha256(convert_to(pg_get_functiondef('public.storage_v2_verify_derived_lexical_masks(bigint)'::REGPROCEDURE),'UTF8'))=semantic.approved_implementation_sha256
  AND sha256(convert_to(pg_get_functiondef('public.storage_v2_posting_fingerprint(text)'::REGPROCEDURE),'UTF8'))=semantic.fingerprint_function_sha256
  AND guard_version.verifier_sha256=semantic.semantic_version_sha256
$version$;
old_version_query TEXT:=$old_version$
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
$old_version$;
old_oracle TEXT:=$old_oracle$  SELECT array_agg(setweight(to_tsvector('simple',convert_from(
       substring(bytes FROM item.start FOR item.length),'UTF8') COLLATE "default"),'A')
       ||setweight(to_tsvector('simple',item.prefix),'B')||setweight(to_tsvector('simple',item.kind),'C') ORDER BY item.ordinal)
   INTO vectors FROM unnest(block.text_byte_starts,block.text_byte_lengths,block.context_prefixes,block.chunk_types)
       WITH ORDINALITY item(start,length,prefix,kind,ordinal);
  IF block.segment_masks IS NOT NULL THEN
   SELECT array_agg(storage_v2_lexical_segment_mask(storage_v2_posting_fingerprints(tsvector_to_array(value)))
      ORDER BY ordinal) INTO expected FROM unnest(vectors) WITH ORDINALITY vector(value,ordinal);
   IF block.segment_masks IS DISTINCT FROM expected THEN
    RAISE EXCEPTION 'derived lexical segment mask differs from complete vector';
   END IF;
  END IF;
  IF block.first_terms IS NOT NULL THEN
   SELECT cache.terms,cache.ordinals INTO expected_terms,expected_ordinals
     FROM storage_v2_lexical_first_term_cache(vectors) cache;
   IF (block.first_terms,block.first_term_ordinals) IS DISTINCT FROM (expected_terms,expected_ordinals) THEN
    RAISE EXCEPTION 'derived lexical first term cache differs from complete vectors';
   END IF;
  END IF;$old_oracle$;
BEGIN
 SELECT definition INTO original FROM mask179_before;patched:=original;
 EXECUTE old_version_query INTO old_semantic_version;
 -- Preserve the original authority and no-mask early return. Semantic
 -- admission is required exactly where177 required its verifier version.
 old_marker:=old_version_query||' INTO cache_version;';
 new_marker:=replace(version_query,' SELECT guard_version.verifier_sha256',
  ' SELECT guard_version.verifier_sha256 INTO cache_version')||E';\n IF cache_version IS NULL THEN'
  ||E'\n  RAISE EXCEPTION ''unapproved mask verifier implementation or semantic dependencies''; END IF;';
 IF (length(patched)-length(replace(patched,old_marker,'')))/length(old_marker)<>1 THEN
  RAISE EXCEPTION 'exact177 cache version marker differs'; END IF;
 patched:=replace(patched,old_marker,new_marker);
 IF (length(patched)-length(replace(patched,old_oracle,'')))/length(old_oracle)<>1 THEN
  RAISE EXCEPTION 'exact177 unchanged oracle marker differs'; END IF;
 old_marker:='pg_try_advisory_xact_lock(hashtextextended(''mainrag.storage-v2-ingest-source:''||source::TEXT,0))';
 new_marker:='pg_try_advisory_xact_lock_shared(hashtextextended(''mainrag.storage-v2-ingest-source:''||source::TEXT,0))';
 IF (length(patched)-length(replace(patched,old_marker,'')))/length(old_marker)<>1 THEN
  RAISE EXCEPTION 'exact177 exclusive source lock marker differs'; END IF;
 patched:=replace(patched,old_marker,new_marker);
 IF (length(patched)-length(replace(patched,old_oracle,'')))/length(old_oracle)<>1
  OR position('NO_DERIVED_MASKS_REQUIRED' IN patched)>=position(new_marker IN patched)
  OR position(new_marker IN patched)>=position('FROM public.storage_v2_mask_verifier_semantic_identity' IN patched) THEN
  RAISE EXCEPTION 'shared lock, early return or unchanged oracle order differs'; END IF;
 EXECUTE patched;
 IF (SELECT to_jsonb(p)-'prosrc' FROM pg_proc p
     WHERE oid='storage_v2_verify_derived_lexical_masks(bigint)'::REGPROCEDURE)
     IS DISTINCT FROM (SELECT authority FROM mask179_before) THEN
  RAISE EXCEPTION 'mask179 authority or volatility differs'; END IF;
 new_hash:=sha256(convert_to(pg_get_functiondef('storage_v2_verify_derived_lexical_masks(bigint)'::REGPROCEDURE),'UTF8'));
 -- Insert after DDL: the implementation hash never appears in its own body.
 INSERT INTO storage_v2_mask_verifier_semantic_identity
 (singleton,semantic_definition,semantic_definition_sha256,semantic_version_sha256,approved_implementation_sha256,fingerprint_function_sha256)
 VALUES(TRUE,original,sha256(convert_to(original,'UTF8')),old_semantic_version,new_hash,
  sha256(convert_to(pg_get_functiondef('public.storage_v2_posting_fingerprint(text)'::REGPROCEDURE),'UTF8')));
 EXECUTE version_query INTO checked_semantic_version;
 IF checked_semantic_version IS DISTINCT FROM old_semantic_version THEN
  RAISE EXCEPTION 'mask179 semantic version changed during installation'; END IF;
 UPDATE storage_v2_lexical_conversion_function_contract SET definition_sha256=new_hash
 WHERE signature='storage_v2_verify_derived_lexical_masks(bigint)'
  AND definition_sha256=sha256(convert_to(original,'UTF8'));
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'mask179 verifier lexical predecessor CAS differs'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
  AND tgname='storage_v2_lexical_conversion_seal_immutable' AND tgenabled='O' AND tgtype=27
  AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
  RAISE EXCEPTION 'mask179 semantic proof lexical seal guard differs'; END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 UPDATE storage_v2_lexical_conversion_contract_seal SET contract_sha256=sha256(convert_to(jsonb_build_object(
  'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),
  'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
 WHERE singleton AND to_jsonb(storage_v2_lexical_conversion_contract_seal)=(SELECT seal FROM mask179_before);
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'mask179 semantic proof lexical seal predecessor CAS differs'; END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 FOR pin IN SELECT value FROM jsonb_array_elements((SELECT pins FROM mask179_before)) LOOP
  expected:=pin.value;
  IF expected->>'signature'='storage_v2_verify_derived_lexical_masks(bigint)' THEN
   expected:=jsonb_set(expected,'{definition_sha256}',to_jsonb('\x'||encode(new_hash,'hex'))); END IF;
  SELECT to_jsonb(c) INTO actual FROM storage_v2_lexical_conversion_function_contract c WHERE signature=pin.value->>'signature';
  IF actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'unexpected mask179 semantic proof lexical dependency delta'; END IF;
 END LOOP;
 IF (SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex')) FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f')
  IS DISTINCT FROM jsonb_set((SELECT functions FROM mask179_before),ARRAY['storage_v2_verify_derived_lexical_masks(bigint)'],to_jsonb(encode(new_hash,'hex')))
  OR (SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,to_jsonb(p)-'prosrc') FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f') IS DISTINCT FROM (SELECT authorities FROM mask179_before)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c) IS DISTINCT FROM (SELECT relations FROM mask179_before)
  OR (SELECT to_jsonb(c)-'contract_sha256' FROM storage_v2_lexical_conversion_contract_seal c WHERE singleton) IS DISTINCT FROM (SELECT seal-'contract_sha256' FROM mask179_before)
  OR (SELECT count(*) FROM storage_v2_lexical_conversion_function_contract)<>jsonb_array_length((SELECT pins FROM mask179_before)) THEN
  RAISE EXCEPTION 'mask179 semantic proof public functions or lexical payload inventory differs'; END IF;
END $reuse$;
SELECT storage_v2_require_lexical_conversion_operator();
DROP TABLE mask179_before;
COMMIT;
