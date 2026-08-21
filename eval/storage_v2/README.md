# Storage-v2 public baseline harness

This harness measures the repository's current PostgreSQL full-text query shape
against a frozen public fixture. It is the baseline/comparison contract for
storage-v2 work; it does not replace the broader golden-set ownership in
[#34](https://github.com/silentspike/mainrag/issues/34).

The harness starts an isolated temporary PostgreSQL cluster over a private Unix
socket, loads only files under `fixtures/corpus/`, evaluates
`fixtures/queries.jsonl`, validates the result against
`manifest.schema.json`, stops the cluster, removes its temporary directory, and
then writes the manifest. It never connects to the running MainRAG API or a
configured production database.

## Prerequisites

- Python 3.11 or newer with `jsonschema`;
- PostgreSQL 18 client/server tools: `initdb`, `pg_ctl`, and `psql`; and
- a Git checkout containing the commit named as the harness identity.

## Reproduce the committed current-path baseline

From the repository root:

```bash
python3 -m unittest eval/storage_v2/test_harness.py
python3 eval/storage_v2/check_writers.py
python3 eval/storage_v2/harness.py \
  --code-sha b969dc7d4f4fa989999bbf2058fe6af0477afde0 \
  --harness-commit HARNESS_COMMIT \
  --output eval/storage_v2/baselines/current-path.json
```

Replace `HARNESS_COMMIT` with the full commit that introduced the harness. The
default run performs three warmups and 30 measured iterations for every query.
Runs with fewer than 30 measured iterations fail closed.

The committed baseline is successful only when:

- at least one document and query were executed;
- every query produced deterministic exact Top-10 identities across all runs;
- every expected result was recalled in Top-10;
- the read-only writer inventory was complete; and
- the generated manifest passed the checked-in JSON Schema and privacy guard.

## What the manifest measures

- exact result identities with deterministic score/path/ID tie-breaking;
- Recall@10 and MRR@10 per query and in aggregate;
- matched documents and scored channel rows separately from returned results;
- first-before-query-warmups latency separately from warm latency;
- warm p50/p95/p99 across at least 30 iterations per query;
- source/content bytes, parsed documents, unchanged-item reuse, ingest errors,
  elapsed ingest time, and post-ingest relation bytes; and
- code, harness, schema, corpus, query-set, backend, cache, concurrency, and
  timestamp identity.

The first measurement is not an operating-system cold-cache proof. Search
measurements use one persistent `psql` session so process startup is excluded;
the measured wall time still includes local client/server protocol and JSON
formatting. These limitations are recorded in every manifest.

## Query coverage

The frozen suite includes AND, OR, NOT, phrase, grouping, exact identifiers,
common terms, no-match behavior, and intentionally adverse complete-fallback
language. It records current PostgreSQL behavior; it does not claim that current
search has a native grouped-query or exact-identifier channel.

## Writer gate

`check_writers.py` is read-only. It hashes every declared repository-managed
runtime/operator write entrypoint and scans tracked source for undeclared known
write signals. It reports operator actions but never stops a service, process,
timer, or worker. Unknown external writers remain an explicit limitation.

## Shadow-generation adapter

A later storage-v2 adapter must emit the same manifest fields and query-result
identity format. It names an explicit candidate generation and MUST NOT change
active/default pointers. Comparison is valid only when code, corpus, query set,
configuration, cache profile, concurrency, and evidence state are either equal
or explicitly classified as a comparison dimension.

Two runs from the same identities must have the same
`search.result_identity_sha256`, query results, Recall@10, MRR@10, and work
counts. Compare two candidate runs with:

```bash
python3 eval/storage_v2/compare_manifests.py FIRST.json SECOND.json
```

## Supported-API shadow slice

`shadow_slice.py` runs the permanent public fixture through the real filesystem
adapter and storage-v2 API without activating it or writing legacy chunks. The
two phases must straddle an API restart; the verify phase rejects an unchanged
server instance ID and records only hashes, counts, timings, generation IDs and
classified result identities.

```bash
export MAINRAG_TOKEN=REDACTED
python3 eval/storage_v2/prepare_shadow_fixture.py prepare \
  --output /tmp/mainrag-storage-v2-shadow-source
python3 eval/storage_v2/shadow_slice.py \
  --phase ingest \
  --source-path /tmp/mainrag-storage-v2-shadow-source \
  --commit-sha 0123456789012345678901234567890123456789 \
  --checkpoint /tmp/mainrag-storage-v2-shadow.json

# Restart the API without changing its database or pack root.

python3 eval/storage_v2/shadow_slice.py \
  --phase verify \
  --checkpoint /tmp/mainrag-storage-v2-shadow.json \
  --output /tmp/mainrag-storage-v2-shadow-result.json
```

The preparer composes the frozen 12-document baseline with one neutral Rust
symbol fixture. This preserves the #55 corpus and query expectations while
making the real `card`, `explain`, `layers`, and `ownership` API/CLI gates
executable. `prepare` refuses to overwrite an existing directory; `verify`
detects drift, `delta` makes the one supported deterministic source change,
and `reset` restores its exact base bytes. The guarded `base -> delta -> reset`
sequence exercises temporal A -> B -> A membership: the return to A must create
a new generation while reusing immutable bodies, nodes, views, and analysis.

The ingest response embeds the existing `ShadowIngestMeasurements` phase keys
(`lesen_hashen_ms`, `content_store_ms`, `strukturprojektion_ms`, `analyse_ms`,
`db_staging_ms`, `intervall_delta_ms`, and `sealing_ms`), so a telemetry
collector can compare the full read/dedup/write pipeline across repeated and
delta runs. The harness never stores the token or the local fixture path.

Warm p50/p95/p99 may differ by at most 50% between the two local subprocess
runs. This deliberately broad tolerance detects large regressions without
pretending that scheduler and process-start noise are deterministic. Result
identity, quality, and work counts have no tolerance and must match exactly.
