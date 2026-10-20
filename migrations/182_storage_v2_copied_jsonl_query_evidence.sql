-- Independently qualify decoded canonical JSONL body support for copied hits.
-- Keep raw frequency/FTS/segment evidence honest. The explicit v5 field requires
-- complete valid object/array JSONL, a strict token in decoded string values,
-- exact whole-file copied identity and copied FTS. No rank/reader is changed.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SELECT storage_v2_posting_conversion_require_operator();
SELECT storage_v2_require_lexical_conversion_operator();
LOCK TABLE storage_v2_posting_conversion_contract,
 storage_v2_lexical_conversion_function_contract,storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

CREATE TEMP TABLE copied182_before ON COMMIT DROP AS
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
  'storage_v2_candidate_query_evidence(bigint,bigint,text,text,bigint[],bigint[])'];
 predecessors CONSTANT TEXT[]:=ARRAY[
  '8fa1475a9064fdacc509b118da2e641bab96136e95b2bec0d6dfed8c9041e800'];
 marker TEXT:=$old$               storage_v2_source_legacy_segment_matches(id, p_query) AS legacy_segment_matches,$old$;
 replacement TEXT:=$new$               storage_v2_source_legacy_segment_matches(id, p_query) AS legacy_segment_matches,
               CASE WHEN p_query ~ '^[[:alnum:]_]+$'
                              AND lower(p_query) NOT IN ('and','or','not') THEN
                 EXISTS (
                   SELECT 1 FROM public.occurrence copied_occurrence
                   JOIN public.artifact_version copied_artifact
                     ON copied_artifact.id=copied_occurrence.artifact_version_id
                    AND copied_artifact.source_id=copied_occurrence.source_id
                   JOIN public.storage_v2_search_document copied_document
                     ON copied_document.id=candidate.document_id
                    AND copied_document.node_id=copied_artifact.content_root_node_id
                    AND copied_document.component_kind='node'
                   JOIN public.storage_v2_legacy_lexical_segment copied_projection
                     ON copied_projection.occurrence_id=copied_occurrence.id
                    AND copied_projection.source_id=copied_occurrence.source_id
                    AND copied_projection.artifact_version_id=copied_artifact.id
                   WHERE copied_occurrence.id=candidate.id
                     AND copied_occurrence.source_id=p_source_id
                     AND copied_projection.legacy_file_hash=decode(copied_artifact.expected_content_hash,'hex')
                     AND copied_projection.fts_vector@@websearch_to_tsquery('simple',p_query)
                 ) AND COALESCE((WITH jsonl_lines AS MATERIALIZED (
                    SELECT line FROM string_to_table(candidate.search_text,chr(10)) AS line
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
                ) ELSE FALSE END FROM jsonl_lines),FALSE)
                 ELSE FALSE END AS copied_jsonl_body_matches,$new$;
 json_marker TEXT:=$old$'legacy_segment_matches', legacy_segment_matches$old$;
 json_replacement TEXT:=$new$'legacy_segment_matches', legacy_segment_matches, 'copied_jsonl_body_matches', copied_jsonl_body_matches$new$;
 gate_marker TEXT:=$old$                       hit -> 'legacy_segment_matches' = 'true'::JSONB
                       AND hit -> 'segment_matches' = 'true'::JSONB
                   )))$old$;
 gate_replacement TEXT:=$new$                       hit -> 'legacy_segment_matches' = 'true'::JSONB
                       AND hit -> 'segment_matches' = 'true'::JSONB
                   ) OR (
                       hit -> 'copied_jsonl_body_matches' = 'true'::JSONB
                   )))$new$;
 query_marker TEXT:=$old$    WITH candidate AS MATERIALIZED ($old$;
 query_replacement TEXT:=$new$    -- This function keeps its original SET row_security=off contract.
    -- The copied security-invoker view requires actual source RLS. SET LOCAL
    -- is contained by this existing function's SET frame on return or error.
    PERFORM set_config('row_security','on',true);
    WITH candidate AS MATERIALIZED ($new$;
 definition TEXT;new_hash BYTEA;index INTEGER;changed INTEGER;pin RECORD;expected JSONB;actual JSONB;lexical_pin BOOLEAN;
 expected_functions JSONB:=(SELECT functions FROM copied182_before);
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
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_source_segment_rank_candidates(bigint[],text)'::REGPROCEDURE),'UTF8')),'hex')
     IS DISTINCT FROM '2be95bf7178d3c293ccafa1b245fe33eec9d60df93a6d0761dcebccb37ce902d'
  OR encode(sha256(convert_to(pg_get_functiondef('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'::REGPROCEDURE),'UTF8')),'hex')
     IS DISTINCT FROM 'ff5f168c0c02533f74eb51ed1bb67d8e5e4ca1e304e2e1d9e5ef9d38ad3287fe' THEN
  RAISE EXCEPTION 'copied JSONL evidence requires unchanged181 rank readers';
 END IF;
 FOR index IN 1..cardinality(signatures) LOOP
  definition:=pg_get_functiondef(signatures[index]::REGPROCEDURE);
  IF encode(sha256(convert_to(definition,'UTF8')),'hex') IS DISTINCT FROM predecessors[index]
   OR (SELECT NOT prosecdef OR provolatile<>'s'
       FROM pg_proc WHERE oid=signatures[index]::REGPROCEDURE)
   OR (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1
   OR (length(definition)-length(replace(definition,json_marker,'')))/length(json_marker)<>1
   OR (length(definition)-length(replace(definition,gate_marker,'')))/length(gate_marker)<>1
   OR (length(definition)-length(replace(definition,query_marker,'')))/length(query_marker)<>1
   OR (length(definition)-length(replace(definition,'''mainrag.storage-v2.query-coverage.v4''','')))/length('''mainrag.storage-v2.query-coverage.v4''')<>1 THEN
   RAISE EXCEPTION 'copied body rank predecessor or boundary differs: %',signatures[index];
  END IF;
  SELECT EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_function_contract c
    WHERE c.signature=signatures[index]) INTO lexical_pin;
  IF lexical_pin AND NOT EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_function_contract c
    WHERE c.signature=signatures[index] AND c.definition_sha256=decode(predecessors[index],'hex')) THEN
   RAISE EXCEPTION 'copied body lexical predecessor pin differs';
  END IF;
  IF EXISTS(SELECT 1 FROM storage_v2_posting_conversion_contract c WHERE c.signature=signatures[index]
    AND c.definition_sha256<>decode(predecessors[index],'hex')) THEN
   RAISE EXCEPTION 'copied body posting predecessor pin differs';
  END IF;
  definition:=replace(definition,marker,replacement);
  definition:=replace(definition,json_marker,json_replacement);
  definition:=replace(definition,gate_marker,gate_replacement);
  definition:=replace(definition,query_marker,query_replacement);
  EXECUTE replace(definition,'''mainrag.storage-v2.query-coverage.v4''','''mainrag.storage-v2.query-coverage.v5''');
  new_hash:=sha256(convert_to(pg_get_functiondef(signatures[index]::REGPROCEDURE),'UTF8'));
  expected_functions:=jsonb_set(expected_functions,ARRAY[signatures[index]],to_jsonb(encode(new_hash,'hex')));
  UPDATE storage_v2_lexical_conversion_function_contract SET definition_sha256=new_hash
   WHERE signature=signatures[index] AND definition_sha256=decode(predecessors[index],'hex');
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>(CASE WHEN lexical_pin THEN 1 ELSE 0 END) THEN
   RAISE EXCEPTION 'copied body lexical predecessor CAS differs'; END IF;
  -- Advance only dependencies already present in the original audited ABI.
  UPDATE storage_v2_posting_conversion_contract SET definition_sha256=new_hash
   WHERE signature=signatures[index] AND definition_sha256=decode(predecessors[index],'hex');
 END LOOP;
 -- The evidence function need not be an existing lexical dependency. Keep
 -- the original sealed rows exactly unchanged when it is not already pinned.
 IF EXISTS(SELECT 1 FROM jsonb_array_elements((SELECT lexical FROM copied182_before)) item
    WHERE item->>'signature'=ANY(signatures)) THEN
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
  AND tgname='storage_v2_lexical_conversion_seal_immutable' AND tgenabled='O' AND tgtype=27
  AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
  RAISE EXCEPTION 'copied body lexical seal guard differs';
 END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 UPDATE storage_v2_lexical_conversion_contract_seal SET contract_sha256=sha256(convert_to(jsonb_build_object(
  'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),
  'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
 WHERE singleton AND to_jsonb(storage_v2_lexical_conversion_contract_seal)=(SELECT seal FROM copied182_before);
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'copied body lexical seal predecessor CAS differs'; END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 END IF;
 FOR pin IN SELECT 'posting' AS kind,value FROM jsonb_array_elements((SELECT posting FROM copied182_before))
  UNION ALL SELECT 'lexical',value FROM jsonb_array_elements((SELECT lexical FROM copied182_before)) LOOP
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
     FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f') IS DISTINCT FROM (SELECT authorities FROM copied182_before)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c) IS DISTINCT FROM (SELECT posting_relations FROM copied182_before)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c) IS DISTINCT FROM (SELECT lexical_relations FROM copied182_before)
  OR (SELECT to_jsonb(c)-'contract_sha256' FROM storage_v2_lexical_conversion_contract_seal c WHERE singleton) IS DISTINCT FROM (SELECT seal-'contract_sha256' FROM copied182_before)
  OR (SELECT count(*) FROM storage_v2_posting_conversion_contract)<>jsonb_array_length((SELECT posting FROM copied182_before))
  OR (SELECT count(*) FROM storage_v2_lexical_conversion_function_contract)<>jsonb_array_length((SELECT lexical FROM copied182_before)) THEN
  RAISE EXCEPTION 'copied body authority, function or catalog inventory differs';
 END IF;
END $copied_body$;
SELECT storage_v2_posting_conversion_require_operator();
SELECT storage_v2_require_lexical_conversion_operator();
COMMIT;
