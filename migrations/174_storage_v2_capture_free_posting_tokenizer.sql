-- Deduplicate local word parsing under one capture-free document scan.
-- Terms, UTF8 byte locators, posting choices and storage identities remain exact.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';

DO $capture_free_posting_tokenizer$
DECLARE
 producer_signature CONSTANT TEXT := 'storage_v2_put_search_document(text,text,bigint,text,text[])';
 predecessor CONSTANT TEXT := '9f2ba80adc1283fb49bc6434e710cc048af34a4df62977a1f1eae617f916023b';
 definition TEXT;marker TEXT;replacement TEXT;before_authority JSONB;changed INTEGER;
 lexical_pin BOOLEAN;
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user) OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'capture-free posting tokenizer migration requires the local administrative operator'
   USING ERRCODE='42501';
 END IF;
 -- Verify all existing consumer/authority contracts before advancing ONE producer.
 PERFORM storage_v2_posting_conversion_require_operator();
 PERFORM storage_v2_require_lexical_conversion_operator();
 LOCK TABLE storage_v2_posting_conversion_contract,storage_v2_lexical_conversion_function_contract
  IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 IF encode(sha256(convert_to(pg_get_functiondef(producer_signature::REGPROCEDURE),'UTF8')),'hex')
      IS DISTINCT FROM predecessor THEN
  RAISE EXCEPTION 'capture-free posting tokenizer producer predecessor differs';
 END IF;
 SELECT jsonb_build_object('oid',oid,'owner',proowner,'acl',proacl::TEXT,'configuration',proconfig,
    'security_definer',prosecdef,'arguments',proargnames,'volatility',provolatile,'parallel',proparallel)
  INTO before_authority FROM pg_proc WHERE oid=producer_signature::REGPROCEDURE;
 SELECT EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_function_contract c
   WHERE c.signature=producer_signature) INTO lexical_pin;
 IF lexical_pin AND NOT EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_function_contract c
    WHERE c.signature=producer_signature AND c.definition_sha256=decode(predecessor,'hex')) THEN
  RAISE EXCEPTION 'lexical producer predecessor pin differs';
 END IF;
 definition:=pg_get_functiondef(producer_signature::REGPROCEDURE);
 marker:=$captured_tokenizer$    WITH word_parts AS MATERIALIZED (
        SELECT p.parts[2] AS token,1::BIGINT AS word_count,
               COALESCE(sum(octet_length(p.parts[1])+octet_length(p.parts[2])) OVER(
                   ORDER BY p.n ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0)
                   +octet_length(p.parts[1])+1 AS byte_start
          FROM regexp_matches(v_normalized_text COLLATE "default",
                   '([^[:alnum:]_]*)([[:alnum:]_]+)','g') WITH ORDINALITY p(parts,n)
    ), whitespace_parts AS MATERIALIZED (
        SELECT p.parts[2] AS token,0::BIGINT AS word_count,
               COALESCE(sum(octet_length(p.parts[1])+octet_length(p.parts[2])) OVER(
                   ORDER BY p.n ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0)
                   +octet_length(p.parts[1])+1 AS byte_start
          FROM regexp_matches(v_normalized_text COLLATE "default",
                   '([[:space:]]*)([^[:space:]]+)','g') WITH ORDINALITY p(parts,n)
    ), searchable_tokens AS (
        SELECT * FROM word_parts
        UNION ALL SELECT * FROM whitespace_parts
         WHERE token !~ '^[[:alnum:]_]+$' AND token ~ '[[:alnum:]_]'
    ), frequency AS (
        SELECT token,count(*)::BIGINT AS frequency,sum(word_count)::BIGINT AS word_count,
               min(byte_start)::INTEGER AS byte_start,octet_length(token) AS byte_length
          FROM searchable_tokens GROUP BY token
    )
$captured_tokenizer$;
 replacement:=$capture_free_tokenizer$    -- One capture-free whole-document whitespace scan. Repeated normalized
    -- whitespace tokens reuse exact local word parsing and byte offsets.
    -- The existing captured word grammar runs only on DISTINCT mixed tokens;
    -- pure words require no local parser. No ASCII/host-language tokenizer.
    WITH raw_runs AS MATERIALIZED (
        SELECT p.parts[1] AS complete_run,p.n
          FROM regexp_matches(v_normalized_text COLLATE "default",
                   '[[:space:]]*[^[:space:]]+','g') WITH ORDINALITY p(parts,n)
    ), unique_runs AS MATERIALIZED (
        SELECT DISTINCT complete_run COLLATE "C" AS complete_run FROM raw_runs
    ), trimmed_runs AS MATERIALIZED (
        SELECT complete_run,
               regexp_replace(complete_run COLLATE "default",'^[[:space:]]*','','')
                   COLLATE "C" AS token,
               octet_length(complete_run) AS complete_bytes
          FROM unique_runs
    ), run_widths AS MATERIALIZED (
        SELECT complete_run,token,complete_bytes,
               complete_bytes-octet_length(token) AS prefix_bytes
          FROM trimmed_runs
    ), positioned AS (
        SELECT widths.token,
               COALESCE(sum(widths.complete_bytes) OVER(
                   ORDER BY raw.n ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0)
                   +widths.prefix_bytes+1 AS byte_start
          FROM raw_runs raw JOIN run_widths widths
            ON raw.complete_run COLLATE "C"=widths.complete_run
    ), whitespace_tokens AS MATERIALIZED (
        -- C equality shares parsing only for byte-identical normalized tokens.
        SELECT token COLLATE "C" AS token,count(*)::BIGINT AS frequency,
               min(byte_start) AS byte_start
          FROM positioned GROUP BY token COLLATE "C"
    ), classified AS MATERIALIZED (
        SELECT token,frequency,byte_start,
               token COLLATE "default" ~ '^[[:alnum:]_]+$' AS pure_word,
               token COLLATE "default" ~ '[[:alnum:]_]' AS contains_word
          FROM whitespace_tokens
    ), mixed_tokens AS MATERIALIZED (
        SELECT token,frequency,byte_start FROM classified
         WHERE NOT pure_word AND contains_word
    ), local_words AS (
        SELECT p.parts[2] COLLATE "default" AS token,mixed.frequency,
               mixed.byte_start
                   +COALESCE(sum(octet_length(p.parts[1])+octet_length(p.parts[2])) OVER(
                       PARTITION BY mixed.token COLLATE "C" ORDER BY p.n
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0)
                   +octet_length(p.parts[1]) AS byte_start
          FROM mixed_tokens mixed CROSS JOIN LATERAL
               regexp_matches(mixed.token COLLATE "default",
                   '([^[:alnum:]_]*)([[:alnum:]_]+)','g') WITH ORDINALITY p(parts,n)
    ), contributions AS (
        SELECT token COLLATE "default" AS token,frequency,
               frequency AS word_count,byte_start FROM classified WHERE pure_word
        UNION ALL
        SELECT token COLLATE "default",frequency,frequency AS word_count,byte_start
          FROM local_words
        UNION ALL
        SELECT token COLLATE "default",frequency,0::BIGINT AS word_count,byte_start
          FROM mixed_tokens
    ), frequency AS (
        -- Return to the original default-collation frequency grouping. Every
        -- local word occurrence contributes its parent whitespace frequency;
        -- minimum base plus local byte offset preserves global first position.
        SELECT token,sum(frequency)::BIGINT AS frequency,sum(word_count)::BIGINT AS word_count,
               min(byte_start)::INTEGER AS byte_start,octet_length(token) AS byte_length
          FROM contributions GROUP BY token
    )
$capture_free_tokenizer$;
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'capture-free posting tokenizer boundary differs';
 END IF;
 -- Compare the exact complete predecessor again immediately before DDL.
 IF encode(sha256(convert_to(pg_get_functiondef(producer_signature::REGPROCEDURE),'UTF8')),'hex')
      IS DISTINCT FROM predecessor THEN
  RAISE EXCEPTION 'capture-free posting tokenizer producer changed before replacement';
 END IF;
 EXECUTE replace(definition,marker,replacement);
 IF before_authority IS DISTINCT FROM (SELECT jsonb_build_object('oid',oid,'owner',proowner,
   'acl',proacl::TEXT,'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames,
   'volatility',provolatile,'parallel',proparallel) FROM pg_proc WHERE oid=producer_signature::REGPROCEDURE) THEN
  RAISE EXCEPTION 'capture-free posting tokenizer changed existing producer authority';
 END IF;
 UPDATE storage_v2_posting_conversion_contract c
  SET definition_sha256=sha256(convert_to(pg_get_functiondef(producer_signature::REGPROCEDURE),'UTF8'))
  WHERE c.signature=producer_signature AND c.definition_sha256=decode(predecessor,'hex');
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'exact one posting producer contract must advance';END IF;
 IF lexical_pin THEN
  -- Only if the existing lexical contract actually depends on this producer:
  -- advance that one pin and its aggregate seal, preserving all other pins.
  UPDATE storage_v2_lexical_conversion_function_contract c
   SET definition_sha256=sha256(convert_to(pg_get_functiondef(producer_signature::REGPROCEDURE),'UTF8'))
   WHERE c.signature=producer_signature AND c.definition_sha256=decode(predecessor,'hex');
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>1 OR NOT EXISTS(SELECT 1 FROM pg_trigger
      WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
        AND tgname='storage_v2_lexical_conversion_seal_immutable' AND tgenabled='O' AND tgtype=27
        AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
   RAISE EXCEPTION 'exact lexical producer contract/seal boundary differs';
  END IF;
  ALTER TABLE storage_v2_lexical_conversion_contract_seal
   DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
  UPDATE storage_v2_lexical_conversion_contract_seal SET contract_sha256=sha256(convert_to(jsonb_build_object(
    'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),
    'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c)
    )::TEXT,'UTF8')) WHERE singleton;
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>1 THEN RAISE EXCEPTION 'exact one lexical aggregate seal must advance';END IF;
  ALTER TABLE storage_v2_lexical_conversion_contract_seal
   ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 PERFORM storage_v2_require_lexical_conversion_operator();
END $capture_free_posting_tokenizer$;
COMMIT;
