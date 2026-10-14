-- Replace only the bounded posting-cost backreference test with a linear run scan.
-- Tokenizer, terms, frequencies, prefix diversity and representation choices stay exact.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';

DO $linear_posting_run_classifier$
DECLARE
 producer_signature CONSTANT TEXT := 'storage_v2_put_search_document(text,text,bigint,text,text[])';
 predecessor CONSTANT TEXT := 'e64938762b3e2bb180c46abc906ca2e2e783352f991149e3bab2f405ec4d3d8a';
 definition TEXT;marker TEXT;replacement TEXT;before_authority JSONB;
 before_functions JSONB;expected_functions JSONB;before_posting JSONB;before_lexical JSONB;before_seal JSONB;
 expected JSONB;actual JSONB;item RECORD;new_hash BYTEA;changed INTEGER;lexical_pin BOOLEAN;
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
  OR current_setting('role') NOT IN ('none',session_user) OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'linear posting classifier requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 PERFORM storage_v2_require_lexical_conversion_operator();
 LOCK TABLE storage_v2_posting_conversion_contract,storage_v2_lexical_conversion_function_contract
  IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 IF encode(sha256(convert_to(pg_get_functiondef(producer_signature::REGPROCEDURE),'UTF8')),'hex')
   IS DISTINCT FROM predecessor THEN RAISE EXCEPTION 'linear posting classifier predecessor differs';END IF;
 SELECT to_jsonb(p)-'prosrc' INTO before_authority FROM pg_proc p WHERE p.oid=producer_signature::REGPROCEDURE;
 SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex'))
  INTO before_functions FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f';
 SELECT jsonb_agg(to_jsonb(c) ORDER BY c.signature) INTO before_posting FROM storage_v2_posting_conversion_contract c;
 SELECT jsonb_agg(to_jsonb(c) ORDER BY c.signature) INTO before_lexical FROM storage_v2_lexical_conversion_function_contract c;
 SELECT to_jsonb(s) INTO STRICT before_seal FROM storage_v2_lexical_conversion_contract_seal s WHERE singleton;
 SELECT EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_function_contract WHERE signature=producer_signature) INTO lexical_pin;
 IF NOT EXISTS(SELECT 1 FROM storage_v2_posting_conversion_contract WHERE signature=producer_signature AND definition_sha256=decode(predecessor,'hex'))
  OR (lexical_pin AND NOT EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_function_contract
    WHERE signature=producer_signature AND definition_sha256=decode(predecessor,'hex'))) THEN
  RAISE EXCEPTION 'linear posting classifier predecessor dependency differs';
 END IF;
 definition:=pg_get_functiondef(producer_signature::REGPROCEDURE);
 marker:='    v_document storage_v2_search_document;';
 replacement:=$declarations$    v_document storage_v2_search_document;
    v_run_sample TEXT;
    v_run_character TEXT;
    v_run_previous TEXT;
    v_run_length INTEGER;
    v_run_reduced_length INTEGER;
    v_run_sample_length INTEGER;$declarations$;
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'linear posting classifier declaration boundary differs';END IF;
 definition:=replace(definition,marker,replacement);
 marker:=$old_classifier$        SELECT count(DISTINCT substring(convert_to(term,'UTF8') FROM 1 FOR 32)),
               coalesce(bool_or(length(regexp_replace(left(term,128)||right(term,128),
                    '(.)\1{3,}', '\1', 'g'))*2<length(left(term,128)||right(term,128))),FALSE)
          INTO v_long_prefix_count,v_repetitive_dictionary
          FROM unnest(v_terms,v_byte_lengths) item(term,byte_length)
         WHERE byte_length>16;$old_classifier$;
 replacement:=$new_classifier$        SELECT count(DISTINCT substring(convert_to(term,'UTF8') FROM 1 FOR 32))
          INTO v_long_prefix_count
          FROM unnest(v_terms,v_byte_lengths) item(term,byte_length)
         WHERE byte_length>16;
        -- Exact replacement-length predicate for (.)\1{3,} with global,
        -- case-sensitive, non-newline-sensitive PostgreSQL matching. Each
        -- maximal run of at least four identical codepoints becomes one.
        -- Split each existing <=256-codepoint sample once; never scan a body,
        -- substring by a growing character offset, truncate a term or parse it.
        v_repetitive_dictionary:=FALSE;
        FOR v_run_sample IN SELECT left(term,128)||right(term,128)
          FROM unnest(v_terms,v_byte_lengths) item(term,byte_length)
         WHERE byte_length>16 LOOP
            v_run_sample_length:=char_length(v_run_sample);
            IF v_run_sample_length>256 THEN
                RAISE EXCEPTION 'posting run sample exceeds its internal envelope';
            END IF;
            v_run_previous:=NULL;
            v_run_length:=0;
            v_run_reduced_length:=0;
            FOREACH v_run_character IN ARRAY string_to_array(v_run_sample,NULL) LOOP
                IF v_run_previous IS NULL OR
                   v_run_character COLLATE "C" IS DISTINCT FROM v_run_previous COLLATE "C" THEN
                    v_run_reduced_length:=v_run_reduced_length+
                        CASE WHEN v_run_length>=4 THEN 1 ELSE v_run_length END;
                    v_run_previous:=v_run_character;
                    v_run_length:=1;
                ELSE
                    v_run_length:=v_run_length+1;
                END IF;
            END LOOP;
            v_run_reduced_length:=v_run_reduced_length+
                CASE WHEN v_run_length>=4 THEN 1 ELSE v_run_length END;
            IF v_run_reduced_length*2<v_run_sample_length THEN
                v_repetitive_dictionary:=TRUE;
                EXIT;
            END IF;
        END LOOP;$new_classifier$;
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'linear posting classifier expression boundary differs';END IF;
 definition:=replace(definition,marker,replacement);
 IF encode(sha256(convert_to(pg_get_functiondef(producer_signature::REGPROCEDURE),'UTF8')),'hex')
  IS DISTINCT FROM predecessor THEN RAISE EXCEPTION 'linear posting classifier predecessor changed';END IF;
 EXECUTE definition;
 IF before_authority IS DISTINCT FROM (SELECT to_jsonb(p)-'prosrc' FROM pg_proc p WHERE p.oid=producer_signature::REGPROCEDURE) THEN
  RAISE EXCEPTION 'linear posting classifier function authority changed';END IF;
 new_hash:=sha256(convert_to(pg_get_functiondef(producer_signature::REGPROCEDURE),'UTF8'));
 UPDATE storage_v2_posting_conversion_contract SET definition_sha256=new_hash
  WHERE signature=producer_signature AND definition_sha256=decode(predecessor,'hex');
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'linear posting classifier posting dependency CAS differs';END IF;
 IF lexical_pin THEN
  UPDATE storage_v2_lexical_conversion_function_contract SET definition_sha256=new_hash
   WHERE signature=producer_signature AND definition_sha256=decode(predecessor,'hex');
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>1 OR NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
   AND tgname='storage_v2_lexical_conversion_seal_immutable' AND tgenabled='O' AND tgtype=27
   AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
   RAISE EXCEPTION 'linear posting classifier lexical dependency/seal differs';END IF;
  ALTER TABLE storage_v2_lexical_conversion_contract_seal DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
  UPDATE storage_v2_lexical_conversion_contract_seal SET contract_sha256=sha256(convert_to(jsonb_build_object(
   'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY c.signature) FROM storage_v2_lexical_conversion_function_contract c),
   'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY c.relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8')) WHERE singleton;
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>1 THEN RAISE EXCEPTION 'linear posting classifier lexical seal CAS differs';END IF;
  ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 END IF;
 FOR item IN SELECT 'posting' AS kind,p AS pin FROM jsonb_array_elements(before_posting) p
  UNION ALL SELECT 'lexical',p FROM jsonb_array_elements(before_lexical) p LOOP
  expected:=item.pin;
  IF expected->>'signature'=producer_signature THEN expected:=jsonb_set(expected,'{definition_sha256}',to_jsonb('\x'||encode(new_hash,'hex')));END IF;
  IF item.kind='posting' THEN SELECT to_jsonb(c) INTO actual FROM storage_v2_posting_conversion_contract c WHERE c.signature=item.pin->>'signature';
  ELSE SELECT to_jsonb(c) INTO actual FROM storage_v2_lexical_conversion_function_contract c WHERE c.signature=item.pin->>'signature';END IF;
  IF actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'unreviewed linear classifier dependency delta';END IF;
 END LOOP;
 expected_functions:=jsonb_set(before_functions,ARRAY[producer_signature],to_jsonb(encode(new_hash,'hex')));
 IF expected_functions IS DISTINCT FROM (SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex'))
    FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f')
  OR (SELECT count(*) FROM storage_v2_posting_conversion_contract)<>jsonb_array_length(before_posting)
  OR (SELECT count(*) FROM storage_v2_lexical_conversion_function_contract)<>jsonb_array_length(before_lexical)
  OR (SELECT to_jsonb(s)-'contract_sha256' FROM storage_v2_lexical_conversion_contract_seal s WHERE singleton)
    IS DISTINCT FROM before_seal-'contract_sha256' THEN
  RAISE EXCEPTION 'linear classifier public function/contract inventory differs';END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 PERFORM storage_v2_require_lexical_conversion_operator();
END $linear_posting_run_classifier$;
COMMIT;
