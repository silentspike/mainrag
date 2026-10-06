-- Admit optional exact lexeme indexes without rewriting stored projections.
-- Index creation and resource admission belong to the separate operator.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $predecessor$
DECLARE pinned RECORD; routine RECORD;
BEGIN
 IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
  RAISE EXCEPTION 'exact lexeme migration requires the administrative schema operator';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('storage-v2-posting-conversion-v1',0));
 IF (SELECT count(*) FROM storage_v2_posting_conversion_contract)<>23
    OR (SELECT count(*) FROM storage_v2_posting_conversion_catalog_contract)<>7 THEN
  RAISE EXCEPTION 'posting conversion predecessor contract is incomplete';
 END IF;
 FOR pinned IN SELECT * FROM storage_v2_posting_conversion_contract LOOP
  SELECT * INTO STRICT routine FROM pg_proc WHERE oid=pinned.signature::REGPROCEDURE;
  IF routine.proowner IS DISTINCT FROM pinned.owner_oid
     OR sha256(convert_to(coalesce(routine.proacl::TEXT,''),'UTF8')) IS DISTINCT FROM pinned.acl_sha256
     OR sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8')) IS DISTINCT FROM pinned.definition_sha256 THEN
   RAISE EXCEPTION 'posting conversion predecessor identity differs: %',pinned.signature;
  END IF;
 END LOOP;
 FOR pinned IN SELECT * FROM storage_v2_posting_conversion_catalog_contract LOOP
  IF storage_v2_posting_conversion_relation_sha256(pinned.relation_oid::REGCLASS)
       IS DISTINCT FROM pinned.identity_sha256 THEN
   RAISE EXCEPTION 'posting conversion predecessor catalog differs';
  END IF;
 END LOOP;
END $predecessor$;

-- Distinct complete lexemes, including every window boundary and metadata
-- vector. This is a necessary condition only; original vectors decide matches.
CREATE FUNCTION storage_v2_compact_lexical_exact_lexemes(p_vectors TSVECTOR[])
RETURNS TEXT[] LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public,pg_temp AS $lexemes$
 SELECT ARRAY(SELECT DISTINCT lexeme COLLATE "C"
  FROM unnest(p_vectors) vector
  CROSS JOIN LATERAL unnest(tsvector_to_array(vector)) lexeme
  ORDER BY 1)
$lexemes$;
ALTER FUNCTION storage_v2_compact_lexical_exact_lexemes(TSVECTOR[])
 OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION storage_v2_compact_lexical_exact_lexemes(TSVECTOR[]) FROM PUBLIC,mainrag;
GRANT EXECUTE ON FUNCTION storage_v2_compact_lexical_exact_lexemes(TSVECTOR[])
 TO mainrag_v2_frontier_owner;

