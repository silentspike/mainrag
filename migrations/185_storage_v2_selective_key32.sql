-- Optional selective int32 GIN probes; original exact readers remain the oracle.
-- No data rewrite or index build. The separate operator owns index admission.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
DO $admission$
BEGIN
 IF inet_client_addr() IS NOT NULL OR NOT (SELECT rolsuper FROM pg_roles WHERE rolname=session_user)
  OR current_setting('role') NOT IN ('none',session_user) OR storage_v2_is_admin() IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'key32 migration requires the local administrative operator' USING ERRCODE='42501';
 END IF;
 PERFORM storage_v2_posting_conversion_require_operator();
 IF to_regprocedure('storage_v2_require_lexical_conversion_operator()') IS NOT NULL THEN
  PERFORM storage_v2_require_lexical_conversion_operator();
 END IF;
 IF to_regprocedure('public.storage_v2_probe_keys32(text[])') IS NOT NULL
  OR to_regprocedure('public.storage_v2_key32_probes_ready(text)') IS NOT NULL
  OR to_regprocedure('public.storage_v2_key32_probes_ready()') IS NOT NULL THEN
  RAISE EXCEPTION 'key32 namespace already exists';
 END IF;
END $admission$;
CREATE TEMP TABLE key185_before ON COMMIT DROP AS
 SELECT p.oid,p.oid::REGPROCEDURE::TEXT signature,pg_get_functiondef(p.oid) definition,to_jsonb(p)-'prosrc' authority
 FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f';
CREATE TEMP TABLE key185_posting ON COMMIT DROP AS SELECT * FROM storage_v2_posting_conversion_contract;
CREATE TEMP TABLE key185_posting_relations ON COMMIT DROP AS SELECT * FROM storage_v2_posting_conversion_catalog_contract;
DO $optional_lexical_snapshot$
BEGIN
 IF to_regclass('storage_v2_lexical_conversion_function_contract') IS NOT NULL THEN
  CREATE TEMP TABLE key185_lexical ON COMMIT DROP AS SELECT * FROM storage_v2_lexical_conversion_function_contract;
  CREATE TEMP TABLE key185_lexical_relations ON COMMIT DROP AS SELECT * FROM storage_v2_lexical_conversion_catalog_contract;
  CREATE TEMP TABLE key185_seal ON COMMIT DROP AS SELECT * FROM storage_v2_lexical_conversion_contract_seal;
 END IF;
END $optional_lexical_snapshot$;

-- First four SHA256 bytes, signed two's-complement int32; collisions only admit
-- candidates. UTF8, complete input terms and sorted unique keys are immutable.
CREATE FUNCTION public.storage_v2_probe_keys32(p_terms TEXT[])
RETURNS INTEGER[] LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public,pg_temp AS $keys$
 SELECT ARRAY(SELECT DISTINCT ('x'||encode(substring(sha256(convert_to(term,'UTF8')) FROM 1 FOR 4),'hex'))::BIT(32)::INTEGER AS key
  FROM unnest(p_terms) input(term) WHERE term IS NOT NULL ORDER BY key)
