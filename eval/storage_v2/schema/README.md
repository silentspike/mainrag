# Storage-v2 generation schema checks

This suite proves the additive generation schema against a disposable
PostgreSQL cluster. It covers transactional bootstrap, idempotent migration
application, source-local generation allocation, immutable artifacts,
half-open membership intervals, atomic activation and requalification, direct
mutation rejection, cross-source rejection, and RLS isolation.

Run against the locally installed PostgreSQL binaries:

```bash
python3 -m unittest \
  eval.storage_v2.schema.test_generation_schema \
  eval.storage_v2.schema.test_content_schema \
  eval.storage_v2.schema.test_content_graph_schema \
  eval.storage_v2.schema.test_search_document_reuse \
  eval.storage_v2.schema.test_search_document_reuse_benchmark \
  eval.storage_v2.schema.test_search_materialization \
  eval.storage_v2.test_release_candidate_operator
```

The shadow-ingest suite also exercises the additive storage-v2 intelligence and
exact-retrieval schemas, named-generation commands, composed-view Boolean and
phrase behavior, authorization isolation, stable hit mappings, redacted export,
and clean-source import.

The search-document reuse suite includes the complete shadow-ingest suite and
adds both body/node collision checks and query-plan regressions against 10,000
synthetic documents. It executes the installed function's initial and
insert-conflict lookups using generic prepared plans and requires the complete
component key in the existing unique index condition.

It also races two real backends on each component kind. The winning insertion
is held uncommitted until the losing insertion waits on its transaction, then
the winner commits. Identical content must return the same document ID; changed
text or identifiers must fail with a profile collision. Each race must leave
exactly one document and its original postings. Client processes, synchronization
relations, and databases are fixture-owned and cleaned up on success or failure.

The late-materialization suite compares complete result JSON before/after
migration 044 for 32 combinations of Boolean, phrase, exact, long-token, repeated
term, and source-filter queries. Counts, scores, ordering, full result content,
function ownership and ACLs must remain identical. It also reruns the inherited
authorization/oversized-document suite, tests migration replay and drift
rejection, and executes the installed query under a generic prepared plan.
The plan must assemble only the returned three texts while evaluating all 24
fixture views. Three installed posting predicates must use both primary-key
columns and retain authoritative term equality. No latency threshold is relaxed.

## Reproducible SQL reuse comparison

After committing the benchmark and migration changes, run:

```bash
python3 -m eval.storage_v2.schema.benchmark_search_document_reuse \
  --repetitions 3 --calls 500 --output /tmp/storage-v2-sql-reuse-comparison.json
```

The benchmark refuses dirty implementation files and existing output files. It
creates the same disposable 10,000-document corpus, checks all four query plans,
alternates migrations 040/043 for three rounds, and verifies unchanged SHA-256
row-set identities. Every measured query must actually call the materializer
the declared number of times. JSON evidence names the existing implementation
commit, migration/script hashes, PostgreSQL version, repeated SQL timings, and
buffer counts. There is no timing threshold in the regression gate.

If `TM_KENNZAHLEN` is supplied, numeric comparison metrics are additionally
written under `sql_reuse`, keeping them distinct from full pipeline phases in
the telemetry viewer. These are warm-cache SQL-only measurements, not an
isolated resource comparison or proof of a whole-production-ingest speedup.
The output file remains with the caller; the benchmark removes its disposable
database and server before returning. Unlike the schema checks, this benchmark
rejects `STORAGE_V2_TEST_SOCKET`: comparisons always own a separate disposable
server and never change functions in an externally supplied database cluster.

For the release gate, point the suite at a separately started disposable
PostgreSQL 18.4 server over a private Unix socket:

```bash
STORAGE_V2_TEST_SOCKET=/path/to/socket \
  python3 -m unittest eval/storage_v2/schema/test_generation_schema.py
```

The suite creates uniquely named databases and removes them on exit. Fixtures
contain only synthetic identifiers and content.