-- One catalog-only decision per reader invocation. Incomplete concurrent
-- builds use the unchanged fallback. A conflicting definition is drift,
-- rather than an apparently successful installation of the intended index.
CREATE FUNCTION storage_v2_exact_lexeme_probes_ready()
RETURNS BOOLEAN LANGUAGE plpgsql STABLE PARALLEL SAFE
SET search_path=pg_catalog,public,pg_temp AS $ready$
DECLARE expected RECORD; actual RECORD; all_ready BOOLEAN:=TRUE;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
  WHERE p.oid='public.storage_v2_compact_lexical_exact_lexemes(tsvector[])'::REGPROCEDURE
    AND p.proowner='mainrag_v2_lexical_rank_owner'::REGROLE
    AND l.lanname='sql' AND p.provolatile='i' AND p.proisstrict AND p.proparallel='s'
    AND NOT p.prosecdef AND NOT p.proleakproof AND p.prosupport=0
    AND p.proconfig=ARRAY['search_path=pg_catalog, public, pg_temp']::TEXT[]
    AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')='4bbda8a04222fff3a7023125546cef2aa6b1713cc4111a9e5054628f90f5e8d7'
    AND has_function_privilege('mainrag_v2_frontier_owner',p.oid,'EXECUTE')
    AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
      WHERE a.grantee NOT IN (p.proowner,'mainrag_v2_frontier_owner'::REGROLE)
         OR a.privilege_type<>'EXECUTE' OR (a.is_grantable AND a.grantee<>p.proowner))) THEN
  RAISE EXCEPTION 'exact lexeme union helper identity differs';
 END IF;
 FOR expected IN SELECT * FROM (VALUES
  ('idx_storage_v2_compact_lexical_exact_lexemes',
   'storage_v2_compact_lexical_block',
   'storage_v2_compact_lexical_exact_lexemes(fts_vectors)',0),
  ('idx_storage_v2_compact_posting_exact_terms',
   'storage_v2_compact_posting_block',NULL::TEXT,3)
 ) indexes(index_name,table_name,expression,key_number) LOOP
  SELECT i.*,idx.relowner AS index_owner,idx.reloptions AS index_options,
         tbl.relowner AS table_owner,am.amname,
         pg_get_expr(i.indexprs,i.indrelid) AS expression,
         op.opcname,op.opcnamespace,
         (SELECT attcollation FROM pg_attribute
          WHERE attrelid=i.indrelid AND attname='terms' AND NOT attisdropped) AS term_collation
    INTO actual FROM pg_index i
    JOIN pg_class idx ON idx.oid=i.indexrelid
    JOIN pg_class tbl ON tbl.oid=i.indrelid
    JOIN pg_am am ON am.oid=idx.relam
    JOIN pg_opclass op ON op.oid=i.indclass[0]
   WHERE i.indexrelid=to_regclass('public.'||expected.index_name);
  IF NOT FOUND THEN
   IF to_regclass('public.'||expected.index_name) IS NOT NULL THEN
    RAISE EXCEPTION 'exact lexeme index definition differs: %',expected.index_name;
   END IF;
   all_ready:=FALSE;
   CONTINUE;
  END IF;
  IF actual.indrelid<>to_regclass('public.'||expected.table_name)
     OR actual.amname<>'gin' OR actual.index_owner<>actual.table_owner
     OR actual.index_options IS NOT NULL
     OR actual.indisunique OR actual.indisprimary OR actual.indisexclusion
     OR actual.indnatts<>1 OR actual.indnkeyatts<>1 OR actual.indpred IS NOT NULL
     OR actual.indkey::TEXT<>expected.key_number::TEXT
     OR actual.expression IS DISTINCT FROM expected.expression
     OR actual.opcname<>'array_ops' OR actual.opcnamespace<>'pg_catalog'::REGNAMESPACE
     OR actual.indoption[0]<>0
     OR actual.indcollation[0] IS DISTINCT FROM
          (CASE WHEN expected.key_number=0 THEN 'default'::REGCOLLATION::OID
                ELSE actual.term_collation END) THEN
   RAISE EXCEPTION 'exact lexeme index definition differs: %',expected.index_name;
  END IF;
  all_ready:=all_ready AND actual.indisvalid AND actual.indisready
                         AND actual.indislive AND NOT actual.indcheckxmin;
 END LOOP;
 RETURN all_ready;
END $ready$;
ALTER FUNCTION storage_v2_exact_lexeme_probes_ready() OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION storage_v2_exact_lexeme_probes_ready() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION storage_v2_exact_lexeme_probes_ready() TO mainrag,mainrag_v2_frontier_owner;

DO $readers$
DECLARE expected RECORD; definition TEXT; previous_metadata JSONB; patched TEXT;
        marker TEXT; replacement TEXT;