$keys$;
ALTER FUNCTION public.storage_v2_probe_keys32(TEXT[]) OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION public.storage_v2_probe_keys32(TEXT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.storage_v2_probe_keys32(TEXT[]) TO mainrag,mainrag_v2_frontier_owner;

-- Per-relation readiness permits a safe partial rollout; the no-arg form below
-- validates BOTH definitions. Missing/incomplete indexes keep exact fallback.
CREATE FUNCTION public.storage_v2_key32_probes_ready(p_kind TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql STABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public,pg_temp AS $ready$
DECLARE expected RECORD;actual RECORD;
BEGIN
 IF p_kind NOT IN ('lexical','posting') THEN
  RAISE EXCEPTION 'unknown key32 probe kind' USING ERRCODE='22023'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
  WHERE p.oid='public.storage_v2_probe_keys32(text[])'::REGPROCEDURE
   AND p.proowner='mainrag_v2_lexical_rank_owner'::REGROLE AND l.lanname='sql'
   AND p.prokind='f' AND p.provolatile='i' AND p.proisstrict AND p.proparallel='s'
   AND NOT p.proretset AND NOT p.prosecdef AND NOT p.proleakproof AND p.prosupport=0
   AND p.prorettype='integer[]'::REGTYPE AND p.pronargs=1 AND p.pronargdefaults=0 AND p.provariadic=0
   AND p.proconfig=ARRAY['search_path=pg_catalog, public, pg_temp']::TEXT[]
   AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')='eadccc159ccec195f25bc63d921d852b4c255dea6d833fdf15869ecc8ff7121a'
   AND has_function_privilege('mainrag',p.oid,'EXECUTE')
   AND has_function_privilege('mainrag_v2_frontier_owner',p.oid,'EXECUTE')
   AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
    WHERE a.grantee NOT IN (p.proowner,'mainrag'::REGROLE,'mainrag_v2_frontier_owner'::REGROLE)
     OR a.privilege_type<>'EXECUTE' OR (a.is_grantable AND a.grantee<>p.proowner))) THEN
  RAISE EXCEPTION 'key32 helper definition or authority differs';
 END IF;
 SELECT * INTO expected FROM (VALUES
  ('lexical','idx_storage_v2_compact_lexical_key32','storage_v2_compact_lexical_block',
   'storage_v2_probe_keys32(storage_v2_compact_lexical_exact_lexemes(fts_vectors))'),
  ('posting','idx_storage_v2_compact_posting_key32','storage_v2_compact_posting_block',
   'storage_v2_probe_keys32(terms)')) e(kind,index_name,table_name,expression) WHERE e.kind=p_kind;
 SELECT i.*,idx.relowner index_owner,idx.reloptions index_options,tbl.relowner table_owner,
  am.amname,pg_get_expr(i.indexprs,i.indrelid) expression,op.opcname,op.opcnamespace
 INTO actual FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid
 JOIN pg_class tbl ON tbl.oid=i.indrelid JOIN pg_am am ON am.oid=idx.relam
 JOIN pg_opclass op ON op.oid=i.indclass[0]
 WHERE i.indexrelid=to_regclass('public.'||expected.index_name);
 IF NOT FOUND THEN
  IF to_regclass('public.'||expected.index_name) IS NOT NULL THEN
   RAISE EXCEPTION 'key32 index definition differs: %',expected.index_name; END IF;
  RETURN FALSE;
 END IF;
 IF actual.indrelid<>to_regclass('public.'||expected.table_name) OR actual.amname<>'gin'
  OR actual.index_owner<>actual.table_owner OR actual.index_options IS NOT NULL
  OR actual.indisunique OR actual.indisprimary OR actual.indisexclusion
  OR actual.indnatts<>1 OR actual.indnkeyatts<>1 OR actual.indpred IS NOT NULL
  OR actual.indkey::TEXT<>'0' OR actual.expression IS DISTINCT FROM expected.expression
  OR actual.opcname<>'array_ops' OR actual.opcnamespace<>'pg_catalog'::REGNAMESPACE
  OR actual.indoption[0]<>0 OR actual.indcollation[0]<>0 THEN
  RAISE EXCEPTION 'key32 index definition differs: %',expected.index_name;
 END IF;
 RETURN actual.indisvalid AND actual.indisready AND actual.indislive AND NOT actual.indcheckxmin;
