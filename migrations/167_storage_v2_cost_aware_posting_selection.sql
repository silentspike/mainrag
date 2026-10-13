-- Select new posting storage from existing exact arrays, not body length.
-- This conservative uncompressed cost admission is not a physical-saving proof.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';

DO $posting_cost_admission$
DECLARE
 producer_signature CONSTANT TEXT := 'storage_v2_put_search_document(text,text,bigint,text,text[])';
 predecessor CONSTANT TEXT := '02d86dffca99c84b336f532c1c203d5ceec0bd129f41ee8f1a089604ea3fe9d9';
 definition TEXT;marker TEXT;replacement TEXT;before_authority JSONB;changed INTEGER;
 lexical_pin BOOLEAN;
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user) OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'posting cost admission migration requires the local administrative operator'
   USING ERRCODE='42501';
 END IF;
 -- Verify all existing consumer/authority contracts before advancing ONE producer.
 PERFORM storage_v2_posting_conversion_require_operator();
 PERFORM storage_v2_require_lexical_conversion_operator();
 LOCK TABLE storage_v2_posting_conversion_contract,storage_v2_lexical_conversion_function_contract
  IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 IF encode(sha256(convert_to(pg_get_functiondef(producer_signature::REGPROCEDURE),'UTF8')),'hex')
      IS DISTINCT FROM predecessor THEN
  RAISE EXCEPTION 'posting cost admission producer predecessor differs';
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
 marker:='    v_document storage_v2_search_document;';
 replacement:=$declarations$    v_document storage_v2_search_document;
    v_compact_model_bytes BIGINT;
    v_byte_model_bytes BIGINT;
    v_model_blocks BIGINT;
    v_long_term_count BIGINT;
    v_long_prefix_count BIGINT;
    v_repetitive_dictionary BOOLEAN;
    v_prefer_byte BOOLEAN;$declarations$;
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'posting cost admission declaration boundary differs';
 END IF;
 definition:=replace(definition,marker,replacement);
 marker:=$old_choice$    -- Small documents retain the established compact representation. Large
    -- new documents retain bounded short terms and exact byte locators for all terms.
    IF octet_length(p_search_text)>=262144 THEN$old_choice$;
 replacement:=$adaptive_choice$    -- Reuse the complete arrays produced by the ONE existing tokenizer pass.
    -- Fresh array datums below are uncompressed: pg_column_size includes array
    -- headers, element headers/alignment, and the cached-term NULL bitmap.
    -- Each 256-term block also includes fixed heap tuple/line-pointer/key
    -- alignment reserves (48 bytes COMPACT, 56 BYTE including the threshold).
    -- Frequency arrays and a conservative fingerprint array upper bound are
    -- common to both; BYTE additionally pays both complete int32 locators.
    -- No staging tables, body rewrite, second tokenizer or extra SHA pass.
    WITH model_input AS MATERIALIZED (
        SELECT item.term,item.frequency,item.byte_start,item.byte_length,item.ordinal,
               (item.ordinal-1)/256 AS block_order
          FROM unnest(v_terms,v_frequencies,v_byte_starts,v_byte_lengths)
               WITH ORDINALITY item(term,frequency,byte_start,byte_length,ordinal)
    ), model_arrays AS (
        SELECT block_order,count(*)::BIGINT AS term_count,
               array_agg(term ORDER BY ordinal) AS complete_terms,
               array_agg(CASE WHEN byte_length<=16 THEN term ELSE NULL END ORDER BY ordinal) AS cached_terms,
               array_agg(frequency ORDER BY ordinal) AS frequencies,
               array_agg(byte_start ORDER BY ordinal) AS starts,
               array_agg(byte_length ORDER BY ordinal) AS lengths
          FROM model_input GROUP BY block_order
    )
    SELECT coalesce(sum(48+pg_column_size(complete_terms)+pg_column_size(frequencies)
                            +24+4*term_count),0)::BIGINT,
           coalesce(sum(56+pg_column_size(cached_terms)+pg_column_size(frequencies)
                            +pg_column_size(starts)+pg_column_size(lengths)+24+4*term_count),0)::BIGINT,
           count(*)::BIGINT,
           (SELECT count(*) FROM model_input WHERE byte_length>16),
           (SELECT count(DISTINCT substring(convert_to(term,'UTF8') FROM 1 FOR 32))
              FROM model_input WHERE byte_length>16),
           -- Highly repetitive words or shared long prefixes can compress far
           -- better than raw arrays suggest. Inspect bounded prefix/suffix
           -- samples of existing terms; this is no canonical re-tokenization.
           coalesce((SELECT bool_or(length(regexp_replace(left(term,128)||right(term,128),
                    '(.)\1{3,}', '\1', 'g'))*2<length(left(term,128)||right(term,128)))
                     FROM model_input WHERE byte_length>16),FALSE)
      INTO v_compact_model_bytes,v_byte_model_bytes,v_model_blocks,
           v_long_term_count,v_long_prefix_count,v_repetitive_dictionary
      FROM model_arrays;
    -- Prefer established COMPACT when uncertain. Require at least a factor2
    -- model advantage AND 4KiB/256bytes-per-block margin; long-prefix/run cases
    -- remain COMPACT. Neither this model nor the margin predicts universal
    -- compressed TOAST/index cost or immediate physical filesystem savings.
    v_prefer_byte:=v_long_term_count>0
       AND v_long_prefix_count*4>=v_long_term_count*3
       AND NOT v_repetitive_dictionary
       AND v_byte_model_bytes*2<=v_compact_model_bytes
       AND v_compact_model_bytes-v_byte_model_bytes>=greatest(4096::BIGINT,v_model_blocks*256);
    IF v_prefer_byte THEN$adaptive_choice$;
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'posting cost admission representation boundary differs';
 END IF;
 EXECUTE replace(definition,marker,replacement);
 IF before_authority IS DISTINCT FROM (SELECT jsonb_build_object('oid',oid,'owner',proowner,
   'acl',proacl::TEXT,'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames,
   'volatility',provolatile,'parallel',proparallel) FROM pg_proc WHERE oid=producer_signature::REGPROCEDURE) THEN
  RAISE EXCEPTION 'posting cost admission changed existing producer authority';
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
END $posting_cost_admission$;
COMMIT;
