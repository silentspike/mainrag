-- Keep sparse membership probes while exposing dense scopes as semijoins.
-- Mutually exclusive branches prevent correlated requested-set rescans.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $guard$
DECLARE expected RECORD; routine RECORD;
BEGIN
    FOR expected IN SELECT * FROM (VALUES
        ('storage_v2_scoped_query_posting(bigint[],text[])','c64e60492b5bad63d1a6cafe93a6750b50a6f36154256a895e6068ede7da968e','mainrag',false,'mainrag'),
        ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])','82701ec27244a73c89d6cce6ed6a48a33b48d21192e46464601ddc79720878d7','mainrag_v2_frontier_owner',true,'mainrag'),
        ('storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)','1e75b6a661c92e177ca42c272b098643662f215f3bdf491a9a4b8db8f3c47112','mainrag_v2_lexical_rank_owner',true,'mainrag_v2_frontier_owner')
    ) AS x(signature,sha256,owner_name,definer,caller_name) LOOP
        SELECT * INTO STRICT routine FROM pg_proc WHERE oid=expected.signature::REGPROCEDURE;
        IF encode(sha256(convert_to(pg_get_functiondef(routine.oid),'UTF8')),'hex')<>expected.sha256
           OR routine.proowner<>expected.owner_name::REGROLE
           OR routine.prosecdef<>expected.definer
           OR routine.proisstrict IS DISTINCT FROM
                TRUE
           OR routine.provolatile<>'s'
           OR NOT has_function_privilege(expected.caller_name,routine.oid,'EXECUTE')
           OR EXISTS(SELECT 1 FROM aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) a
                WHERE a.grantee NOT IN (routine.proowner,expected.caller_name::REGROLE)
                   OR a.privilege_type<>'EXECUTE'
                   OR (a.is_grantable AND a.grantee<>routine.proowner)) THEN
            RAISE EXCEPTION 'dense scope reader definition or authority differs: %',expected.signature;
        END IF;
    END LOOP;
END $guard$;

DO $scopes$
DECLARE patch RECORD; definition TEXT;
BEGIN
    FOR patch IN SELECT * FROM (VALUES
        ('storage_v2_scoped_query_posting(bigint[],text[])',$old0$SELECT candidate.* FROM ordinary_candidate candidate
          WHERE CASE WHEN EXISTS(SELECT 1 FROM ordinary_candidate OFFSET 32)
                     THEN EXISTS(SELECT 1 FROM requested WHERE requested.id=candidate.document_id)
                     ELSE candidate.document_id=ANY(p_document_ids) END$old0$,$new0$SELECT candidate.* FROM ordinary_candidate candidate
          WHERE NOT EXISTS(SELECT 1 FROM ordinary_candidate OFFSET 32)
            AND candidate.document_id=ANY(p_document_ids)
        UNION ALL
        SELECT candidate.* FROM ordinary_candidate candidate
          WHERE EXISTS(SELECT 1 FROM ordinary_candidate OFFSET 32)
            AND EXISTS(SELECT 1 FROM requested WHERE requested.id=candidate.document_id)$new0$),
        ('storage_v2_scoped_query_posting(bigint[],text[])',$old1$SELECT block.* FROM matching_block block
          WHERE CASE WHEN EXISTS(SELECT 1 FROM matching_block OFFSET 32)
                     THEN EXISTS(SELECT 1 FROM requested WHERE requested.id=block.document_id)
                     ELSE block.document_id=ANY(p_document_ids) END$old1$,$new1$SELECT block.* FROM matching_block block
          WHERE NOT EXISTS(SELECT 1 FROM matching_block OFFSET 32)
            AND block.document_id=ANY(p_document_ids)
        UNION ALL
        SELECT block.* FROM matching_block block
          WHERE EXISTS(SELECT 1 FROM matching_block OFFSET 32)
            AND EXISTS(SELECT 1 FROM requested WHERE requested.id=block.document_id)$new1$),
        ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])',$old2$SELECT projection.* FROM unscoped_projection projection
         WHERE CASE WHEN EXISTS(SELECT 1 FROM unscoped_projection OFFSET 32)
                    THEN EXISTS(SELECT 1 FROM requested WHERE requested.id=projection.occurrence_id)
                    ELSE projection.occurrence_id=ANY(p_occurrence_ids) END$old2$,$new2$SELECT projection.* FROM unscoped_projection projection
          WHERE NOT EXISTS(SELECT 1 FROM unscoped_projection OFFSET 32)
            AND projection.occurrence_id=ANY(p_occurrence_ids)
        UNION ALL
        SELECT projection.* FROM unscoped_projection projection
          WHERE EXISTS(SELECT 1 FROM unscoped_projection OFFSET 32)
            AND EXISTS(SELECT 1 FROM requested WHERE requested.id=projection.occurrence_id)$new2$),
        ('storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)',$old3$SELECT matching.* FROM matching
          -- Avoid expanding a large requested set when exact native matches are absent.
          WHERE EXISTS (SELECT 1 FROM matching)
            AND CASE WHEN EXISTS(SELECT 1 FROM matching OFFSET 32)
                     THEN EXISTS(SELECT 1 FROM requested WHERE requested.id=matching.occurrence_id)
                     ELSE matching.occurrence_id=ANY(p_occurrence_ids) END$old3$,$new3$SELECT matching.* FROM matching
          WHERE NOT EXISTS(SELECT 1 FROM matching OFFSET 32)
            AND matching.occurrence_id=ANY(p_occurrence_ids)
        UNION ALL
        SELECT matching.* FROM matching
          WHERE EXISTS(SELECT 1 FROM matching OFFSET 32)
            AND EXISTS(SELECT 1 FROM requested WHERE requested.id=matching.occurrence_id)$new3$)
    ) x(signature,old_scope,new_scope) LOOP
        definition:=pg_get_functiondef(patch.signature::REGPROCEDURE);
        IF (length(definition)-length(replace(definition,patch.old_scope,'')))/length(patch.old_scope)<>1 THEN
            RAISE EXCEPTION 'dense scope reader replacement boundary differs';
        END IF;
        EXECUTE replace(definition,patch.old_scope,patch.new_scope);
    END LOOP;
END $scopes$;
COMMIT;