END $ready$;
ALTER FUNCTION public.storage_v2_key32_probes_ready(TEXT) OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION public.storage_v2_key32_probes_ready(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.storage_v2_key32_probes_ready(TEXT) TO mainrag,mainrag_v2_frontier_owner;
CREATE FUNCTION public.storage_v2_key32_probes_ready()
RETURNS BOOLEAN LANGUAGE sql STABLE PARALLEL SAFE
SET search_path=pg_catalog,public,pg_temp AS $both$
 SELECT bool_and(ready) FROM (SELECT public.storage_v2_key32_probes_ready('lexical') AS ready
  UNION ALL SELECT public.storage_v2_key32_probes_ready('posting')) probes
$both$;
ALTER FUNCTION public.storage_v2_key32_probes_ready() OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION public.storage_v2_key32_probes_ready() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.storage_v2_key32_probes_ready() TO mainrag,mainrag_v2_frontier_owner;

DO $readers$
DECLARE routine RECORD;definition TEXT;patched TEXT;marker TEXT;replacement TEXT;hash BYTEA;changed INTEGER;
BEGIN
 IF (SELECT count(*) FROM key185_before WHERE signature IN (
  'storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)',
  'storage_v2_scoped_query_posting(bigint[],text[])'))<>2 THEN
  RAISE EXCEPTION 'key32 requires both original readers'; END IF;
 FOR routine IN SELECT * FROM key185_before WHERE signature IN (
  'storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)',
  'storage_v2_scoped_query_posting(bigint[],text[])') LOOP
  definition:=routine.definition;patched:=definition;
  IF routine.signature LIKE 'storage_v2_authorized_cached%' THEN
   IF encode(sha256(convert_to(definition,'UTF8')),'hex') NOT IN (
    'd73adfacb3037c6881b3d589170d5558ebb81f091da90dffcde7f94f5a58fdf8',
    '8a4a4ebbbe70c2e2303243ab10a19c2135b2d77a4d6c9b3a01c63b9bb8eb2cc7') THEN
    RAISE EXCEPTION 'key32 lexical predecessor differs'; END IF;
   marker:='    lazy_exact_ready BOOLEAN := public.storage_v2_exact_lexeme_probes_ready();';
   replacement:='    lazy_key32_ready BOOLEAN := public.storage_v2_key32_probes_ready(''lexical'');'
    ||E'\n    lazy_legacy_exact_ready BOOLEAN := public.storage_v2_exact_lexeme_probes_ready();'
    ||E'\n    lazy_exact_ready BOOLEAN := lazy_key32_ready OR lazy_legacy_exact_ready;';
   IF (length(patched)-length(replace(patched,marker,'')))/length(marker)<>1 THEN RAISE EXCEPTION 'key32 lexical declaration differs'; END IF;
   patched:=replace(patched,marker,replacement);
   marker:='    lazy_exact_cursor INTEGER;';
   patched:=replace(patched,marker,marker||E'\n    lazy_keys32 INTEGER[] := CASE WHEN lazy_key32_ready THEN public.storage_v2_probe_keys32(lazy_terms) END;');
   -- Split only the collective scan. Encoded payloads have no key32 index and
   -- therefore retain their prior exact/fingerprint branch without key32 work.
   marker:=$old$              FROM public.storage_v2_compact_lexical_block_all block
             WHERE block.source_id = ANY(v_source_ids)
               AND block.fingerprints @> lazy_fingerprints
               AND (NOT lazy_exact_ready OR
                    public.storage_v2_compact_lexical_exact_lexemes(block.fts_vectors) @> lazy_terms)$old$;
   IF to_regclass('public.storage_v2_encoded_lexical_block') IS NOT NULL THEN
    replacement:=$new$              FROM public.storage_v2_compact_lexical_block block
             WHERE block.source_id = ANY(v_source_ids)
               AND block.fingerprints @> lazy_fingerprints
               AND CASE WHEN lazy_key32_ready THEN
                    public.storage_v2_probe_keys32(public.storage_v2_compact_lexical_exact_lexemes(block.fts_vectors)) @> lazy_keys32
                   WHEN lazy_legacy_exact_ready THEN
                    public.storage_v2_compact_lexical_exact_lexemes(block.fts_vectors) @> lazy_terms ELSE TRUE END
            UNION ALL
            SELECT block.occurrence_id,block.source_id,block.artifact_version_id,block.block_order
              FROM public.storage_v2_encoded_lexical_block block
             WHERE block.source_id = ANY(v_source_ids)
               AND block.fingerprints @> lazy_fingerprints
               AND CASE WHEN lazy_legacy_exact_ready THEN
                    public.storage_v2_compact_lexical_exact_lexemes(public.storage_v2_decode_lexical_vectors(block.fts_payload)) @> lazy_terms
                   ELSE TRUE END$new$;
   ELSE
    marker:=replace(marker,'storage_v2_compact_lexical_block_all','storage_v2_compact_lexical_block');
    replacement:=replace(marker,$old$AND (NOT lazy_exact_ready OR
                    public.storage_v2_compact_lexical_exact_lexemes(block.fts_vectors) @> lazy_terms)$old$,
     $new$AND CASE WHEN lazy_key32_ready THEN
                    public.storage_v2_probe_keys32(public.storage_v2_compact_lexical_exact_lexemes(block.fts_vectors)) @> lazy_keys32
                   WHEN lazy_legacy_exact_ready THEN
                    public.storage_v2_compact_lexical_exact_lexemes(block.fts_vectors) @> lazy_terms ELSE TRUE END$new$);
   END IF;
  ELSE
   IF encode(sha256(convert_to(definition,'UTF8')),'hex')<>'a2a480bf7b7ed91501eefea5cabc6cf82ee9b97a0d247ef85d93da28ad38d55d' THEN
    RAISE EXCEPTION 'key32 posting predecessor differs'; END IF;
   marker:='    v_exact_lexeme_probes BOOLEAN := public.storage_v2_exact_lexeme_probes_ready();';
   replacement:=marker||E'\n    v_key32_ready BOOLEAN := public.storage_v2_key32_probes_ready(''posting'');'
    ||E'\n    v_keys32 INTEGER[];';
   IF (length(patched)-length(replace(patched,marker,'')))/length(marker)<>1 THEN RAISE EXCEPTION 'key32 posting declaration differs'; END IF;
   patched:=replace(patched,marker,replacement);
   marker:='    IF v_min_document IS NULL THEN RETURN; END IF;';
   patched:=replace(patched,marker,marker||E'\n    IF v_key32_ready THEN v_keys32:=public.storage_v2_probe_keys32(v_terms); END IF;');
   marker:=$old$           AND (NOT v_exact_lexeme_probes OR
                public.storage_v2_compact_posting_exact_term_keys(block.terms) && v_hashes)$old$;
   replacement:=$new$           AND CASE WHEN v_key32_ready THEN public.storage_v2_probe_keys32(block.terms) && v_keys32
               WHEN v_exact_lexeme_probes THEN public.storage_v2_compact_posting_exact_term_keys(block.terms) && v_hashes
               ELSE TRUE END$new$;
  END IF;
  IF (length(patched)-length(replace(patched,marker,'')))/length(marker)<>1 THEN RAISE EXCEPTION 'key32 scoped predicate differs'; END IF;
  patched:=replace(patched,marker,replacement);EXECUTE patched;
  IF (SELECT to_jsonb(p)-'prosrc' FROM pg_proc p WHERE p.oid=routine.oid) IS DISTINCT FROM routine.authority THEN
   RAISE EXCEPTION 'key32 reader authority changed'; END IF;
  hash:=sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8'));
  UPDATE storage_v2_posting_conversion_contract SET definition_sha256=hash
   WHERE signature=routine.signature AND definition_sha256=sha256(convert_to(definition,'UTF8'));
  IF to_regclass('key185_lexical') IS NOT NULL THEN
   UPDATE storage_v2_lexical_conversion_function_contract SET definition_sha256=hash
    WHERE signature=routine.signature AND definition_sha256=sha256(convert_to(definition,'UTF8'));
  END IF;
 END LOOP;
 IF to_regclass('key185_lexical') IS NOT NULL THEN
  INSERT INTO storage_v2_lexical_conversion_function_contract
   SELECT p.oid::REGPROCEDURE::TEXT,sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),p.proowner,
    sha256(convert_to(coalesce(p.proacl::TEXT,''),'UTF8')) FROM pg_proc p WHERE p.oid IN (
     'storage_v2_probe_keys32(text[])'::REGPROCEDURE,'storage_v2_key32_probes_ready(text)'::REGPROCEDURE,'storage_v2_key32_probes_ready()'::REGPROCEDURE);
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='storage_v2_lexical_conversion_contract_seal'::REGCLASS
   AND tgname='storage_v2_lexical_conversion_seal_immutable' AND tgenabled='O' AND tgtype=27
   AND tgfoid='storage_v2_reject_retrieval_mutation()'::REGPROCEDURE) THEN RAISE EXCEPTION 'key32 lexical seal guard differs'; END IF;
  ALTER TABLE storage_v2_lexical_conversion_contract_seal DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
  UPDATE storage_v2_lexical_conversion_contract_seal SET function_count=(SELECT count(*) FROM storage_v2_lexical_conversion_function_contract),
   contract_sha256=sha256(convert_to(jsonb_build_object(
    'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),
    'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c))::TEXT,'UTF8'))
   WHERE singleton AND to_jsonb(storage_v2_lexical_conversion_contract_seal)=(SELECT to_jsonb(c) FROM key185_seal c);
  GET DIAGNOSTICS changed=ROW_COUNT;
  IF changed<>1 THEN RAISE EXCEPTION 'key32 lexical seal predecessor CAS differs'; END IF;
  ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
 END IF;
END $readers$;
SELECT storage_v2_posting_conversion_require_operator();
DO $final$
BEGIN
 IF EXISTS(SELECT 1 FROM key185_before old JOIN pg_proc p ON p.oid=old.oid
  WHERE to_jsonb(p)-'prosrc' IS DISTINCT FROM old.authority)
  OR (SELECT count(*) FROM pg_proc WHERE pronamespace='public'::REGNAMESPACE AND prokind='f')<>(SELECT count(*)+3 FROM key185_before)
  OR EXISTS(SELECT 1 FROM key185_before old WHERE old.signature NOT IN (
   'storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)',
   'storage_v2_scoped_query_posting(bigint[],text[])') AND pg_get_functiondef(old.oid)<>old.definition)
  OR (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c)
    IS DISTINCT FROM (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM key185_posting_relations c)
  OR (SELECT count(*) FROM storage_v2_posting_conversion_contract)<>(SELECT count(*) FROM key185_posting) THEN
  RAISE EXCEPTION 'key32 changed unrelated public function/authority/posting catalog membership'; END IF;
 IF to_regclass('key185_lexical') IS NOT NULL THEN
  IF (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c)
    IS DISTINCT FROM (SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM key185_lexical_relations c)
   OR (SELECT count(*) FROM storage_v2_lexical_conversion_function_contract)<>(SELECT count(*)+3 FROM key185_lexical) THEN
   RAISE EXCEPTION 'key32 changed unrelated lexical catalog membership'; END IF;
  PERFORM storage_v2_require_lexical_conversion_operator();
 END IF;
 PERFORM public.storage_v2_key32_probes_ready();
END $final$;
COMMIT;
