# Exact composed Top-K prototype

This executable prototype owns the search-feasibility decision for storage v2.
It is isolated from the runtime and uses only synthetic fixtures in a temporary
PostgreSQL cluster.

## Run

From the repository root:

```bash
python3 -m unittest eval/storage_v2/topk/test_prototype.py
python3 eval/storage_v2/topk/prototype.py \
  --commit-sha PROTOTYPE_COMMIT \
  --output eval/storage_v2/topk/results/native-gin.json
```

The default run performs three warmups and 30 measured iterations for every
query. It fails if any SQL Top-10 differs from the exhaustive Python reference,
if a scoped forbidden hit appears, if more than 500 search documents are fully
considered, or if warm p95 reaches 200 ms.

## Physical prototype

- `prototype_document`: one unique indexed body, token count, exact identifiers,
  and a native PostgreSQL `tsvector`/GIN index;
- `prototype_posting`: term frequency and document frequency used by the
  deterministic lexical score;
- `prototype_view_component`: ordered document membership plus role weight;
- `prototype_occurrence`: external hit identity plus tenant/source scope;
- `prototype_view`: graph contribution; and
- `prototype_rerank`: query-bound fixture contribution applied in stage two.

The prepared SQL takes term, phrase, exact-identifier, tenant, source, query, and
JSON-AST parameters. Scope-visible views are established first. Terms are scored
per document, the best contribution per term/view is retained, and Boolean AST
coverage is evaluated at the composed-view level. Phrases must match within one
document component; they never bridge components.

## Correctness and fallback

The prototype intentionally performs a complete evaluation of every scoped view
needed by the Boolean query. It has no 500-row candidate cap. Graph and rerank
bonuses are applied to every Boolean-matched view in a complete second stage.
The recorded upper bound covers those fixture contributions, but the prototype
does not claim a production-safe WAND/MaxScore pruning implementation.

This fallback is acceptable only while the measured fully scored document count
and latency gates pass. Exceeding a gate yields NO-GO; it never changes the
result set to manufacture performance.

## Evidence

Each result artifact contains:

- fixture/query hashes and exact prototype commit;
- backend version and explicit PostgreSQL settings;
- parsed AST and exact SQL/reference Top-10 per query;
- matched postings, fully considered search documents/views, and shortlist size;
- cold-first and warm p50/p95/p99;
- complete `EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON)` output; and
- the bounded backend decision and unresolved production gates.

The artifact is validated by `artifact.schema.json`. It contains no production
query, path, account, host, address, credential, or raw log.

Because the fixture is intentionally small, PostgreSQL may correctly choose
sequential scans over the available GIN index. The recorded plan is authoritative
for this run. Index selection and scale behavior remain qualification gates and
are not inferred from index presence.
