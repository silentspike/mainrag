-- A term index probe must not enumerate postings from an unrelated large
-- corpus. Keep a bounded complete rare-term probe; if it overflows, discard
-- its partial rows and read the full posting set of distinct scoped documents.
-- No term, result, score, corpus statistic, or tie boundary is truncated.
DO $migration$
DECLARE
    v_signature TEXT;
    v_definition TEXT;
    v_old TEXT := $old$    query_posting AS MATERIALIZED (
        SELECT posting.document_id, posting.term, posting.term_frequency
          FROM query_values query
          CROSS JOIN unnest(query.terms) AS requested_term(value)
          CROSS JOIN LATERAL (
              SELECT document_id, term, term_frequency
                FROM storage_v2_search_posting
               WHERE term = requested_term.value
                 AND term_sha256 = ANY(query.term_hashes)
               OFFSET 0
          ) posting
    ),$old$;
    v_new TEXT := $new$    query_posting_probe AS MATERIALIZED (
        SELECT posting.document_id, posting.term, posting.term_frequency
          FROM query_values query
          CROSS JOIN unnest(query.terms) AS requested_term(value)
          CROSS JOIN LATERAL (
              SELECT document_id, term, term_frequency
                FROM storage_v2_search_posting
               WHERE term = requested_term.value
                 AND term_sha256 = ANY(query.term_hashes)
               LIMIT 4097
               OFFSET 0
          ) posting
    ),
    term_probe_state AS MATERIALIZED (
        SELECT term, COUNT(*) > 4096 AS overflow
          FROM query_posting_probe GROUP BY term
    ),
    scoped_document AS MATERIALIZED (
        SELECT DISTINCT document_id FROM scoped_binding
    ),
    query_posting AS MATERIALIZED (
        SELECT probe.document_id, probe.term, probe.term_frequency
          FROM query_posting_probe probe
          JOIN term_probe_state state ON state.term = probe.term AND NOT state.overflow
        UNION ALL
        SELECT document.document_id, posting.term, posting.term_frequency
          FROM term_probe_state state
          CROSS JOIN scoped_document document
          CROSS JOIN LATERAL (
              SELECT term, term_frequency FROM storage_v2_search_posting
               WHERE document_id = document.document_id
                 AND term_sha256 = digest(state.term, 'sha256')
               OFFSET 0
          ) posting
         WHERE state.overflow AND posting.term = state.term
    ),$new$;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ] LOOP
        v_definition := pg_get_functiondef(v_signature::REGPROCEDURE);
        IF (length(v_definition)-length(replace(v_definition,v_new,'')))/length(v_new)=1
           AND strpos(v_definition,v_old)=0 THEN CONTINUE; END IF;
        IF (length(v_definition)-length(replace(v_definition,v_old,'')))/length(v_old)<>1
           OR strpos(v_definition,v_new)>0 THEN
            RAISE EXCEPTION 'query posting definition differs before bounded term probes';
        END IF;
        EXECUTE replace(v_definition,v_old,v_new);
    END LOOP;
END
$migration$;
