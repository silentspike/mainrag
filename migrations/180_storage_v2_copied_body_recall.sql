-- Preserve copied whole-file ranks for literal body terms whose PostgreSQL
-- parser token differs at an extraction boundary. Context-only and fragment
-- projections retain their existing canonical/segment admission conditions.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SELECT storage_v2_posting_conversion_require_operator();
SELECT storage_v2_require_lexical_conversion_operator();
LOCK TABLE storage_v2_posting_conversion_contract,
 storage_v2_lexical_conversion_function_contract,storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

CREATE TEMP TABLE copied180_before ON COMMIT DROP AS
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
  'de3abad48566b7e35a69376d9f35e5c4d27065272d3b655d6dd9416dcc51e1b2',
  '4a16635c85d593ff01f3d83aa1bce8202552e2be944e3e469e8fb59621e62521'];
 marker TEXT:=$old$        OR storage_v2_source_legacy_segment_matches(projection.occurrence_id,p_query)
    ), matching_legacy AS MATERIALIZED$old$;
 replacement TEXT:=$new$        OR storage_v2_source_legacy_segment_matches(projection.occurrence_id,p_query)
        OR CASE WHEN octet_length(p_query) BETWEEN 1 AND 128
                     AND p_query ~ '^[[:alnum:]_]+$'
                     AND lower(p_query) NOT IN ('and','or','not') THEN EXISTS (
            -- A copied vector alone can match an extracted context. Require
            -- the SAME complete file plus independent canonical literal body
            -- support; never infer this permission from a fragment's path.
            SELECT 1 FROM public.storage_v2_search_document literal_document
            JOIN public.artifact_version literal_artifact
              ON literal_artifact.id=projection.artifact_version_id
             AND literal_document.node_id=literal_artifact.content_root_node_id
            JOIN public.occurrence literal_occurrence
              ON literal_occurrence.id=projection.occurrence_id
             AND literal_occurrence.artifact_version_id=literal_artifact.id
             AND literal_occurrence.source_id=literal_artifact.source_id
            JOIN public.storage_v2_legacy_lexical_segment literal_projection
              ON literal_projection.occurrence_id=literal_occurrence.id
             AND literal_projection.source_id=literal_occurrence.source_id
             AND literal_projection.artifact_version_id=literal_artifact.id
             AND literal_projection.legacy_chunk_id=projection.legacy_chunk_id
            WHERE literal_document.id=projection.canonical_document_id
              AND literal_document.component_kind='node'
              AND literal_projection.legacy_file_hash=decode(literal_artifact.expected_content_hash,'hex')
              AND literal_projection.fts_vector@@v_query
              AND public.storage_v2_literal_term_count(literal_document.search_text,p_query)>0
        ) ELSE FALSE END
    ), matching_legacy AS MATERIALIZED$new$;
 definition TEXT;new_hash BYTEA;index INTEGER;changed INTEGER;pin RECORD;expected JSONB;actual JSONB;
 expected_functions JSONB:=(SELECT functions FROM copied180_before);
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
 WHERE singleton AND to_jsonb(storage_v2_lexical_conversion_contract_seal)=(SELECT seal FROM copied180_before);
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'copied body lexical seal predecessor CAS differs'; END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 FOR pin IN SELECT 'posting' AS kind,value FROM jsonb_array_elements((SELECT posting FROM copied180_before))
  UNION ALL SELECT 'lexical',value FROM jsonb_array_elements((SELECT lexical FROM copied180_before)) LOOP
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
     FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f') IS DISTINCT FROM (SELECT authorities FROM copied180_before)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c) IS DISTINCT FROM (SELECT posting_relations FROM copied180_before)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c) IS DISTINCT FROM (SELECT lexical_relations FROM copied180_before)
  OR (SELECT to_jsonb(c)-'contract_sha256' FROM storage_v2_lexical_conversion_contract_seal c WHERE singleton) IS DISTINCT FROM (SELECT seal-'contract_sha256' FROM copied180_before)
  OR (SELECT count(*) FROM storage_v2_posting_conversion_contract)<>jsonb_array_length((SELECT posting FROM copied180_before))
  OR (SELECT count(*) FROM storage_v2_lexical_conversion_function_contract)<>jsonb_array_length((SELECT lexical FROM copied180_before)) THEN
  RAISE EXCEPTION 'copied body authority, function or catalog inventory differs';
 END IF;
END $copied_body$;
SELECT storage_v2_posting_conversion_require_operator();
SELECT storage_v2_require_lexical_conversion_operator();
COMMIT;
