-- Preserve complete retained postings from either established tokenizer epoch.
-- SQL034 included punctuation-only whitespace-delimited terms. SQL099 and
-- later constructors excluded them; neither historical representation is
-- rewritten merely to satisfy the newer tokenizer grammar.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='60s';

DO $tokenizer_parity$
DECLARE
 signature CONSTANT TEXT := 'storage_v2_convert_retained_postings(uuid,bytea,bigint,bytea,bytea,bigint,integer,bigint,bytea)';
 definition TEXT; before_authority JSONB; marker TEXT; replacement TEXT;
BEGIN
 IF inet_client_addr() IS NOT NULL
    OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
    OR current_setting('role') NOT IN ('none',session_user)
    OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'retained posting tokenizer migration requires the local administrative operator'
   USING ERRCODE='42501';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 IF encode(sha256(convert_to((SELECT prosrc FROM pg_proc WHERE oid=signature::REGPROCEDURE),'UTF8')),'hex')
      IS DISTINCT FROM '940474fb359abc528a68a934d05b7152f261602dd031857b7dff7bb902313e17' THEN
  RAISE EXCEPTION 'retained posting tokenizer predecessor body differs';
 END IF;
 SELECT jsonb_build_object('owner',proowner,'acl',proacl::TEXT,'configuration',proconfig,
                          'security_definer',prosecdef,'arguments',proargnames)
  INTO before_authority FROM pg_proc WHERE oid=signature::REGPROCEDURE;
 IF NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
   WHERE p.oid=signature::REGPROCEDURE AND r.rolsuper AND p.prosecdef
    AND p.proconfig=ARRAY['search_path=pg_catalog, public','row_security=off']::TEXT[])
    OR has_function_privilege('mainrag',signature,'EXECUTE') THEN
  RAISE EXCEPTION 'retained posting tokenizer predecessor authority differs';
 END IF;
 definition:=pg_get_functiondef(signature::REGPROCEDURE);

 marker:=' v_rows JSONB; flat BOOLEAN; compact BOOLEAN; budget NUMERIC; lock_budget NUMERIC;';
 replacement:=' v_rows JSONB; flat BOOLEAN; compact BOOLEAN; keep_punctuation_only BOOLEAN; budget NUMERIC; lock_budget NUMERIC;';
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained posting tokenizer declaration boundary differs';
 END IF;
 definition:=replace(definition,marker,replacement);

 -- This chooses a canonical grammar, not an allowlist of retained extras.
 -- The unchanged complete terms/frequencies/token-count and UTF-8 locator
 -- checks below must still prove the entire selected canonical variant.
 marker:=' SELECT array_agg(sha256(convert_to(stored_row::TEXT,''UTF8''))) INTO old_hashes FROM (';
 replacement:=$choose$ -- Verified retained pairs select the original or modern whitespace grammar.
 SELECT EXISTS(SELECT 1 FROM unnest(old_terms) retained(term)
   WHERE retained.term COLLATE "default" !~ '[[:alnum:]_]') INTO keep_punctuation_only;
 SELECT array_agg(sha256(convert_to(stored_row::TEXT,'UTF8'))) INTO old_hashes FROM ($choose$;
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained posting tokenizer verified-pair boundary differs';
 END IF;
 definition:=replace(definition,marker,replacement);

 marker:=$modern$WHERE token !~ '^[[:alnum:]_]+$' AND token ~ '[[:alnum:]_]'$modern$;
 replacement:=$both$WHERE token !~ '^[[:alnum:]_]+$' AND (token ~ '[[:alnum:]_]' OR keep_punctuation_only)$both$;
 IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
  RAISE EXCEPTION 'retained posting tokenizer whitespace boundary differs';
 END IF;
 EXECUTE replace(definition,marker,replacement);
 IF before_authority IS DISTINCT FROM (SELECT jsonb_build_object('owner',proowner,'acl',proacl::TEXT,
    'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames)
    FROM pg_proc WHERE oid=signature::REGPROCEDURE) THEN
  RAISE EXCEPTION 'retained posting tokenizer changed the existing authority';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
END $tokenizer_parity$;
COMMIT;
