-- Defer sparse ID arrays until execution; retain dense semijoins.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $guard$
DECLARE expected RECORD; routine RECORD;
BEGIN
    FOR expected IN SELECT * FROM (VALUES
        ('storage_v2_scoped_query_posting(bigint[],text[])','080d2ab7cf3227692c0b40706b4f6bc8328f432afa88b2c3297388e52a3208a4','mainrag',false,'mainrag'),
        ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])','fb9df8db184a719f9e164d3d822789b52e6929cc07901e06c435bbfa1761885a','mainrag_v2_frontier_owner',true,'mainrag'),
        ('storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)','3542659c078ade29fff811f8903ef39d88d0c39c54b08dca5563e4582a72dff0','mainrag_v2_lexical_rank_owner',true,'mainrag_v2_frontier_owner')
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
            RAISE EXCEPTION 'bounded array scope reader definition or authority differs: %',expected.signature;
        END IF;
    END LOOP;
END $guard$;

DO $scopes$
DECLARE patch RECORD; definition TEXT;
BEGIN
    FOR patch IN SELECT * FROM (VALUES
        ('storage_v2_scoped_query_posting(bigint[],text[])',$old0$candidate.document_id=ANY(p_document_ids)$old0$,$new0$candidate.document_id=ANY((SELECT p_document_ids OFFSET 0)::BIGINT[])$new0$),
        ('storage_v2_scoped_query_posting(bigint[],text[])',$old1$block.document_id=ANY(p_document_ids)$old1$,$new1$block.document_id=ANY((SELECT p_document_ids OFFSET 0)::BIGINT[])$new1$),
        ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])',$old2$projection.occurrence_id=ANY(p_occurrence_ids)$old2$,$new2$projection.occurrence_id=ANY((SELECT p_occurrence_ids OFFSET 0)::BIGINT[])$new2$),
        ('storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)',$old3$matching.occurrence_id=ANY(p_occurrence_ids)$old3$,$new3$matching.occurrence_id=ANY((SELECT p_occurrence_ids OFFSET 0)::BIGINT[])$new3$)
    ) x(signature,old_scope,new_scope) LOOP
        definition:=pg_get_functiondef(patch.signature::REGPROCEDURE);
        IF (length(definition)-length(replace(definition,patch.old_scope,'')))/length(patch.old_scope)<>1 THEN
            RAISE EXCEPTION 'bounded array scope reader replacement boundary differs';
        END IF;
        EXECUTE replace(definition,patch.old_scope,patch.new_scope);
    END LOOP;
END $scopes$;
COMMIT;
