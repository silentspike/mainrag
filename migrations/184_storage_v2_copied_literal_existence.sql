-- Use a bounded existence predicate for copied ranks; exact evidence still counts.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SELECT storage_v2_posting_conversion_require_operator();
SELECT storage_v2_require_lexical_conversion_operator();
LOCK TABLE storage_v2_posting_conversion_contract,
 storage_v2_lexical_conversion_function_contract,storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

CREATE TEMP TABLE copied184_before ON COMMIT DROP AS
 SELECT
  (SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,
    encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex'))
   FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f') AS functions,
  (SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,to_jsonb(p)-'prosrc')
   FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f') AS authorities,
  (SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_posting_conversion_contract c) AS posting,
  (SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c) AS lexical,
  (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c) AS posting_relations,
  (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c) AS lexical_relations,
  (SELECT to_jsonb(c) FROM storage_v2_lexical_conversion_contract_seal c WHERE singleton) AS seal;

DO $copied_body$
DECLARE
 signatures CONSTANT TEXT[]:=ARRAY[
  'storage_v2_source_segment_rank_candidates(bigint[],text)',
  'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'];
 predecessors CONSTANT TEXT[]:=ARRAY[
  '0ac39bd401c7ccaf69da5f40f7d095ea1443026b3c7047e9111eb3965e283be7',
  '03c54fc3c3138747b4a0c0bb983fd8c5a4cb2e22c19d260cbd7672885b89c851'];
 body_count CONSTANT TEXT:='public.storage_v2_literal_term_count(literal_document.search_text,p_query)>0';
 decoded_count CONSTANT TEXT:='public.storage_v2_literal_term_count(decoded.value #>> ''{}'',p_query)>0';
 proof_start CONSTANT TEXT:='AND (literal_projection.legacy_file_hash=decode(literal_artifact.expected_content_hash,''hex'')';
 body_start CONSTANT TEXT:='AND ('||body_count;
 body_end CONSTANT TEXT:='ELSE FALSE END FROM jsonl_lines))';
 word_pattern CONSTANT TEXT:=$pattern$E'\\m'||lower(p_query)||E'\\M'$pattern$;
 definition TEXT; new_hash BYTEA; rank_index INTEGER; changed INTEGER;
 pin RECORD; expected JSONB; actual JSONB;
 expected_functions JSONB:=(SELECT functions FROM copied184_before);
BEGIN
 IF inet_client_addr() IS NOT NULL
  OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
  OR current_setting('role') NOT IN ('none',session_user)
  OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'copied literal existence requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_literal_term_count(text,text)'::REGPROCEDURE),'UTF8')),'hex')
    IS DISTINCT FROM '2a4d00c7b51b87f579f5389deb493b9207472abf475036babcc74d49529cca00' THEN
  RAISE EXCEPTION 'copied literal existence oracle differs';
 END IF;
 FOR rank_index IN 1..cardinality(signatures) LOOP
  definition:=pg_get_functiondef(signatures[rank_index]::REGPROCEDURE);
  IF encode(sha256(convert_to(definition,'UTF8')),'hex') IS DISTINCT FROM predecessors[rank_index]
   OR (length(definition)-length(replace(definition,proof_start,'')))/length(proof_start)<>1
   OR (length(definition)-length(replace(definition,body_start,'')))/length(body_start)<>1
   OR (length(definition)-length(replace(definition,body_end,'')))/length(body_end)<>1
   OR (length(definition)-length(replace(definition,decoded_count,'')))/length(decoded_count)<>1 THEN
   RAISE EXCEPTION 'copied literal existence predecessor or boundary differs: %',signatures[rank_index];
  END IF;
  IF NOT EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_function_contract
    WHERE signature=signatures[rank_index] AND definition_sha256=decode(predecessors[rank_index],'hex'))
   OR EXISTS(SELECT 1 FROM storage_v2_posting_conversion_contract
    WHERE signature=signatures[rank_index] AND definition_sha256<>decode(predecessors[rank_index],'hex')) THEN
   RAISE EXCEPTION 'copied literal existence dependency pin differs';
  END IF;
  -- The simple term fence already excludes punctuation. Check the folded term
  -- as well before placing it in the regex, and retain the original predicate
  -- for a locale-specific fold that no longer consists entirely of word chars.
  -- WHERE conjuncts can be reordered. Only open the canonical body once
  -- the complete file identity and copied projection predicate both pass.
  definition:=replace(definition,proof_start,replace(proof_start,'AND (','AND CASE WHEN ('));
  definition:=replace(definition,body_start,
      'THEN (CASE WHEN lower(p_query) ~ ''^[[:alnum:]_]+$'' THEN regexp_like(lower(literal_document.search_text),'
      ||word_pattern||') ELSE '||body_count||' END');
  definition:=replace(definition,decoded_count,
      'CASE WHEN lower(p_query) ~ ''^[[:alnum:]_]+$'' THEN regexp_like(lower(decoded.value #>> ''{}''),'
      ||word_pattern||') ELSE '||decoded_count||' END');
  definition:=replace(definition,body_end,body_end||' ELSE FALSE END');
  EXECUTE definition;
  new_hash:=sha256(convert_to(pg_get_functiondef(signatures[rank_index]::REGPROCEDURE),'UTF8'));
  expected_functions:=jsonb_set(expected_functions,ARRAY[signatures[rank_index]],to_jsonb(encode(new_hash,'hex')));
  UPDATE storage_v2_lexical_conversion_function_contract SET definition_sha256=new_hash
   WHERE signature=signatures[rank_index] AND definition_sha256=decode(predecessors[rank_index],'hex');
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>1 THEN RAISE EXCEPTION 'copied literal existence lexical predecessor CAS differs'; END IF;
  UPDATE storage_v2_posting_conversion_contract SET definition_sha256=new_hash
   WHERE signature=signatures[rank_index] AND definition_sha256=decode(predecessors[rank_index],'hex');
 END LOOP;
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
  AND tgname='storage_v2_lexical_conversion_seal_immutable' AND tgenabled='O' AND tgtype=27
  AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
  RAISE EXCEPTION 'copied literal existence lexical seal guard differs';
 END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 UPDATE storage_v2_lexical_conversion_contract_seal SET contract_sha256=sha256(convert_to(jsonb_build_object(
  'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),
  'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
 WHERE singleton AND to_jsonb(storage_v2_lexical_conversion_contract_seal)=(SELECT seal FROM copied184_before);
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'copied literal existence lexical seal predecessor CAS differs'; END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 FOR pin IN SELECT 'posting' AS kind,value FROM jsonb_array_elements((SELECT posting FROM copied184_before))
  UNION ALL SELECT 'lexical',value FROM jsonb_array_elements((SELECT lexical FROM copied184_before)) LOOP
  expected:=pin.value;
  IF expected->>'signature'=ANY(signatures) THEN
   expected:=jsonb_set(expected,'{definition_sha256}',to_jsonb('\x'||(expected_functions->>(expected->>'signature'))));
  END IF;
  IF pin.kind='posting' THEN
   SELECT to_jsonb(c) INTO actual FROM storage_v2_posting_conversion_contract c WHERE signature=pin.value->>'signature';
  ELSE
   SELECT to_jsonb(c) INTO actual FROM storage_v2_lexical_conversion_function_contract c WHERE signature=pin.value->>'signature';
  END IF;
  IF actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'unexpected copied literal existence dependency delta'; END IF;
 END LOOP;
 IF (SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex'))
     FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f') IS DISTINCT FROM expected_functions
  OR (SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,to_jsonb(p)-'prosrc')
     FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f') IS DISTINCT FROM (SELECT authorities FROM copied184_before)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c) IS DISTINCT FROM (SELECT posting_relations FROM copied184_before)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c) IS DISTINCT FROM (SELECT lexical_relations FROM copied184_before)
  OR (SELECT to_jsonb(c)-'contract_sha256' FROM storage_v2_lexical_conversion_contract_seal c WHERE singleton) IS DISTINCT FROM (SELECT seal-'contract_sha256' FROM copied184_before)
  OR (SELECT count(*) FROM storage_v2_posting_conversion_contract)<>jsonb_array_length((SELECT posting FROM copied184_before))
  OR (SELECT count(*) FROM storage_v2_lexical_conversion_function_contract)<>jsonb_array_length((SELECT lexical FROM copied184_before)) THEN
  RAISE EXCEPTION 'copied literal existence authority, function or catalog inventory differs';
 END IF;
END $copied_body$;
SELECT storage_v2_posting_conversion_require_operator();
SELECT storage_v2_require_lexical_conversion_operator();
COMMIT;
