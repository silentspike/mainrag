-- Exclude simple-conjunction candidates with a nonmatching authoritative
-- lexical projection before document aggregation. Corpus statistics remain
-- complete, and other AST shapes retain their existing path.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $guard$
DECLARE signature TEXT; expected TEXT; callers REGROLE[];
BEGIN
    FOR signature,expected,callers IN SELECT * FROM (VALUES
        ('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
         'edb2ad48632256e3c02065103a23cd37d38332d0acd064746cd1f82d2c1cca30',
         ARRAY['mainrag'::REGROLE,0::OID::REGROLE]),
        ('storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)',
         'c7cba5967e4b6cd47e110a4c878045750e1506413e41f14a8d19efce702386b0',
         ARRAY['mainrag'::REGROLE])
    ) required(signature,expected,callers) LOOP
        IF encode(sha256(convert_to(pg_get_functiondef(signature::REGPROCEDURE),'UTF8')),'hex')<>expected THEN
            RAISE EXCEPTION 'conjunction pruning reader definition differs: %',signature;
        END IF;
        IF (SELECT proowner FROM pg_proc WHERE oid=signature::REGPROCEDURE)<>'mainrag'::REGROLE
           OR NOT has_function_privilege('mainrag',signature,'EXECUTE')
           OR EXISTS (SELECT 1 FROM pg_proc routine CROSS JOIN LATERAL
                aclexplode(coalesce(routine.proacl,acldefault('f',routine.proowner))) permission
                WHERE routine.oid=signature::REGPROCEDURE
                  AND (NOT permission.grantee=ANY(callers)
                    OR permission.privilege_type<>'EXECUTE'
                    OR (permission.is_grantable AND permission.grantee<>routine.proowner))) THEN
            RAISE EXCEPTION 'conjunction pruning reader authority differs: %',signature;
        END IF;
    END LOOP;
END $guard$;

DO $helpers$
BEGIN
    IF encode(sha256(convert_to(pg_get_functiondef(
        'storage_v2_simple_and_query(jsonb)'::REGPROCEDURE),'UTF8')),'hex')
          <> '85fd6d6684c9c4f1b4a8af1e721faf796e2c7d52867f134a8fdd32c1051154f6'
       OR encode(sha256(convert_to(pg_get_functiondef(
        'storage_v2_source_segment_presence(bigint[])'::REGPROCEDURE),'UTF8')),'hex')
          <> '7947e57eecb8ac8afbf147a38331a506ef014aaa1c2794f1a76c73c66312a843' THEN
        RAISE EXCEPTION 'conjunction pruning helper definition differs';
    END IF;
END $helpers$;

DO $prune$
DECLARE signature TEXT; definition TEXT;
old_presence TEXT := $old$THEN (SELECT array_agg(matched.id) FROM matched
                         LEFT JOIN lexical_ranks ranked ON ranked.occurrence_id=matched.id
                        WHERE ranked.occurrence_id IS NULL)$old$;
new_presence TEXT := $new$THEN (SELECT array_agg(scope.occurrence_id)
                         FROM (SELECT DISTINCT occurrence_id FROM scoped_posting) scope
                         LEFT JOIN lexical_ranks ranked ON ranked.occurrence_id=scope.occurrence_id
                        WHERE ranked.occurrence_id IS NULL)$new$;
old_matches TEXT := $old$         GROUP BY occurrence_id
    ),
    best_term AS ($old$;
new_matches TEXT := $new$           AND (v_simple_and_query IS NULL OR occurrence_id NOT IN (
               SELECT occurrence_id FROM lexical_presence
           ))
         GROUP BY occurrence_id
    ),
    best_term AS ($new$;
old_scores TEXT := $old$         WHERE posting.term = ANY(query.score_terms)
           AND posting.occurrence_id NOT IN$old$;
new_scores TEXT := $new$         WHERE posting.term = ANY(query.score_terms)
           AND (v_simple_and_query IS NULL OR posting.occurrence_id NOT IN (
               SELECT occurrence_id FROM lexical_presence
           ))
           AND posting.occurrence_id NOT IN$new$;
old_evidence TEXT := $old$        SELECT occurrence_id FROM term_match_aggregate
        UNION SELECT occurrence_id FROM phrase_aggregate$old$;
new_evidence TEXT := $new$        SELECT occurrence_id FROM term_match_aggregate
         WHERE v_simple_and_query IS NULL
            OR matched_terms @> (SELECT terms FROM query_values)
        UNION SELECT occurrence_id FROM phrase_aggregate$new$;
marker TEXT;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        definition:=pg_get_functiondef(signature::REGPROCEDURE);
        FOREACH marker IN ARRAY ARRAY[old_presence,old_matches,old_scores,old_evidence] LOOP
            IF (length(definition)-length(replace(definition,marker,'')))/length(marker)<>1 THEN
                RAISE EXCEPTION 'conjunction pruning reader boundary differs: %',signature;
            END IF;
        END LOOP;
        definition:=replace(definition,old_presence,new_presence);
        definition:=replace(definition,old_matches,new_matches);
        definition:=replace(definition,old_scores,new_scores);
        definition:=replace(definition,old_evidence,new_evidence);
        EXECUTE definition;
    END LOOP;
END $prune$;
COMMIT;
