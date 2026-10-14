-- Bound local token Window keys and seek lexical contexts by their existing PK.
-- Exact terms, frequencies, locators, weighted windows and authority stay intact.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';

DO $bounded_posting_token_keys$
DECLARE
 signatures CONSTANT TEXT[]:=ARRAY[
  'storage_v2_put_search_document(text,text,bigint,text,text[])',
  'storage_v2_begin_lexical_document_context(bigint,bigint,bigint,integer)',
  'storage_v2_require_lexical_document_context(bigint,bigint)'];
 predecessors CONSTANT TEXT[]:=ARRAY[
  '9d4f6a28b1e681df79954e2e88a677fe30f546b1c8654f77fef09a943b0543d5',
  'e3a9c3fedc04a954e3e82e48a25ebcc37a5a22168e9064c1c75227a4f09c5329',
  '666191ce0e26dc609600829b2aa86729ebec1cbcbdf30a4ee40b622ae5e943a2'];
 authorities JSONB[]:=ARRAY[]::JSONB[];old_posting JSONB;old_lexical JSONB;
 old_seal JSONB;current_pin JSONB;expected JSONB;item RECORD;
 definition TEXT;marker TEXT;replacement TEXT;new_hash BYTEA;
 index INTEGER;changed INTEGER;lexical_changed INTEGER:=0;function_count BIGINT;
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
  OR current_setting('role') NOT IN ('none',session_user) OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'bounded token keys migration requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 PERFORM storage_v2_require_lexical_conversion_operator();
 LOCK TABLE storage_v2_posting_conversion_contract,storage_v2_lexical_conversion_function_contract
  IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 SELECT count(*) INTO function_count FROM pg_proc WHERE pronamespace='public'::REGNAMESPACE AND prokind='f';
 SELECT jsonb_agg(to_jsonb(c) ORDER BY c.signature) INTO old_posting FROM storage_v2_posting_conversion_contract c;
 SELECT jsonb_agg(to_jsonb(c) ORDER BY c.signature) INTO old_lexical FROM storage_v2_lexical_conversion_function_contract c;
 SELECT to_jsonb(s) INTO STRICT old_seal FROM storage_v2_lexical_conversion_contract_seal s WHERE singleton;
 -- This validated immutable FK makes the new source_item predicate redundant:
 -- the occurrence's artifact already equals the ingest item's artifact.
 IF NOT EXISTS(SELECT 1 FROM pg_constraint c WHERE c.contype='f' AND c.convalidated
  AND c.conrelid='storage_v2_ingest_run_item'::REGCLASS AND c.confrelid='artifact_version'::REGCLASS
  AND c.conkey=ARRAY(SELECT a.attnum FROM unnest(ARRAY['artifact_version_id','source_item_id','source_id'])
   WITH ORDINALITY n(name,ordinal) JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attname=n.name ORDER BY n.ordinal)
  AND c.confkey=ARRAY(SELECT a.attnum FROM unnest(ARRAY['id','item_id','source_id'])
   WITH ORDINALITY n(name,ordinal) JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attname=n.name ORDER BY n.ordinal)) THEN
  RAISE EXCEPTION 'validated exact artifact/source-item/source foreign key required';
 END IF;
 FOR index IN 1..cardinality(signatures) LOOP
  IF encode(sha256(convert_to(pg_get_functiondef(signatures[index]::REGPROCEDURE),'UTF8')),'hex')
      IS DISTINCT FROM predecessors[index] THEN RAISE EXCEPTION 'bounded token/context predecessor differs: %',signatures[index];END IF;
  authorities:=array_append(authorities,(SELECT to_jsonb(p)-'prosrc' FROM pg_proc p WHERE oid=signatures[index]::REGPROCEDURE));
  FOR item IN SELECT to_jsonb(c) AS pin FROM storage_v2_posting_conversion_contract c WHERE c.signature=signatures[index]
    UNION ALL SELECT to_jsonb(c) FROM storage_v2_lexical_conversion_function_contract c WHERE c.signature=signatures[index] LOOP
   IF item.pin->>'definition_sha256' IS DISTINCT FROM ('\x'||predecessors[index]) THEN
    RAISE EXCEPTION 'bounded token/context predecessor dependency pin differs';END IF;
  END LOOP;
 END LOOP;
 IF NOT EXISTS(SELECT 1 FROM storage_v2_posting_conversion_contract c WHERE c.signature=signatures[1]) THEN
  RAISE EXCEPTION 'existing posting producer dependency required';END IF;

 FOR index IN 1..cardinality(signatures) LOOP
  definition:=pg_get_functiondef(signatures[index]::REGPROCEDURE);
  IF encode(sha256(convert_to(definition,'UTF8')),'hex') IS DISTINCT FROM predecessors[index] THEN
   RAISE EXCEPTION 'bounded token/context predecessor changed before replacement';END IF;
  IF index=1 THEN
   marker:=$old_mixed$    ), mixed_tokens AS MATERIALIZED (
        SELECT token,frequency,byte_start FROM classified
         WHERE NOT pure_word AND contains_word
$old_mixed$;
   replacement:=$new_mixed$    ), mixed_tokens AS MATERIALIZED (
        -- One private integer partition key per byte-identical parent token.
        -- Do not copy a possibly document-sized token into every local word's
        -- Window sort tuple. These IDs are scratch only and never persisted.
        SELECT token,frequency,byte_start,row_number() OVER () AS mixed_id FROM classified
         WHERE NOT pure_word AND contains_word
$new_mixed$;
   IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
    RAISE EXCEPTION 'bounded mixed-token parent boundary differs';END IF;
   definition:=replace(definition,marker,replacement);
   marker:='PARTITION BY mixed.token COLLATE "C" ORDER BY p.n';
   IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
    RAISE EXCEPTION 'bounded mixed-token window boundary differs';END IF;
   definition:=replace(definition,marker,'PARTITION BY mixed.mixed_id ORDER BY p.n');
  ELSE
   marker:='JOIN public.artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id';
   IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
    RAISE EXCEPTION 'bounded lexical context artifact boundary differs';END IF;
   definition:=replace(definition,marker,marker||' AND item.source_item_id=artifact.item_id');
  END IF;
  EXECUTE definition;
  IF authorities[index] IS DISTINCT FROM (SELECT to_jsonb(p)-'prosrc' FROM pg_proc p WHERE oid=signatures[index]::REGPROCEDURE) THEN
   RAISE EXCEPTION 'bounded token/context function authority changed';END IF;
  new_hash:=sha256(convert_to(pg_get_functiondef(signatures[index]::REGPROCEDURE),'UTF8'));
  -- Advance only dependencies that existed before this migration. Context
  -- functions are not added to a consumer contract that never depended on them.
  UPDATE storage_v2_posting_conversion_contract c SET definition_sha256=new_hash
   WHERE c.signature=signatures[index] AND c.definition_sha256=decode(predecessors[index],'hex');
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>(SELECT count(*) FROM jsonb_array_elements(old_posting) p WHERE p->>'signature'=signatures[index]) THEN
   RAISE EXCEPTION 'bounded token/context posting dependency CAS differs';END IF;
  UPDATE storage_v2_lexical_conversion_function_contract c SET definition_sha256=new_hash
   WHERE c.signature=signatures[index] AND c.definition_sha256=decode(predecessors[index],'hex');
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>(SELECT count(*) FROM jsonb_array_elements(old_lexical) p WHERE p->>'signature'=signatures[index]) THEN
   RAISE EXCEPTION 'bounded token/context lexical dependency CAS differs';END IF;
  lexical_changed:=lexical_changed+changed;
 END LOOP;
 IF lexical_changed>0 THEN
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
   AND tgname='storage_v2_lexical_conversion_seal_immutable' AND tgenabled='O' AND tgtype=27
   AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
   RAISE EXCEPTION 'bounded token/context lexical seal guard differs';END IF;
  ALTER TABLE storage_v2_lexical_conversion_contract_seal DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
  UPDATE storage_v2_lexical_conversion_contract_seal SET contract_sha256=sha256(convert_to(jsonb_build_object(
   'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY c.signature) FROM storage_v2_lexical_conversion_function_contract c),
   'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY c.relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8')) WHERE singleton;
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>1 THEN RAISE EXCEPTION 'bounded token/context lexical seal CAS differs';END IF;
  ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 END IF;
 -- Compare every old pin including its unchanged authority/scope fields. No
 -- blanket registration, pin removal, owner change or relation re-signing.
 FOR item IN SELECT 'posting' AS kind,p AS pin FROM jsonb_array_elements(old_posting) p
  UNION ALL SELECT 'lexical',p FROM jsonb_array_elements(old_lexical) p LOOP
  expected:=item.pin;
  IF expected->>'signature'=ANY(signatures) THEN
   expected:=jsonb_set(expected,'{definition_sha256}',to_jsonb('\x'||encode(sha256(convert_to(
    pg_get_functiondef((expected->>'signature')::REGPROCEDURE),'UTF8')),'hex')));
  END IF;
  IF item.kind='posting' THEN
   SELECT to_jsonb(c) INTO current_pin FROM storage_v2_posting_conversion_contract c WHERE c.signature=item.pin->>'signature';
  ELSE
   SELECT to_jsonb(c) INTO current_pin FROM storage_v2_lexical_conversion_function_contract c WHERE c.signature=item.pin->>'signature';
  END IF;
  IF current_pin IS DISTINCT FROM expected THEN RAISE EXCEPTION 'unreviewed token/context dependency delta';END IF;
 END LOOP;
 IF (SELECT count(*) FROM storage_v2_posting_conversion_contract)<>jsonb_array_length(old_posting)
  OR (SELECT count(*) FROM storage_v2_lexical_conversion_function_contract)<>jsonb_array_length(old_lexical)
  OR (SELECT to_jsonb(s)-'contract_sha256' FROM storage_v2_lexical_conversion_contract_seal s WHERE singleton)
     IS DISTINCT FROM old_seal-'contract_sha256'
  OR (SELECT count(*) FROM pg_proc WHERE pronamespace='public'::REGNAMESPACE AND prokind='f')<>function_count THEN
  RAISE EXCEPTION 'bounded token/context function or contract inventory differs';END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 PERFORM storage_v2_require_lexical_conversion_operator();
END $bounded_posting_token_keys$;
COMMIT;
