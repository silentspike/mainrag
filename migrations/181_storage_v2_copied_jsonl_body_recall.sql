-- Decode complete, valid JSONL body string values before checking literal
-- token boundaries. Raw JSON escape letters are not decoded word characters.
-- This changes only the two copied rank predicates installed by migration 180;
-- whole-file identity, copied FTS, actor and source fences remain unchanged.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SELECT storage_v2_posting_conversion_require_operator();
SELECT storage_v2_require_lexical_conversion_operator();
LOCK TABLE storage_v2_posting_conversion_contract,
 storage_v2_lexical_conversion_function_contract,storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

CREATE TEMP TABLE copied181_before ON COMMIT DROP AS
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
  '1ebb002a0817d761d326217d780af73cecda29226d0e3d4e62445fd605c3d26f',
  '7dd485c6268ba37694be1fed59d0b68ffc6ccb8005aa9f30e12deeb66b078176'];
 marker TEXT:=$old$              AND public.storage_v2_literal_term_count(literal_document.search_text,p_query)>0$old$;
 replacement TEXT:=$new$              AND (public.storage_v2_literal_term_count(literal_document.search_text,p_query)>0
                OR (WITH jsonl_lines AS MATERIALIZED (
                    SELECT line FROM string_to_table(literal_document.search_text,chr(10)) AS line
                    WHERE btrim(line,E' \t\r')<>''
                ) SELECT CASE WHEN bool_and(
                    CASE WHEN pg_input_is_valid(line,'jsonb')
                         THEN jsonb_typeof(line::JSONB) IN ('object','array')
                         ELSE FALSE END
                ) THEN EXISTS (
                    SELECT 1 FROM jsonl_lines decoded_line
                    CROSS JOIN LATERAL jsonb_path_query(
                        -- Keep casts safe independently of planner evaluation
                        -- order; malformed records never become body evidence.
                        CASE WHEN pg_input_is_valid(decoded_line.line,'jsonb')
                             THEN decoded_line.line::JSONB ELSE 'null'::JSONB END,
                        'strict $.** ? (@.type() == "string")'
                    ) AS decoded(value)
                    WHERE public.storage_v2_literal_term_count(decoded.value #>> '{}',p_query)>0
                ) ELSE FALSE END FROM jsonl_lines))$new$;
 definition TEXT;new_hash BYTEA;index INTEGER;changed INTEGER;pin RECORD;expected JSONB;actual JSONB;
 expected_functions JSONB:=(SELECT functions FROM copied181_before);
BEGIN
 IF inet_client_addr() IS NOT NULL
  OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
  OR current_setting('role') NOT IN ('none',session_user)
  OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'copied body recall requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_literal_term_count(text,text)'::REGPROCEDURE),'UTF8')),'hex')
    IS DISTINCT FROM '2a4d00c7b51b87f579f5389deb493b9207472abf475036babcc74d49529cca00' THEN
  RAISE EXCEPTION 'copied body literal oracle differs';
 END IF;
 FOR index IN 1..cardinality(signatures) LOOP
  definition:=pg_get_functiondef(signatures[index]::REGPROCEDURE);
  IF encode(sha256(convert_to(definition,'UTF8')),'hex') IS DISTINCT FROM predecessors[index]
   OR (SELECT proowner<>'mainrag_v2_frontier_owner'::REGROLE OR NOT prosecdef OR provolatile<>'s'
       FROM pg_proc WHERE oid=signatures[index]::REGPROCEDURE)
   OR (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
   RAISE EXCEPTION 'copied body rank predecessor or boundary differs: %',signatures[index];
  END IF;
  IF NOT EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_function_contract c
     WHERE c.signature=signatures[index] AND c.definition_sha256=decode(predecessors[index],'hex')) THEN
   RAISE EXCEPTION 'copied body lexical predecessor pin differs';
  END IF;
  EXECUTE replace(definition,marker,replacement);
  new_hash:=sha256(convert_to(pg_get_functiondef(signatures[index]::REGPROCEDURE),'UTF8'));
  expected_functions:=jsonb_set(expected_functions,ARRAY[signatures[index]],to_jsonb(encode(new_hash,'hex')));
  UPDATE storage_v2_lexical_conversion_function_contract SET definition_sha256=new_hash
   WHERE signature=signatures[index] AND definition_sha256=decode(predecessors[index],'hex');
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>1 THEN RAISE EXCEPTION 'copied body lexical predecessor CAS differs'; END IF;
  -- Advance only dependencies already present in the original audited ABI.
  UPDATE storage_v2_posting_conversion_contract SET definition_sha256=new_hash
   WHERE signature=signatures[index] AND definition_sha256=decode(predecessors[index],'hex');
 END LOOP;
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
  AND tgname='storage_v2_lexical_conversion_seal_immutable' AND tgenabled='O' AND tgtype=27
  AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
  RAISE EXCEPTION 'copied body lexical seal guard differs';
 END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 UPDATE storage_v2_lexical_conversion_contract_seal SET contract_sha256=sha256(convert_to(jsonb_build_object(
  'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),
  'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
 WHERE singleton AND to_jsonb(storage_v2_lexical_conversion_contract_seal)=(SELECT seal FROM copied181_before);
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'copied body lexical seal predecessor CAS differs'; END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 FOR pin IN SELECT 'posting' AS kind,value FROM jsonb_array_elements((SELECT posting FROM copied181_before))
  UNION ALL SELECT 'lexical',value FROM jsonb_array_elements((SELECT lexical FROM copied181_before)) LOOP
  expected:=pin.value;
  IF expected->>'signature'=ANY(signatures) THEN
   expected:=jsonb_set(expected,'{definition_sha256}',to_jsonb('\x'||(expected_functions->>(expected->>'signature'))));
  END IF;
  IF pin.kind='posting' THEN
   SELECT to_jsonb(c) INTO actual FROM storage_v2_posting_conversion_contract c WHERE signature=pin.value->>'signature';
  ELSE
   SELECT to_jsonb(c) INTO actual FROM storage_v2_lexical_conversion_function_contract c WHERE signature=pin.value->>'signature';
  END IF;
  IF actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'unexpected copied body dependency delta'; END IF;
 END LOOP;
 IF (SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex'))
     FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f') IS DISTINCT FROM expected_functions
  OR (SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,to_jsonb(p)-'prosrc')
     FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f') IS DISTINCT FROM (SELECT authorities FROM copied181_before)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c) IS DISTINCT FROM (SELECT posting_relations FROM copied181_before)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c) IS DISTINCT FROM (SELECT lexical_relations FROM copied181_before)
  OR (SELECT to_jsonb(c)-'contract_sha256' FROM storage_v2_lexical_conversion_contract_seal c WHERE singleton) IS DISTINCT FROM (SELECT seal-'contract_sha256' FROM copied181_before)
  OR (SELECT count(*) FROM storage_v2_posting_conversion_contract)<>jsonb_array_length((SELECT posting FROM copied181_before))
  OR (SELECT count(*) FROM storage_v2_lexical_conversion_function_contract)<>jsonb_array_length((SELECT lexical FROM copied181_before)) THEN
  RAISE EXCEPTION 'copied body authority, function or catalog inventory differs';
 END IF;
END $copied_body$;
SELECT storage_v2_posting_conversion_require_operator();
SELECT storage_v2_require_lexical_conversion_operator();
COMMIT;
