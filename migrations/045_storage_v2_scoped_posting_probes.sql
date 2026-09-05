-- Migration 045: keep common-term lookup inside the authorized document scope.
-- A global term-hash scan can look deceptively cheap when statistics estimate
-- one match. Probe the fixed-width document key first, then check authoritative
-- term equality outside that lookup. OFFSET 0 is an intentional pull-up barrier,
-- not a result cap. Reuse the complete scoped postings for all three consumers.

DO $$
DECLARE
    v_signature REGPROCEDURE :=
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE;
    v_definition TEXT := pg_get_functiondef(v_signature);
    v_old TEXT := $old$    document_frequency AS (
        SELECT posting.term, COUNT(DISTINCT binding.occurrence_id)::DOUBLE PRECISION AS frequency
          FROM scoped_binding binding
          JOIN storage_v2_search_posting posting ON posting.document_id = binding.document_id
          CROSS JOIN query_values query
         WHERE posting.term_sha256 = ANY(query.term_hashes)
           AND posting.term = ANY(query.terms)
         GROUP BY posting.term
    ),
    term_rows AS (
        SELECT binding.occurrence_id, posting.term, binding.component_ordinal,
               binding.role_weight,
               binding.role_weight
                 * LN(1 + (stats.view_count + 1.0) / (frequency.frequency + 1.0))
                 * posting.term_frequency
                 / (posting.term_frequency + 0.5
                    + 0.5 * (view_stats.view_length / NULLIF(stats.average_view_length, 0)))
                 AS contribution
          FROM scoped_binding binding
          JOIN view_stats ON view_stats.occurrence_id = binding.occurrence_id
          JOIN storage_v2_search_posting posting ON posting.document_id = binding.document_id
          JOIN document_frequency frequency ON frequency.term = posting.term
          CROSS JOIN corpus_stats stats
          CROSS JOIN query_values query
         WHERE posting.term_sha256 = ANY(query.score_term_hashes)
           AND posting.term = ANY(query.score_terms)
    ),
    term_match_aggregate AS (
        SELECT binding.occurrence_id,
               array_agg(DISTINCT posting.term ORDER BY posting.term) AS matched_terms
          FROM scoped_binding binding
          JOIN storage_v2_search_posting posting ON posting.document_id = binding.document_id
          CROSS JOIN query_values query
         WHERE posting.term_sha256 = ANY(query.term_hashes)
           AND posting.term = ANY(query.terms)
         GROUP BY binding.occurrence_id
    ),$old$;
    v_new TEXT := $new$    scoped_posting AS MATERIALIZED (
        SELECT binding.occurrence_id, binding.component_ordinal, binding.role_weight,
               posting.term, posting.term_frequency
          FROM scoped_binding binding
          CROSS JOIN query_values query
          CROSS JOIN LATERAL (
              SELECT term, term_frequency FROM storage_v2_search_posting
               WHERE document_id = binding.document_id
                 AND term_sha256 = ANY(query.term_hashes)
               OFFSET 0
          ) posting
         WHERE posting.term = ANY(query.terms)
    ),
    document_frequency AS (
        SELECT term, COUNT(DISTINCT occurrence_id)::DOUBLE PRECISION AS frequency
          FROM scoped_posting GROUP BY term
    ),
    term_rows AS (
        SELECT posting.occurrence_id, posting.term, posting.component_ordinal,
               posting.role_weight,
               posting.role_weight
                 * LN(1 + (stats.view_count + 1.0) / (frequency.frequency + 1.0))
                 * posting.term_frequency
                 / (posting.term_frequency + 0.5
                    + 0.5 * (view_stats.view_length / NULLIF(stats.average_view_length, 0)))
                 AS contribution
          FROM scoped_posting posting
          JOIN view_stats ON view_stats.occurrence_id = posting.occurrence_id
          JOIN document_frequency frequency ON frequency.term = posting.term
          CROSS JOIN corpus_stats stats
          CROSS JOIN query_values query
         WHERE posting.term = ANY(query.score_terms)
    ),
    term_match_aggregate AS (
        SELECT occurrence_id, array_agg(DISTINCT term ORDER BY term) AS matched_terms
          FROM scoped_posting GROUP BY occurrence_id
    ),$new$;
BEGIN
    IF strpos(v_definition, v_new) > 0 THEN
        RETURN;
    END IF;
    IF (length(v_definition) - length(replace(v_definition, v_old, '')))
       / length(v_old) <> 1 THEN
        RAISE EXCEPTION 'storage-v2 posting consumers differ from the reviewed definition';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;
