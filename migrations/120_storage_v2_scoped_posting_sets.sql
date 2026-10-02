-- Keep large document scopes as sets and filter compact blocks before expansion.
-- Exact document IDs, term text, hash checks and every matching block item remain.
BEGIN;
DO $guard$ BEGIN
 IF encode(sha256(convert_to(pg_get_functiondef('storage_v2_scoped_term_posting(bigint[],text)'::regprocedure),'UTF8')),'hex') <> 'bdb55c72f3d33a34219ae93c468a25697cde4a37fa2e78442ed6860c45237f2b'
    AND NOT EXISTS (SELECT 1 FROM pg_proc routine
      WHERE routine.oid='storage_v2_scoped_term_posting(bigint[],text)'::regprocedure
        AND encode(sha256(convert_to(routine.prosrc,'UTF8')),'hex')='9466a739be9f10127a1c83babaa06feb4059b1971b65b3d73d556ff5a828cf35'
        AND routine.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')
        AND routine.provolatile='s' AND routine.proisstrict AND NOT routine.prosecdef
        AND routine.proretset AND routine.prorettype='record'::regtype
        AND routine.proconfig=ARRAY['search_path=pg_catalog, public, pg_temp','plan_cache_mode=force_custom_plan','enable_nestloop=off','jit=off']) THEN
  RAISE EXCEPTION 'scoped posting definition differs';
 END IF;
 IF (SELECT proowner FROM pg_proc WHERE oid='storage_v2_scoped_term_posting(bigint[],text)'::regprocedure) <> 'mainrag'::regrole THEN
  RAISE EXCEPTION 'scoped posting authority differs';
 END IF;
 IF EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
   aclexplode(COALESCE(routine.proacl,acldefault('f',routine.proowner))) permission
   WHERE routine.oid='storage_v2_scoped_term_posting(bigint[],text)'::regprocedure
     AND (permission.grantee <> 'mainrag'::regrole OR permission.privilege_type <> 'EXECUTE'
       OR (permission.is_grantable AND permission.grantee <> routine.proowner))) THEN
  RAISE EXCEPTION 'scoped posting authority differs';
 END IF;
END $guard$;
CREATE OR REPLACE FUNCTION public.storage_v2_scoped_term_posting(p_document_ids bigint[], p_term text)
 RETURNS TABLE(document_id bigint, term text, term_frequency bigint)
 LANGUAGE plpgsql
 STABLE STRICT
 SET search_path TO 'pg_catalog', 'public', 'pg_temp'
 SET plan_cache_mode TO 'force_custom_plan'
 SET enable_nestloop TO 'off'
 SET jit TO 'off'
AS $function$
BEGIN
    IF cardinality(p_document_ids) <= 1024 THEN
        RETURN QUERY
        SELECT requested.id,posting.term,posting.term_frequency
          FROM (SELECT DISTINCT id FROM unnest(p_document_ids) input(id)) requested
          CROSS JOIN LATERAL (
              SELECT probe.term,probe.term_frequency
                FROM public.storage_v2_document_posting(requested.id,p_term) probe OFFSET 0
          ) posting;
    ELSE
        RETURN QUERY
        WITH requested AS MATERIALIZED (
            SELECT DISTINCT id FROM unnest(p_document_ids) input(id)
        ), ordinary_posting AS MATERIALIZED (
            SELECT probe.document_id,probe.term,probe.term_frequency
              FROM public.storage_v2_search_posting probe
             WHERE probe.term=p_term
               AND CASE WHEN probe.term_sha256=public.digest(p_term,'sha256')
                        THEN TRUE ELSE FALSE END OFFSET 0
        ), matching_block AS MATERIALIZED (
            SELECT block.document_id,block.terms,block.term_frequencies
              FROM public.storage_v2_compact_posting_block block
             WHERE block.fingerprints @> ARRAY[public.storage_v2_posting_fingerprint(p_term)]
               AND EXISTS (SELECT 1 FROM requested WHERE requested.id=block.document_id)
        )
        SELECT posting.document_id,posting.term,posting.term_frequency::BIGINT
          FROM ordinary_posting posting
         WHERE EXISTS (SELECT 1 FROM requested WHERE requested.id=posting.document_id)
        UNION ALL
        SELECT block.document_id,item.term,item.frequency
          FROM matching_block block
          CROSS JOIN LATERAL unnest(block.terms,block.term_frequencies) item(term,frequency)
         WHERE item.term=p_term;
    END IF;
END
$function$;
COMMIT;
