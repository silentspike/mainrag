-- Migration 044: avoid composing every scoped view before selecting top-k.
-- Scoring still evaluates the complete authorized scope. Only result text is
-- deferred; counts, score normalization, tie order and phrase semantics stay
-- unchanged. Posting probes use the full fixed-width primary key, retaining
-- authoritative term equality as a collision guard.

DO $$
DECLARE
    v_signature REGPROCEDURE :=
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE;
    v_definition TEXT := pg_get_functiondef(v_signature);
    v_old TEXT[] := ARRAY[
        $old$AS terms,$old$,
        $old$AS score_terms,$old$,
        $old$SELECT occurrence_id, SUM(token_count)::DOUBLE PRECISION AS view_length,
               string_agg(search_text, E'\n' ORDER BY component_ordinal) AS content
          FROM scoped_binding GROUP BY occurrence_id$old$,
        $old$WHERE posting.term = ANY(query.terms)$old$,
        $old$WHERE posting.term = ANY(query.score_terms)$old$,
        $old$SELECT visible.*, view_stats.content, view_stats.view_length,$old$,
        $old$'content', content,$old$,
        $old$binding.role_weight, document.search_text, document.token_count,
               document.exact_identifiers, document.fts_simple$old$,
        $old$FROM scoped_binding binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.phrases)$old$,
        $old$FROM scoped_binding binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.exact_values)$old$
    ];
    v_new TEXT[] := ARRAY[
        $new$AS terms,
            COALESCE(array_agg(DISTINCT digest(value, 'sha256'))
                FILTER (WHERE kind = 'term'), ARRAY[]::BYTEA[]) AS term_hashes,$new$,
        $new$AS score_terms,
            COALESCE(array_agg(DISTINCT digest(value, 'sha256'))
                FILTER (WHERE kind = 'term' AND NOT negated), ARRAY[]::BYTEA[]) AS score_term_hashes,$new$,
        $new$SELECT occurrence_id, SUM(token_count)::DOUBLE PRECISION AS view_length
          FROM scoped_binding GROUP BY occurrence_id$new$,
        $new$WHERE posting.term_sha256 = ANY(query.term_hashes)
           AND posting.term = ANY(query.terms)$new$,
        $new$WHERE posting.term_sha256 = ANY(query.score_term_hashes)
           AND posting.term = ANY(query.score_terms)$new$,
        $new$SELECT visible.*, view_stats.view_length,$new$,
        $new$'content', (
                SELECT string_agg(document.search_text, E'\n' ORDER BY binding.ordinal)
                  FROM storage_v2_search_view_document binding
                  JOIN storage_v2_search_document document ON document.id = binding.document_id
                 WHERE binding.view_id = ordered.view_id
            ),$new$,
        $new$binding.role_weight, document.token_count$new$,
        $new$FROM (
              SELECT scope.occurrence_id, document.fts_simple, document.search_text
                FROM scoped_binding scope
                JOIN storage_v2_search_document document ON document.id = scope.document_id
          ) binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.phrases)$new$,
        $new$FROM (
              SELECT scope.occurrence_id, document.exact_identifiers
                FROM scoped_binding scope
                JOIN storage_v2_search_document document ON document.id = scope.document_id
          ) binding
          CROSS JOIN query_values query
          CROSS JOIN unnest(query.exact_values)$new$
    ];
    v_expected INTEGER[] := ARRAY[1, 1, 1, 2, 1, 1, 1, 1, 1, 1];
    v_count INTEGER;
BEGIN
    FOR i IN 1..array_length(v_old, 1) LOOP
        v_count := (length(v_definition) - length(replace(v_definition, v_new[i], '')))
                   / length(v_new[i]);
        IF v_count = v_expected[i] THEN
            CONTINUE;
        END IF;
        IF v_count <> 0 OR
           (length(v_definition) - length(replace(v_definition, v_old[i], '')))
               / length(v_old[i]) <> v_expected[i] THEN
            RAISE EXCEPTION 'storage-v2 retrieval definition differs at substitution %', i;
        END IF;
        v_definition := replace(v_definition, v_old[i], v_new[i]);
    END LOOP;
    EXECUTE v_definition;
END
$$;
