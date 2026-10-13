-- Reject mathematically uneconomic FTS changes before any indexed UPDATE.
-- The96-byte fingerprint charge remains an admission model, not a physical
-- upper bound. Identifier-only transitions retain the original138/166 guards.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';
DO $cache_preflight_cost$
DECLARE
 signature CONSTANT TEXT:='storage_v2_convert_retained_document_caches(uuid,bytea,bigint[],bytea[],bigint,integer,bigint,bytea)';
 definition TEXT;marker TEXT;replacement TEXT;before_authority JSONB;
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user) OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'retained cache preflight migration requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 PERFORM storage_v2_require_lexical_conversion_operator();
 definition:=pg_get_functiondef(signature::REGPROCEDURE);
 IF encode(sha256(convert_to(definition,'UTF8')),'hex') IS DISTINCT FROM
    '06a899c86ded22d6402f3e0439dff6b7c742dd9ab7aa3cdf20a272abc5a7e330' THEN
  RAISE EXCEPTION 'retained cache preflight predecessor differs';
 END IF;
 SELECT jsonb_build_object('owner',proowner,'acl',proacl::TEXT,'configuration',proconfig,
     'security_definer',prosecdef,'arguments',proargnames) INTO before_authority
  FROM pg_proc WHERE oid=signature::REGPROCEDURE;
 IF NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
    WHERE p.oid=signature::REGPROCEDURE AND r.rolsuper AND p.prosecdef
     AND p.proconfig=ARRAY['search_path=pg_catalog, public','row_security=off']::TEXT[])
    OR has_function_privilege('mainrag',signature,'EXECUTE') THEN
  RAISE EXCEPTION 'retained cache preflight predecessor authority differs';
 END IF;

 marker:='vector TSVECTOR; identifiers TEXT[];';
 replacement:='vector TSVECTOR; proposed_fingerprints INTEGER[]; identifiers TEXT[];';
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained cache preflight declaration boundary differs';
 END IF;
 definition:=replace(definition,marker,replacement);

 marker:=$before$  SELECT EXISTS(SELECT 1 FROM storage_v2_document_postings_seal s WHERE s.document_id=item.id
     AND s.materialization_sha256=old_document.materialization_sha256) INTO seal_present;$before$;
 replacement:=$preflight$  IF fts_change THEN
   vector:=old_document.fts_simple;
   proposed_fingerprints:=storage_v2_lexical_block_fingerprints(ARRAY[vector]);
   -- Every other destination/cache/audit cost is nonnegative. If this single
   -- existing model charge consumes the whole original cache, no FTS variant
   -- can win. Choose identifiers before UPDATE/GIN work, never by a percentage.
   IF 96::BIGINT*cardinality(proposed_fingerprints)>=original_stored_cache_bytes THEN
    -- The skipped UPDATE cannot run161's canonical guard. Check exactly once
    -- here instead, before any identifier mutation. The normal FTS path still
    -- uses only that existing trigger; it never adds this second tokenizer.
    IF vector IS DISTINCT FROM storage_v2_safe_tsvector(old_document.search_text) THEN
     disposition:='RETAIN_VECTOR_MISMATCH';RETURN NEXT;CONTINUE;
    END IF;
    fts_change:=FALSE;
    IF NOT identifier_change THEN
     disposition:='RETAIN_NO_SAVINGS';RETURN NEXT;CONTINUE;
    END IF;
   END IF;
  END IF;
  SELECT EXISTS(SELECT 1 FROM storage_v2_document_postings_seal s WHERE s.document_id=item.id
     AND s.materialization_sha256=old_document.materialization_sha256) INTO seal_present;$preflight$;
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained cache preflight decision boundary differs';
 END IF;
 definition:=replace(definition,marker,replacement);

 marker:='fts_simple_fingerprints=storage_v2_lexical_block_fingerprints(ARRAY[vector]) WHERE d.id=item.id;';
 replacement:='fts_simple_fingerprints=proposed_fingerprints WHERE d.id=item.id;';
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained cache preflight fingerprint reuse boundary differs';
 END IF;
 EXECUTE replace(definition,marker,replacement);
 IF before_authority IS DISTINCT FROM (SELECT jsonb_build_object('owner',proowner,'acl',proacl::TEXT,
    'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames)
    FROM pg_proc WHERE oid=signature::REGPROCEDURE) THEN
  RAISE EXCEPTION 'retained cache preflight changed the existing authority';
 END IF;
 PERFORM storage_v2_require_lexical_conversion_operator();
END $cache_preflight_cost$;
COMMIT;
