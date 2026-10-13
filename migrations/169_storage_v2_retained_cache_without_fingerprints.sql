-- Reuse the nonnull lexeme cache's existing GIN; do not duplicate its keys in
-- a fingerprint cache. Null-vector derived documents retain their old format.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';
DO $cache_format$
DECLARE
 converter CONSTANT TEXT:='storage_v2_convert_retained_document_caches(uuid,bytea,bigint[],bytea[],bigint,integer,bigint,bytea)';
 guard_signature CONSTANT TEXT:='storage_v2_reject_document_mutation()';
 guard_pin CONSTANT TEXT:='13a936c176da7dc2b619053a791ceead5f3bd2d38147a9bbbde783d88c31a369';
 definition TEXT;marker TEXT;replacement TEXT;pinned RECORD;changed INTEGER;before_authority JSONB;
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user) OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'retained cache format requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 PERFORM storage_v2_require_lexical_conversion_operator();
 LOCK TABLE storage_v2_lexical_conversion_function_contract IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 IF encode(sha256(convert_to((SELECT prosrc FROM pg_proc WHERE oid=converter::REGPROCEDURE),'UTF8')),'hex')
     IS DISTINCT FROM '8bfcebcf6f8da312328ce9f3e75bd52617e1c1e8ded805b2dc453f71d6e5238d' THEN
  RAISE EXCEPTION 'retained cache format converter predecessor differs';
 END IF;
 FOR pinned IN SELECT * FROM (VALUES
  (guard_signature,guard_pin),
  ('storage_v2_logical_document_fts(tsvector,boolean,text)','86e5f40880a95c2dae7e807f3d8f5a0f1d9e5140a94bfc4248b16d50bad24bfb'),
  ('storage_v2_source_segment_ranks(bigint[],text)','59b9a02a909e89c6ffda76dfbf195f60a7acd727f417560cc3c2ec343cd660fe'),
  ('storage_v2_source_segment_ranks_precise(bigint[],text)','cb57a29eaba662bf2c1b4b8f2dd705f5d85ae59e456ca40f83cb94bc6a4f5778'),
  ('storage_v2_source_segment_rank_candidates(bigint[],text)','de3abad48566b7e35a69376d9f35e5c4d27065272d3b655d6dd9416dcc51e1b2'),
  ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])','4a16635c85d593ff01f3d83aa1bce8202552e2be944e3e469e8fb59621e62521'),
  ('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)','223bcbc54409a447ce4dcf6f82b8aefc687f4844d3f7fdb96cb95ec304606413'),
  ('storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)','d611365a78418acc2b8049c82ed0f869edaec34f82971d72b19f7b71a5ac91d3')
 ) expected(signature,definition_sha256) LOOP
  IF encode(sha256(convert_to(pg_get_functiondef(pinned.signature::REGPROCEDURE),'UTF8')),'hex')
      IS DISTINCT FROM pinned.definition_sha256 THEN
   RAISE EXCEPTION 'retained cache format reader/guard predecessor differs: %',pinned.signature;
  END IF;
 END LOOP;
 SELECT jsonb_object_agg(oid::TEXT,jsonb_build_object('owner',proowner,'acl',proacl::TEXT,
    'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames,
    'volatility',provolatile,'parallel',proparallel)) INTO before_authority
  FROM pg_proc WHERE oid IN (converter::REGPROCEDURE,guard_signature::REGPROCEDURE);

 definition:=pg_get_functiondef(guard_signature::REGPROCEDURE);
 marker:=$old$       AND NEW.fts_simple_fingerprints IS NOT DISTINCT FROM
            storage_v2_lexical_block_fingerprints(ARRAY[OLD.fts_simple]) THEN$old$;
 replacement:=$new$       AND (CASE WHEN NEW.fts_simple_fingerprints IS NULL THEN
             coalesce(current_setting('mainrag.retained_cache_no_fingerprint_operation',TRUE),'')
                 ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
           ELSE NEW.fts_simple_fingerprints IS NOT DISTINCT FROM
            storage_v2_lexical_block_fingerprints(ARRAY[OLD.fts_simple]) END) THEN$new$;
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained cache format exact vector guard boundary differs';
 END IF;
 EXECUTE replace(definition,marker,replacement);

 definition:=pg_get_functiondef(converter::REGPROCEDURE);
 marker:='vector TSVECTOR; proposed_fingerprints INTEGER[]; identifiers TEXT[];';
 replacement:=marker||E'\n without_fingerprints BOOLEAN:=coalesce(current_setting(''mainrag.retained_cache_no_fingerprint_operation'',TRUE)=p_operation::TEXT,FALSE);';
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained cache format mode declaration boundary differs';
 END IF;
 definition:=replace(definition,marker,replacement);
 marker:='      OR receipt.document_identity_sha256 IS DISTINCT FROM document_identity_sha256';
 replacement:=marker||E'\n      OR (receipt.fts_changed AND ((SELECT d.fts_simple_fingerprints IS NULL\n          FROM storage_v2_search_document d WHERE d.id=item.id) IS DISTINCT FROM without_fingerprints))';
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained cache format replay boundary differs';
 END IF;
 definition:=replace(definition,marker,replacement);
 marker:='   proposed_fingerprints:=storage_v2_lexical_block_fingerprints(ARRAY[vector]);';
 replacement:=$mode$   IF without_fingerprints THEN proposed_fingerprints:=NULL;
   ELSE proposed_fingerprints:=storage_v2_lexical_block_fingerprints(ARRAY[vector]);END IF;$mode$;
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained cache format fingerprint preparation boundary differs';
 END IF;
 definition:=replace(definition,marker,replacement);
 marker:='   IF 96::BIGINT*cardinality(proposed_fingerprints)>=original_stored_cache_bytes THEN';
 replacement:='   IF NOT without_fingerprints AND 96::BIGINT*cardinality(proposed_fingerprints)>=original_stored_cache_bytes THEN';
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained cache format preflight boundary differs';
 END IF;
 definition:=replace(definition,marker,replacement);
 marker:='    SELECT 96::BIGINT*cardinality(d.fts_simple_fingerprints) INTO fingerprint_index_bytes';
 replacement:='    SELECT coalesce(96::BIGINT*cardinality(d.fts_simple_fingerprints),0) INTO fingerprint_index_bytes';
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained cache format cost boundary differs';
 END IF;
 EXECUTE replace(definition,marker,replacement);
 IF before_authority IS DISTINCT FROM (SELECT jsonb_object_agg(oid::TEXT,jsonb_build_object('owner',proowner,
   'acl',proacl::TEXT,'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames,
   'volatility',provolatile,'parallel',proparallel)) FROM pg_proc
   WHERE oid IN (converter::REGPROCEDURE,guard_signature::REGPROCEDURE)) THEN
  RAISE EXCEPTION 'retained cache format changed existing function authority';
 END IF;

 -- Advance only the exact existing dependency on the changed mutation guard.
 UPDATE storage_v2_lexical_conversion_function_contract
  SET definition_sha256=sha256(convert_to(pg_get_functiondef(guard_signature::REGPROCEDURE),'UTF8'))
  WHERE signature=guard_signature AND definition_sha256=decode(guard_pin,'hex');
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 OR NOT EXISTS(SELECT 1 FROM pg_trigger
    WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
      AND tgname='storage_v2_lexical_conversion_seal_immutable' AND tgenabled='O' AND tgtype=27
      AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN
  RAISE EXCEPTION 'retained cache format exact lexical guard/seal boundary differs';
 END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 UPDATE storage_v2_lexical_conversion_contract_seal SET contract_sha256=sha256(convert_to(jsonb_build_object(
  'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),
  'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c)
  )::TEXT,'UTF8')) WHERE singleton;
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'retained cache format requires exactly one lexical seal';END IF;
 ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 PERFORM storage_v2_posting_conversion_require_operator();
 PERFORM storage_v2_require_lexical_conversion_operator();
END $cache_format$;

CREATE FUNCTION storage_v2_convert_retained_document_caches_without_fingerprint(
 p_operation UUID,p_manifest BYTEA,p_documents BIGINT[],p_materializations BYTEA[],
 p_max_body_bytes BIGINT,p_statement_budget_ms INTEGER,
 p_retained_run BIGINT DEFAULT NULL,p_retained_run_sha256 BYTEA DEFAULT NULL
) RETURNS TABLE(document_id BIGINT,disposition TEXT,original_cache_sha256 BYTEA,
 cache_payload_sha256 BYTEA,document_identity_sha256 BYTEA,original_stored_cache_bytes BIGINT,
 stored_cache_bytes BIGINT,audit_bytes BIGINT,text_bytes BIGINT,fingerprint_index_bytes BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
DECLARE previous_mode TEXT:=current_setting('mainrag.retained_cache_no_fingerprint_operation',TRUE);
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user) OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'retained cache format requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 PERFORM set_config('mainrag.retained_cache_no_fingerprint_operation',coalesce(p_operation::TEXT,''),TRUE);
 RETURN QUERY SELECT * FROM storage_v2_convert_retained_document_caches(p_operation,p_manifest,
    p_documents,p_materializations,p_max_body_bytes,p_statement_budget_ms,p_retained_run,p_retained_run_sha256);
 PERFORM set_config('mainrag.retained_cache_no_fingerprint_operation',coalesce(previous_mode,''),TRUE);
EXCEPTION WHEN OTHERS THEN
 PERFORM set_config('mainrag.retained_cache_no_fingerprint_operation',coalesce(previous_mode,''),TRUE);
 RAISE;
END $$;
REVOKE ALL ON FUNCTION storage_v2_convert_retained_document_caches_without_fingerprint(
 UUID,BYTEA,BIGINT[],BYTEA[],BIGINT,INTEGER,BIGINT,BYTEA) FROM PUBLIC,mainrag;
COMMENT ON FUNCTION storage_v2_convert_retained_document_caches_without_fingerprint(
 UUID,BYTEA,BIGINT[],BYTEA[],BIGINT,INTEGER,BIGINT,BYTEA) IS
 'Named local retained-cache conversion with a nonnull stripped vector and no duplicate fingerprint cache. Exact canonical guards, receipts, frozen-run and resource admission remain mandatory. Stored datum savings do not prove physical release.';
COMMIT;
