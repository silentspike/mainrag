-- Migration 052: compute each complete per-occurrence search aggregate once.
-- Inlined grouped relations can be rescanned for every visible occurrence by
-- generic nested-loop plans. Materialization retains the full authorized scope;
-- it introduces no candidate cap and changes no scoring or ranking expression.
-- Source/generation cardinalities differ substantially. Bind this function to
-- custom plans so each execution can use its actual scope and query parameters;
-- the function-local setting is restored on return, including error paths.

DO $$
DECLARE
    v_signature REGPROCEDURE :=
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE;
    v_definition TEXT := pg_get_functiondef(v_signature);
    v_name TEXT;
    v_old TEXT;
    v_new TEXT;
    v_old_count INTEGER := 0;
    v_new_count INTEGER := 0;
    v_custom_plan BOOLEAN;
BEGIN
    SELECT COALESCE('plan_cache_mode=force_custom_plan' = ANY(proconfig), FALSE)
      INTO v_custom_plan FROM pg_proc WHERE oid = v_signature;
    IF EXISTS (
        SELECT 1 FROM pg_proc CROSS JOIN LATERAL unnest(proconfig) setting
         WHERE oid = v_signature AND setting LIKE 'plan_cache_mode=%'
           AND setting <> 'plan_cache_mode=force_custom_plan'
    ) THEN
        RAISE EXCEPTION 'storage-v2 search plan configuration differs from the reviewed baseline';
    END IF;
    IF strpos(v_definition, 'scoped_posting AS MATERIALIZED (') = 0 THEN
        RAISE EXCEPTION 'storage-v2 scoped posting prerequisite is missing';
    END IF;
    FOREACH v_name IN ARRAY ARRAY[
        'term_match_aggregate', 'term_aggregate', 'phrase_aggregate', 'exact_aggregate'
    ] LOOP
        v_old := E'\n    ' || v_name || ' AS (';
        v_new := E'\n    ' || v_name || ' AS MATERIALIZED (';
        v_old_count := v_old_count +
            (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old);
        v_new_count := v_new_count +
            (length(v_definition) - length(replace(v_definition, v_new, ''))) / length(v_new);
        IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old)
             + (length(v_definition) - length(replace(v_definition, v_new, ''))) / length(v_new) <> 1 THEN
            RAISE EXCEPTION 'storage-v2 search aggregate definition differs: %', v_name;
        END IF;
    END LOOP;
    IF v_new_count = 4 AND v_old_count = 0 THEN
        IF NOT v_custom_plan THEN
            EXECUTE 'ALTER FUNCTION storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint) '
                 || 'SET plan_cache_mode = force_custom_plan';
        END IF;
        RETURN;
    END IF;
    IF v_old_count <> 4 OR v_new_count <> 0 THEN
        RAISE EXCEPTION 'storage-v2 search aggregate definition is partially materialized';
    END IF;
    FOREACH v_name IN ARRAY ARRAY[
        'term_match_aggregate', 'term_aggregate', 'phrase_aggregate', 'exact_aggregate'
    ] LOOP
        v_definition := replace(v_definition, E'\n    ' || v_name || ' AS (',
                                E'\n    ' || v_name || ' AS MATERIALIZED (');
    END LOOP;
    EXECUTE v_definition;
    IF NOT v_custom_plan THEN
        EXECUTE 'ALTER FUNCTION storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint) '
             || 'SET plan_cache_mode = force_custom_plan';
    END IF;
END
$$;
