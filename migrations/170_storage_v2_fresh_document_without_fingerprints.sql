-- Fresh nonnull stripped vectors already supply the exact lexeme GIN cache.
-- Avoid computing/storing duplicate document fingerprints. Null-vector
-- fallback and every logical identity/reader/posting remain unchanged.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='15s';

DO $fresh_cache_format$
DECLARE
 prepare_signature CONSTANT TEXT:='storage_v2_prepare_document_vector()';
 predecessor CONSTANT TEXT:='488e0259e7ccaa9c7367c2e589d1746eff96906743281b5d5dc62dcf380bc7ec';
 definition TEXT;marker TEXT;replacement TEXT;authority JSONB;catalog_identity BYTEA;
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user)
    OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'fresh cache format requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 PERFORM storage_v2_require_lexical_conversion_operator();
 LOCK TABLE storage_v2_search_document IN SHARE ROW EXCLUSIVE MODE NOWAIT;
 IF encode(sha256(convert_to((SELECT prosrc FROM pg_proc WHERE oid=prepare_signature::REGPROCEDURE),'UTF8')),'hex')
      IS DISTINCT FROM predecessor
    OR NOT EXISTS(SELECT 1 FROM pg_proc WHERE oid=prepare_signature::REGPROCEDURE AND proowner='mainrag'::REGROLE
         AND prorettype='trigger'::REGTYPE AND prosecdef
         AND proconfig=ARRAY['search_path=pg_catalog, public','row_security=off']::TEXT[])
    OR NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_search_document'::REGCLASS
         AND tgname='storage_v2_search_document_vector' AND tgenabled='O' AND tgtype=7
         AND tgfoid=prepare_signature::REGPROCEDURE) THEN
  RAISE EXCEPTION 'fresh cache format predecessor or INSERT trigger authority differs';
 END IF;
 -- Neither existing contract pins this trigger routine's body. Relation
 -- contracts retain the same trigger OID/attachment after CREATE OR REPLACE.
 -- An unexpected new dependency needs its own exact review, not a blind seal
 -- refresh or an unrelated producer/reader pin change.
 IF EXISTS(SELECT 1 FROM storage_v2_posting_conversion_contract WHERE signature=prepare_signature)
    OR EXISTS(SELECT 1 FROM storage_v2_lexical_conversion_function_contract WHERE signature=prepare_signature) THEN
  RAISE EXCEPTION 'fresh cache format has an unreviewed direct function contract';
 END IF;
 SELECT jsonb_build_object('oid',oid,'owner',proowner,'acl',proacl::TEXT,'configuration',proconfig,
    'security_definer',prosecdef,'arguments',proargnames,'result',prorettype,'volatility',provolatile,
    'parallel',proparallel) INTO authority FROM pg_proc WHERE oid=prepare_signature::REGPROCEDURE;
 catalog_identity:=storage_v2_posting_conversion_relation_sha256('storage_v2_search_document'::REGCLASS);
 definition:=pg_get_functiondef(prepare_signature::REGPROCEDURE);
 marker:='  NEW.fts_simple_fingerprints:=storage_v2_lexical_block_fingerprints(ARRAY[vector]);';
 replacement:=$choice$  IF vector IS NULL THEN
   -- Preserve the established oversized/null-vector fallback exactly.
   NEW.fts_simple_fingerprints:=storage_v2_lexical_block_fingerprints(ARRAY[vector]);
  ELSE
   -- Same nonnull strip(vector); no duplicate whole-document fingerprint
   -- keys or fingerprint helper invocation. This is not physical reclamation.
   NEW.fts_simple_fingerprints:=NULL;
  END IF;$choice$;
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'fresh cache format fingerprint assignment boundary differs';
 END IF;
 EXECUTE replace(definition,marker,replacement);
 IF authority IS DISTINCT FROM (SELECT jsonb_build_object('oid',oid,'owner',proowner,'acl',proacl::TEXT,
     'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames,'result',prorettype,
     'volatility',provolatile,'parallel',proparallel) FROM pg_proc WHERE oid=prepare_signature::REGPROCEDURE)
    OR catalog_identity IS DISTINCT FROM
       storage_v2_posting_conversion_relation_sha256('storage_v2_search_document'::REGCLASS) THEN
  RAISE EXCEPTION 'fresh cache format changed function authority or document catalog identity';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 PERFORM storage_v2_require_lexical_conversion_operator();
END $fresh_cache_format$;
COMMIT;
