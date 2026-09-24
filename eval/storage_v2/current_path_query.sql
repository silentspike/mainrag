WITH q AS (
    SELECT {constructor}('simple', {query}) AS simple_q,
           {constructor}('english', {query}) AS english_q
),
channel AS (
    SELECT d.id, d.path,
           ts_rank_cd(d.fts_simple, q.simple_q, 1)::double precision AS score
    FROM documents d CROSS JOIN q WHERE d.fts_simple @@ q.simple_q
    UNION ALL
    SELECT d.id, d.path,
           (ts_rank_cd(d.fts_english, q.english_q, 1) * 0.8)::double precision AS score
    FROM documents d CROSS JOIN q WHERE d.fts_english @@ q.english_q
),
grouped AS (
    SELECT id, path, MAX(score) AS score FROM channel GROUP BY id, path
),
top_results AS (
    SELECT id, path, score FROM grouped
    ORDER BY score DESC, path ASC, id ASC LIMIT 10
)
SELECT json_build_object(
    'matched_documents', (SELECT COUNT(*) FROM grouped),
    'scored_channel_rows', (SELECT COUNT(*) FROM channel),
    'results', COALESCE(
        (SELECT json_agg(path ORDER BY score DESC, path ASC, id ASC) FROM top_results),
        '[]'::json
    )
);
