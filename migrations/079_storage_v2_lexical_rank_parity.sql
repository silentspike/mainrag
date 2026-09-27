-- Lexical segments prove source-backed support, but they must not replace the
-- established posting score used to order the retrieval view. The former
-- million-point segment bonus caused newly projected occurrences to displace
-- baseline paths even when their legacy lexical score was lower. Keep the
-- segment rank in the matching gate and use the existing lexical score for
-- final ordering so candidate and legacy paths retain the same order.
DO $$
DECLARE
    v_function REGPROCEDURE;
    v_sql TEXT;
    v_old TEXT := E'COALESCE(1000000.0 + staged.segment_score, lexical_score)\n                 + graph_score + semantic_score + rerank_score AS final_score';
    v_new TEXT := 'lexical_score + graph_score + semantic_score + rerank_score AS final_score';
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'::REGPROCEDURE,
        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'::REGPROCEDURE
    ] LOOP
        v_sql := pg_get_functiondef(v_function);
        IF strpos(v_sql, v_old) = 0 THEN
            RAISE EXCEPTION 'storage-v2 lexical ranking definition changed before parity repair';
        END IF;
        EXECUTE replace(v_sql, v_old, v_new);
    END LOOP;
END
$$;
