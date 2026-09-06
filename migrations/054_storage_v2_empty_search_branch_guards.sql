-- Migration 054: do not fetch document text/identifiers for empty query classes.
-- Scalar subqueries become one-time filters before the document join. The
-- existing unnest/match predicates and complete scoring scope are unchanged.
DO $$
DECLARE
    v_signature REGPROCEDURE :=
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE;
    v_definition TEXT := pg_get_functiondef(v_signature);
    v_old TEXT[] := ARRAY[
        'WHERE storage_v2_phrase_matches(binding.fts_simple, binding.search_text, phrase.value)',
        'WHERE exact.value = ANY(binding.exact_identifiers)'
    ];
    v_new TEXT[] := ARRAY[
        'WHERE cardinality((SELECT phrases FROM query_values)) > 0 AND storage_v2_phrase_matches(binding.fts_simple, binding.search_text, phrase.value)',
        'WHERE cardinality((SELECT exact_values FROM query_values)) > 0 AND exact.value = ANY(binding.exact_identifiers)'
    ];
    v_old_count INTEGER := 0;
    v_new_count INTEGER := 0;
    v_index INTEGER;
    v_before INTEGER;
    v_after INTEGER;
BEGIN
    IF strpos(v_definition, 'phrase_aggregate AS MATERIALIZED (') = 0
       OR strpos(v_definition, 'exact_aggregate AS MATERIALIZED (') = 0 THEN
        RAISE EXCEPTION 'storage-v2 empty-branch materialization prerequisite differs';
    END IF;
    FOR v_index IN 1..2 LOOP
        v_before := (length(v_definition) - length(replace(v_definition, v_old[v_index], '')))
            / length(v_old[v_index]);
        v_after := (length(v_definition) - length(replace(v_definition, v_new[v_index], '')))
            / length(v_new[v_index]);
        IF v_before + v_after <> 1 THEN
            RAISE EXCEPTION 'storage-v2 empty-branch predicate prerequisite differs';
        END IF;
        v_old_count := v_old_count + v_before;
        v_new_count := v_new_count + v_after;
    END LOOP;
    IF v_new_count = 2 AND v_old_count = 0 THEN
        RETURN;
    END IF;
    IF v_old_count <> 2 OR v_new_count <> 0 THEN
        RAISE EXCEPTION 'storage-v2 empty-branch definition is partially guarded';
    END IF;
    FOR v_index IN 1..2 LOOP
        v_definition := replace(v_definition, v_old[v_index], v_new[v_index]);
    END LOOP;
    EXECUTE v_definition;
END
$$;