BEGIN
 FOR expected IN SELECT * FROM (VALUES
  ('storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)',
   '2cbd8ce3db73fae9f6b323d78eeab6d805911d0faa9c6cfe466d0b02d07c26c5',
   'mainrag_v2_lexical_rank_owner'),
  ('storage_v2_scoped_query_posting(bigint[],text[])',
   '457bdfccb701e3ebb5aad7cea192e20767d8b63684851b2d361eb9e3cf5b0d84','mainrag')
 ) routines(signature,definition_sha256,owner_name) LOOP
  definition:=pg_get_functiondef(expected.signature::REGPROCEDURE);
  SELECT to_jsonb(p)-'prosrc' INTO previous_metadata FROM pg_proc p
   WHERE p.oid=expected.signature::REGPROCEDURE;
  IF encode(sha256(convert_to(definition,'UTF8')),'hex') IS DISTINCT FROM expected.definition_sha256
     OR (SELECT proowner FROM pg_proc WHERE oid=expected.signature::REGPROCEDURE)
          <>expected.owner_name::REGROLE
     OR EXISTS(SELECT 1 FROM pg_proc p
       CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
       WHERE p.oid=expected.signature::REGPROCEDURE
         AND (a.grantee<>p.proowner OR a.privilege_type<>'EXECUTE')) THEN
   RAISE EXCEPTION 'exact lexeme reader predecessor or authority differs: %',expected.signature;
  END IF;
  patched:=definition;
  IF expected.owner_name='mainrag_v2_lexical_rank_owner' THEN
   marker:='    lazy_candidate RECORD;';
   replacement:=marker||E'\n    lazy_exact_ready BOOLEAN := public.storage_v2_exact_lexeme_probes_ready();\n    lazy_terms TEXT[] := tsvector_to_array(to_tsvector(''simple'',p_query));\n    lazy_exact_cursor INTEGER;';
   IF (length(patched)-length(replace(patched,marker,'')))/length(marker)<>1 THEN
    RAISE EXCEPTION 'native exact lexeme declaration boundary differs'; END IF;
   patched:=replace(patched,marker,replacement);
   marker:=$old$               AND block.fingerprints @> lazy_fingerprints
        ), candidates AS MATERIALIZED ($old$;
   replacement:=$new$               AND block.fingerprints @> lazy_fingerprints
               AND (NOT lazy_exact_ready OR
                    public.storage_v2_compact_lexical_exact_lexemes(block.fts_vectors) @> lazy_terms)
        ), candidates AS MATERIALIZED ($new$;
   IF (length(patched)-length(replace(patched,marker,'')))/length(marker)<>1 THEN
    RAISE EXCEPTION 'native exact lexeme candidate boundary differs'; END IF;
   patched:=replace(patched,marker,replacement);
   marker:='                   min(block.block_order) AS first_block_order';
   replacement:=marker||E',\n                   CASE WHEN lazy_exact_ready\n                        THEN array_agg(block.block_order ORDER BY block.block_order)\n                        ELSE NULL::BIGINT[] END AS exact_block_orders';
   IF (length(patched)-length(replace(patched,marker,'')))/length(marker)<>1 THEN
    RAISE EXCEPTION 'native exact lexeme key list boundary differs'; END IF;
   patched:=replace(patched,marker,replacement);
   marker:='        lazy_after_block := -1;';
   IF (length(patched)-length(replace(patched,marker,'')))/length(marker)<>1 THEN
    RAISE EXCEPTION 'native exact lexeme cursor boundary differs'; END IF;
   patched:=replace(patched,marker,marker||E'\n        lazy_exact_cursor := 1;');
   marker:=$old$            SELECT block.block_order INTO lazy_next_block
              FROM public.storage_v2_compact_lexical_block block
             WHERE block.occurrence_id = lazy_candidate.occurrence_id
               AND block.block_order > lazy_after_block
               AND CASE WHEN block.source_id = lazy_candidate.source_id
                        THEN block.artifact_version_id = lazy_candidate.artifact_version_id
                        ELSE FALSE END
               AND block.fingerprints @> lazy_fingerprints
             ORDER BY block.block_order
             LIMIT 1;
            IF NOT FOUND THEN EXIT; END IF;$old$;
   replacement:=$new$            IF lazy_exact_ready THEN
                -- These narrow keys were already exact-union filtered in the
                -- custom-planned source scan. A conjunction can span different
                -- segments, so retain the original same-segment vector recheck.
                -- Advance this ordered key list rather than reopening a global
                -- GIN scan or loading sixteen-bit collision blocks on a miss.
                lazy_exact_cursor := lazy_exact_cursor + 1;
                lazy_next_block := lazy_candidate.exact_block_orders[lazy_exact_cursor];
                IF lazy_next_block IS NULL THEN EXIT; END IF;
            ELSE
$new$||marker||E'\n            END IF;';
   IF (length(patched)-length(replace(patched,marker,'')))/length(marker)<>1 THEN
    RAISE EXCEPTION 'native exact lexeme next-block boundary differs'; END IF;
   patched:=replace(patched,marker,replacement);
  ELSE
   marker:='    v_terms TEXT[];';
   IF (length(patched)-length(replace(patched,marker,'')))/length(marker)<>1 THEN
    RAISE EXCEPTION 'posting exact lexeme declaration boundary differs'; END IF;
   patched:=replace(patched,marker,marker||E'\n    v_exact_lexeme_probes BOOLEAN := public.storage_v2_exact_lexeme_probes_ready();');
   marker:=$old$         WHERE block.fingerprints && public.storage_v2_posting_fingerprints(v_terms)
           AND block.document_id BETWEEN v_min_document AND v_max_document$old$;
   replacement:=marker||E'\n           AND (NOT v_exact_lexeme_probes OR block.terms && v_terms)';
   IF (length(patched)-length(replace(patched,marker,'')))/length(marker)<>1 THEN
    RAISE EXCEPTION 'posting exact lexeme candidate boundary differs'; END IF;
   patched:=replace(patched,marker,replacement);
  END IF;
  EXECUTE patched;
  IF (SELECT to_jsonb(p)-'prosrc' FROM pg_proc p WHERE p.oid=expected.signature::REGPROCEDURE)
       IS DISTINCT FROM previous_metadata THEN
   RAISE EXCEPTION 'exact lexeme reader authority or configuration changed';
  END IF;
 END LOOP;
 -- Only the audited scoped reader definition changes in the existing23-member
 -- converter contract. Every owner/ACL, other function and catalog pin stays.
 UPDATE storage_v2_posting_conversion_contract
 SET definition_sha256=sha256(convert_to(pg_get_functiondef(signature::REGPROCEDURE),'UTF8'))
 WHERE signature='storage_v2_scoped_query_posting(bigint[],text[])'
   AND definition_sha256=decode('457bdfccb701e3ebb5aad7cea192e20767d8b63684851b2d361eb9e3cf5b0d84','hex');
 IF NOT FOUND THEN RAISE EXCEPTION 'scoped posting converter predecessor pin differs'; END IF;
END $readers$;
COMMIT;
