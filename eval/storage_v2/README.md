# Storage-v2 public baseline harness

## Ingest evidence correction

The SQL fixture harness does not run production ingestion. Its v2 manifest
labels loading as `synthetic_sql_load`, reports logical input bytes and actual
table counts, and leaves parser/reuse/source-read measurements null with
`ingest.status=NOT_RUN`. Successful search tests may still be recorded, but the
aggregate remains `BLOCKED` (nonzero exit), not full baseline acceptance.
The committed v1 baseline is preserved as historical SQL-fixture evidence; it
does not validate under the corrected v2 acceptance schema. Do not rewrite its
old measurements or use them as a current production ingest baseline.

Storage-v2 runtime telemetry separately records `source_io.application_read_bytes`
and `ablauf.deferred_source_read_bytes` at actual deferred source reads, including
repeated verification loads. This counter excludes adapter reads, pack reads and
OS/device I/O. Filesystem adapter content probes, eager reads and fragment-boundary
reads are recorded separately in `source_io.adapter_read_bytes`.
`total_content_read_bytes` sums adapter and deferred loads when both are observed.
`content_read_coverage` can then be `COMPLETE`, while overall `coverage` remains
`PARTIAL`: filesystem metadata, walker configuration, pack reads and device I/O
are not included. Uninstrumented adapters remain null, never an invented zero.

The current source-sync API reports `stats.source_io` and request-local
`stats.work.chunker_calls` / `stats.work.intelligence_parser_calls`. These count
actual calls, including failed analysis attempts; concurrent runs do not share
counters. They are not counts of persisted rows, unique bodies, AST nodes or
successful analyses. Deferred legacy streaming reads and explicit-path sync I/O
remain partial/not measured. Initial and unchanged eager filesystem syncs both
include probe/content reads; a hash skip does not imply zero I/O.

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

## Run the SQL fixture microbenchmark

From the repository root:

```bash
python3 -m unittest eval/storage_v2/test_harness.py
python3 eval/storage_v2/check_writers.py
python3 eval/storage_v2/harness.py \
  --code-sha CODE_COMMIT \
  --harness-commit HARNESS_COMMIT \
  --output target/storage-v2/sql-fixture-v2.json
```

Replace both commit placeholders with the full reviewed subject/harness commits. The
default run performs three warmups and 30 measured iterations for every query.
Runs with fewer than 30 measured iterations fail closed.

The SQL/search subtests are successful only when:

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
- logical fixture bytes, loaded/repeated SQL row counts, SQL-load elapsed time,
  and post-load relation bytes (production ingest measurements remain null); and
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

The dependency-free [integrity presentation policy](../../docs/telemetry-integrity.js)
supports private viewers without publishing their HTML, labels, or measurements.
`candidate_state` metrics are pinned point-in-time measurements: zero binding
errors remain visible, missing observations remain missing, and distinct counts
are displayed without compact abbreviations. `query_client_ms` includes
connection startup and the complete source-state SQL query; it is not search
API latency or a candidate qualification result. Viewers must honor `showZero`
and `pinned` before applying zero/counter/change filters, and use
`formatExactCount` for `n_exact` units. Failed runs remain excluded by the
collector/aggregator, not converted into zero-valued successful measurements.

Run the public presentation regressions with
`node --test eval/storage_v2/telemetry_viewer.test.cjs`. The required docs check
also runs them without private assets or production access.

Candidate verification evidence includes `query_seed_summary`: case counts,
exact distinct query-text counts, repeated-query cases, and positive/negative
counts. Different expected paths for the same query are not independent queries.
This diagnostic does not remove cases, change seed selection, waive failed
quality/latency checks, or establish representative gold coverage. It survives
partial verification failures after the server returns its seed set.

Each search gate also emits count-only `diagnostics`. These distinguish expected
paths absent from either top-k, displaced baseline paths, changed common-path
order, and multiple hits from one path. A top-k omission does not prove absence
from the corpus or establish a ranking cause. These observations do not modify
any acceptance decision. `repeated_result_diagnostics` is for repeated reads of
the **same** engine and request: it compares every hit field and the total,
classifying only exact identity or complete equal-score hit permutations.
It rejects duplicate identities, non-finite scores, and malformed identities;
all other differences remain unclassified. Timing is excluded from identity,
but latency checks are unchanged. The helper is not a cross-engine equivalence
test and does not turn a quality failure into a pass.

The legacy keyword query now uses chunk ID ascending after score descending in
all four tenant/source branches. This stabilizes ties, including membership at
the result limit; it does not change the relevance score or access predicates.
`python3 -m unittest eval/storage_v2/schema/test_keyword_tie_order.py` executes
the actual Rust SQL templates on a disposable PostgreSQL fixture, including
term/phrase cases, both insertion orders, ties crossing the limit, unequal
scores, and tenant/source filtering. Changing the baseline tie policy requires
fresh runtime-bound comparison evidence after deployment; earlier evidence is
retained, not relabeled as a measurement of this implementation.

The same public presentation policy pins `search_quality` diagnostic metrics,
including `quality_passed = 0` (FAIL). HTTP client and server clocks have separate
millisecond labels; repeated-pair medians are not maximum-latency gates. A
successful telemetry collection does not mean search quality passed. Distinct
top-10 path counts describe result composition, not relevance improvement.
Viewers must honor the metric's `preference`: `higher` for the quality-pass
flag, `lower` for latency, and `neutral` for suite/result counts. Neutral metrics
must not receive best/worst colors in cells, trends, or percentage deltas.

Warm p50/p95/p99 may differ by at most 50% between the two local subprocess
runs. This deliberately broad tolerance detects large regressions without
pretending that scheduler and process-start noise are deterministic. Result
identity, quality, and work counts have no tolerance and must match exactly.
