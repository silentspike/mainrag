-- Admit copied fragment body hits only after complete canonical file verification.
-- Both rank overloads and independent query evidence use the same layout/hash
-- proof; selected fragments still require their own original body predicate.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SELECT storage_v2_posting_conversion_require_operator();
SELECT storage_v2_require_lexical_conversion_operator();
LOCK TABLE storage_v2_posting_conversion_contract,
 storage_v2_lexical_conversion_function_contract,storage_v2_lexical_conversion_contract_seal
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;

CREATE TEMP TABLE copied183_before ON COMMIT DROP AS
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
  'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])',
  'storage_v2_candidate_query_evidence(bigint,bigint,text,text,bigint[],bigint[])'];
 predecessors CONSTANT TEXT[]:=ARRAY[
  '2be95bf7178d3c293ccafa1b245fe33eec9d60df93a6d0761dcebccb37ce902d',
  'ff5f168c0c02533f74eb51ed1bb67d8e5e4ca1e304e2e1d9e5ef9d38ad3287fe',
  'c42d99e03d5ef3bff8d703f2bb18df9d3eef65b76bce6a90d42b3922b97351ff'];
 rank_marker TEXT:=$old$copied_result AS MATERIALIZED ($old$;
 rank_replacement TEXT:=$new$fragment_targets AS MATERIALIZED (
        SELECT DISTINCT occurrence_row.source_id,occurrence_row.source_path,projection.legacy_file_hash
          FROM authorized_projection selected
          JOIN public.occurrence occurrence_row ON occurrence_row.id=selected.occurrence_id
          JOIN public.artifact_version artifact ON artifact.id=selected.artifact_version_id
          JOIN public.storage_v2_legacy_lexical_segment projection
            ON projection.occurrence_id=occurrence_row.id
           AND projection.source_id=occurrence_row.source_id
           AND projection.artifact_version_id=artifact.id
           AND projection.legacy_chunk_id=selected.legacy_chunk_id
         WHERE projection.fts_vector@@v_query
           AND encode(projection.legacy_file_hash,'hex')<>artifact.expected_content_hash
           AND public.storage_v2_can_access_source(occurrence_row.source_id,'read')
    ), fragment_meta AS MATERIALIZED (
        -- Missing/invalid members remain in the group and reject the file.
        SELECT target.*, sibling.id AS occurrence_id, artifact.id AS artifact_id,
               document.id AS document_id, artifact.expected_content_hash,
               artifact.byte_length, body.digest AS raw_body_digest,
               CASE WHEN jsonb_typeof(sibling.locator->'byte_start')='number'
                         AND sibling.locator->>'byte_start' ~ '^(0|[1-9][0-9]{0,17})$'
                    THEN (sibling.locator->>'byte_start')::BIGINT END AS byte_start,
               CASE WHEN jsonb_typeof(sibling.locator->'byte_end')='number'
                         AND sibling.locator->>'byte_end' ~ '^(0|[1-9][0-9]{0,17})$'
                    THEN (sibling.locator->>'byte_end')::BIGINT END AS byte_end,
               COALESCE(sibling.locator->'fragmented'='true'::JSONB
                 AND artifact.source_id=sibling.source_id
                 AND document.component_kind='node'
                 AND document.node_id=artifact.content_root_node_id
                 AND document.profile_id='mainrag.lexical-simple.v1'
                 AND body.digest_algorithm='sha256-v1'
                 AND body.logical_length=artifact.byte_length
                 AND node.logical_length=artifact.byte_length
                 AND octet_length(document.search_text)=artifact.byte_length
                 AND NOT EXISTS(SELECT 1 FROM public.storage_v2_search_view_document other
                                WHERE other.view_id=sibling.view_id AND other.ordinal<>0),FALSE) AS identities_valid
          FROM fragment_targets target
          JOIN public.occurrence sibling
            ON sibling.source_id=target.source_id AND sibling.source_path=target.source_path
           AND sibling.role='artifact' AND sibling.parent_occurrence_id IS NULL
          LEFT JOIN public.artifact_version artifact ON artifact.id=sibling.artifact_version_id
          LEFT JOIN public.storage_v2_search_view_document binding
            ON binding.view_id=sibling.view_id AND binding.ordinal=0
          LEFT JOIN public.storage_v2_search_document document ON document.id=binding.document_id
          LEFT JOIN public.content_node node ON node.id=artifact.content_root_node_id
          LEFT JOIN public.content_body body ON body.id=node.body_id
         WHERE sibling.id=ANY(p_occurrence_ids)
    ), fragment_order AS MATERIALIZED (
        SELECT fragment_meta.*,lag(byte_end) OVER (
            PARTITION BY source_id,source_path,legacy_file_hash
            ORDER BY fragment_meta.byte_start,fragment_meta.occurrence_id) AS previous_end
          FROM fragment_meta
    ), fragment_layout AS MATERIALIZED (
        SELECT source_id,source_path,legacy_file_hash,count(*) AS pieces,
               sum(byte_length::NUMERIC) AS total_bytes,
               COALESCE(bool_and(COALESCE(identities_valid AND byte_start>=0 AND byte_end>byte_start
                 AND byte_end-byte_start=byte_length
                 AND (previous_end=byte_start OR previous_end IS NULL AND byte_start=0),FALSE)),FALSE)
                 AND count(byte_start)=count(*) AND count(byte_end)=count(*) AS valid
          FROM fragment_order GROUP BY source_id,source_path,legacy_file_hash
    ), fragment_bounded AS MATERIALIZED (
        -- Bound each file by the retained-body window (8 MiB), and the
        -- complete statement by the existing SQL byte window (32 MiB),
        -- before loading canonical text. Larger files cannot starve small ones.
        -- Oversized or incomplete sets receive no additional admission.
        SELECT * FROM fragment_layout
         WHERE valid AND pieces>1 AND total_bytes BETWEEN 1 AND 8388608
           AND (SELECT sum(total_bytes) FROM fragment_layout
                 WHERE valid AND pieces>1 AND total_bytes BETWEEN 1 AND 8388608)<=33554432
    ), fragment_bytes AS MATERIALIZED (
        SELECT meta.*,convert_to(document.search_text,'UTF8') AS bytes
          FROM fragment_order meta JOIN fragment_bounded bounded
            USING(source_id,source_path,legacy_file_hash)
          JOIN public.storage_v2_search_document document ON document.id=meta.document_id
    ), fragment_verified AS MATERIALIZED (
        SELECT source_id,source_path,legacy_file_hash
          FROM fragment_bytes GROUP BY source_id,source_path,legacy_file_hash
        HAVING bool_and(octet_length(bytes)=byte_length
                 AND sha256(bytes)=raw_body_digest
                 AND encode(sha256(bytes),'hex')=expected_content_hash)
           AND sha256(string_agg(bytes,''::BYTEA ORDER BY fragment_bytes.byte_start,fragment_bytes.occurrence_id))=legacy_file_hash
    ), copied_result AS MATERIALIZED ($new$;
 evidence_marker TEXT:=$old$reference AS MATERIALIZED ($old$;
 evidence_replacement TEXT:=$new$fragment_targets AS MATERIALIZED (
        SELECT DISTINCT occurrence_row.source_id,occurrence_row.source_path,projection.legacy_file_hash
          FROM candidate selected
          JOIN public.occurrence occurrence_row ON occurrence_row.id=selected.id
          JOIN public.artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id
          JOIN public.storage_v2_legacy_lexical_segment projection
            ON projection.occurrence_id=occurrence_row.id
           AND projection.source_id=occurrence_row.source_id
           AND projection.artifact_version_id=artifact.id
         WHERE projection.fts_vector@@websearch_to_tsquery('simple',p_query)
           AND encode(projection.legacy_file_hash,'hex')<>artifact.expected_content_hash
    ), fragment_meta AS MATERIALIZED (
        -- Missing/invalid members remain in the group and reject the file.
        SELECT target.*, sibling.id AS occurrence_id, artifact.id AS artifact_id,
               document.id AS document_id, artifact.expected_content_hash,
               artifact.byte_length, body.digest AS raw_body_digest,
               CASE WHEN jsonb_typeof(sibling.locator->'byte_start')='number'
                         AND sibling.locator->>'byte_start' ~ '^(0|[1-9][0-9]{0,17})$'
                    THEN (sibling.locator->>'byte_start')::BIGINT END AS byte_start,
               CASE WHEN jsonb_typeof(sibling.locator->'byte_end')='number'
                         AND sibling.locator->>'byte_end' ~ '^(0|[1-9][0-9]{0,17})$'
                    THEN (sibling.locator->>'byte_end')::BIGINT END AS byte_end,
               COALESCE(sibling.locator->'fragmented'='true'::JSONB
                 AND artifact.source_id=sibling.source_id
                 AND document.component_kind='node'
                 AND document.node_id=artifact.content_root_node_id
                 AND document.profile_id='mainrag.lexical-simple.v1'
                 AND body.digest_algorithm='sha256-v1'
                 AND body.logical_length=artifact.byte_length
                 AND node.logical_length=artifact.byte_length
                 AND octet_length(document.search_text)=artifact.byte_length
                 AND NOT EXISTS(SELECT 1 FROM public.storage_v2_search_view_document other
                                WHERE other.view_id=sibling.view_id AND other.ordinal<>0),FALSE) AS identities_valid
          FROM fragment_targets target
          JOIN public.occurrence sibling
            ON sibling.source_id=target.source_id AND sibling.source_path=target.source_path
           AND sibling.role='artifact' AND sibling.parent_occurrence_id IS NULL
          LEFT JOIN public.artifact_version artifact ON artifact.id=sibling.artifact_version_id
          LEFT JOIN public.storage_v2_search_view_document binding
            ON binding.view_id=sibling.view_id AND binding.ordinal=0
          LEFT JOIN public.storage_v2_search_document document ON document.id=binding.document_id
          LEFT JOIN public.content_node node ON node.id=artifact.content_root_node_id
          LEFT JOIN public.content_body body ON body.id=node.body_id
         WHERE EXISTS(SELECT 1 FROM public.generation_item_version membership
             WHERE membership.source_id=p_source_id AND membership.source_item_id=artifact.item_id
               AND membership.artifact_version_id=artifact.id
               AND membership.valid_from_seq<=v_generation.generation_seq
               AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq>v_generation.generation_seq))
    ), fragment_order AS MATERIALIZED (
        SELECT fragment_meta.*,lag(byte_end) OVER (
            PARTITION BY source_id,source_path,legacy_file_hash
            ORDER BY fragment_meta.byte_start,fragment_meta.occurrence_id) AS previous_end
          FROM fragment_meta
    ), fragment_layout AS MATERIALIZED (
        SELECT source_id,source_path,legacy_file_hash,count(*) AS pieces,
               sum(byte_length::NUMERIC) AS total_bytes,
               COALESCE(bool_and(COALESCE(identities_valid AND byte_start>=0 AND byte_end>byte_start
                 AND byte_end-byte_start=byte_length
                 AND (previous_end=byte_start OR previous_end IS NULL AND byte_start=0),FALSE)),FALSE)
                 AND count(byte_start)=count(*) AND count(byte_end)=count(*) AS valid
          FROM fragment_order GROUP BY source_id,source_path,legacy_file_hash
    ), fragment_bounded AS MATERIALIZED (
        -- Bound each file by the retained-body window (8 MiB), and the
        -- complete statement by the existing SQL byte window (32 MiB),
        -- before loading canonical text. Larger files cannot starve small ones.
        -- Oversized or incomplete sets receive no additional admission.
        SELECT * FROM fragment_layout
         WHERE valid AND pieces>1 AND total_bytes BETWEEN 1 AND 8388608
           AND (SELECT sum(total_bytes) FROM fragment_layout
                 WHERE valid AND pieces>1 AND total_bytes BETWEEN 1 AND 8388608)<=33554432
    ), fragment_bytes AS MATERIALIZED (
        SELECT meta.*,convert_to(document.search_text,'UTF8') AS bytes
          FROM fragment_order meta JOIN fragment_bounded bounded
            USING(source_id,source_path,legacy_file_hash)
          JOIN public.storage_v2_search_document document ON document.id=meta.document_id
    ), fragment_verified AS MATERIALIZED (
        SELECT source_id,source_path,legacy_file_hash
          FROM fragment_bytes GROUP BY source_id,source_path,legacy_file_hash
        HAVING bool_and(octet_length(bytes)=byte_length
                 AND sha256(bytes)=raw_body_digest
                 AND encode(sha256(bytes),'hex')=expected_content_hash)
           AND sha256(string_agg(bytes,''::BYTEA ORDER BY fragment_bytes.byte_start,fragment_bytes.occurrence_id))=legacy_file_hash
    ), reference AS MATERIALIZED ($new$;
 rank_fence TEXT:=$old$literal_projection.legacy_file_hash=decode(literal_artifact.expected_content_hash,'hex')$old$;
 rank_fence_replacement TEXT:=$new$(literal_projection.legacy_file_hash=decode(literal_artifact.expected_content_hash,'hex')
                OR EXISTS(SELECT 1 FROM fragment_verified verified
                    JOIN fragment_bytes selected_fragment
                      USING(source_id,source_path,legacy_file_hash)
                    WHERE selected_fragment.occurrence_id=literal_occurrence.id
                      AND selected_fragment.artifact_id=literal_artifact.id
                      AND selected_fragment.document_id=literal_document.id
                      AND verified.source_id=literal_occurrence.source_id
                      AND verified.source_path=literal_occurrence.source_path
                      AND verified.legacy_file_hash=literal_projection.legacy_file_hash))$new$;
 evidence_fence TEXT:=$old$copied_projection.legacy_file_hash=decode(copied_artifact.expected_content_hash,'hex')$old$;
 evidence_fence_replacement TEXT:=$new$(copied_projection.legacy_file_hash=decode(copied_artifact.expected_content_hash,'hex')
                       OR EXISTS(SELECT 1 FROM fragment_verified verified
                           JOIN fragment_bytes selected_fragment
                             USING(source_id,source_path,legacy_file_hash)
                           WHERE selected_fragment.occurrence_id=copied_occurrence.id
                             AND selected_fragment.artifact_id=copied_artifact.id
                             AND selected_fragment.document_id=copied_document.id
                             AND verified.source_id=copied_occurrence.source_id
                             AND verified.source_path=copied_occurrence.source_path
                             AND verified.legacy_file_hash=copied_projection.legacy_file_hash))$new$;
 marker TEXT;replacement TEXT;fence TEXT;fence_replacement TEXT;
 definition TEXT;new_hash BYTEA;index INTEGER;changed INTEGER;pin RECORD;expected JSONB;actual JSONB;lexical_pin BOOLEAN;
 expected_functions JSONB:=(SELECT functions FROM copied183_before);
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
  IF index<=2 THEN
   marker:=rank_marker;replacement:=rank_replacement;
   fence:=rank_fence;fence_replacement:=rank_fence_replacement;
  ELSE
   marker:=evidence_marker;replacement:=evidence_replacement;
   fence:=evidence_fence;fence_replacement:=evidence_fence_replacement;
  END IF;
  definition:=pg_get_functiondef(signatures[index]::REGPROCEDURE);
  IF encode(sha256(convert_to(definition,'UTF8')),'hex') IS DISTINCT FROM predecessors[index]
   OR (SELECT NOT prosecdef OR provolatile<>'s'
       FROM pg_proc WHERE oid=signatures[index]::REGPROCEDURE)
   OR (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1
   OR (length(definition)-length(replace(definition,fence,'')))/length(fence)<>1 THEN
   RAISE EXCEPTION 'copied body rank predecessor or boundary differs: %',signatures[index];
  END IF;
  SELECT EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_function_contract c
    WHERE c.signature=signatures[index]) INTO lexical_pin;
  IF (index<=2 AND NOT lexical_pin) OR (lexical_pin AND NOT EXISTS(
    SELECT 1 FROM storage_v2_lexical_conversion_function_contract c
     WHERE c.signature=signatures[index] AND c.definition_sha256=decode(predecessors[index],'hex'))) THEN
   RAISE EXCEPTION 'copied fragment lexical predecessor pin differs';
  END IF;
  IF EXISTS(SELECT 1 FROM storage_v2_posting_conversion_contract c
    WHERE c.signature=signatures[index] AND c.definition_sha256<>decode(predecessors[index],'hex')) THEN
   RAISE EXCEPTION 'copied fragment posting predecessor pin differs';
  END IF;
  EXECUTE replace(replace(definition,marker,replacement),fence,fence_replacement);
  new_hash:=sha256(convert_to(pg_get_functiondef(signatures[index]::REGPROCEDURE),'UTF8'));
  expected_functions:=jsonb_set(expected_functions,ARRAY[signatures[index]],to_jsonb(encode(new_hash,'hex')));
  UPDATE storage_v2_lexical_conversion_function_contract SET definition_sha256=new_hash
   WHERE signature=signatures[index] AND definition_sha256=decode(predecessors[index],'hex');
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>(CASE WHEN lexical_pin THEN 1 ELSE 0 END) THEN
   RAISE EXCEPTION 'copied fragment lexical predecessor CAS differs'; END IF;
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
 WHERE singleton AND to_jsonb(storage_v2_lexical_conversion_contract_seal)=(SELECT seal FROM copied183_before);
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'copied body lexical seal predecessor CAS differs'; END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 FOR pin IN SELECT 'posting' AS kind,value FROM jsonb_array_elements((SELECT posting FROM copied183_before))
  UNION ALL SELECT 'lexical',value FROM jsonb_array_elements((SELECT lexical FROM copied183_before)) LOOP
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
     FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f') IS DISTINCT FROM (SELECT authorities FROM copied183_before)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c) IS DISTINCT FROM (SELECT posting_relations FROM copied183_before)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c) IS DISTINCT FROM (SELECT lexical_relations FROM copied183_before)
  OR (SELECT to_jsonb(c)-'contract_sha256' FROM storage_v2_lexical_conversion_contract_seal c WHERE singleton) IS DISTINCT FROM (SELECT seal-'contract_sha256' FROM copied183_before)
  OR (SELECT count(*) FROM storage_v2_posting_conversion_contract)<>jsonb_array_length((SELECT posting FROM copied183_before))
  OR (SELECT count(*) FROM storage_v2_lexical_conversion_function_contract)<>jsonb_array_length((SELECT lexical FROM copied183_before)) THEN
  RAISE EXCEPTION 'copied body authority, function or catalog inventory differs';
 END IF;
END $copied_body$;
SELECT storage_v2_posting_conversion_require_operator();
SELECT storage_v2_require_lexical_conversion_operator();
COMMIT;
