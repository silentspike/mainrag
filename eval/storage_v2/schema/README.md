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
  eval.storage_v2.schema.test_scoped_posting_probes \
  eval.storage_v2.schema.test_query_coverage_evidence \
  eval.storage_v2.schema.test_structural_card_reuse \
  eval.storage_v2.schema.test_structural_card_reuse_benchmark \
  eval.storage_v2.schema.test_linear_hash_parts \
  eval.storage_v2.schema.test_linear_hash_parts_benchmark \
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

Migration 045's suite adds 5,000 unrelated documents containing the same common
term and verifies that neither results nor scope normalization change. The
installed complete query must use one shared physical posting lookup, the full
document/digest primary key, and no global posting term index. It retains the
late Top-K content boundary and checks authoritative text equality outside the
index lookup. Its 36-case differential gate includes positive/negative/repeated
terms, phrases, exact values, long tokens and filters, plus migration replay,
drift rejection and the complete inherited shadow-ingest suite. The historical
044 structural suite pins its own version; all other fresh schema consumers
exercise the latest migration sequence.

Migration 046's read-only coverage suite binds returned occurrence and legacy
chunk identities to a verified source/generation/commit, recomputes literal
support from body-digest-checked text, and rejects inconsistent projections or
postings. It checks replay, unchanged generation/pointer/evidence state, source
and administrator authority, input bounds, and locale-aware token boundaries.
The operator suite additionally rejects proof tampering and baseline path
displacement/reordering, and verifies that the complete proof is bound into
dual-read and qualification evidence. These literal-query checks do not replace
the broader search benchmark.

Migration 047 reuses only complete, authorized structural-card bundles whose
full fields and digests agree. Its suite rejects speculative writes on the
reuse path, compares the previous error contract for invalid and colliding
inputs, checks incomplete-analysis recovery, and races actual insert conflicts.
It extracts the installed lookup for a generic-plan check on 1,000 cards;
the existing symbol, occurrence, analysis, and card indexes must each return
one row without filtering a broader partition. The inherited full shadow gate
also runs against the new migration.

After committing the implementation, compare both cold and repeated card work:

```bash
python3 -m eval.storage_v2.schema.benchmark_structural_card_reuse \
  --repetitions 3 --calls 500 --output /tmp/storage-v2-card-comparison.json
```

This uses an owned disposable database, alternates the previous/new functions,
checks actual nonzero function calls and unchanged semantic rows, and requires
zero WAL records for complete reuse. Cold writes use identical rolled-back
inputs; the warm fixture is first created by the previous function. Optional
`TM_KENNZAHLEN` output uses `card_reuse`, separate from pipeline or API timings.
It is a synthetic SQL comparison, not production latency or throughput proof.

Migration 048 retains the canonical hash byte format while aggregating large
part arrays without repeated copies of the growing root. The small-key path
remains unchanged. Differential tests compare the previous function and an
independent SHA-256 framing implementation across the crossover, binary/empty
parts, Unicode domains, array dimensions/lower bounds, null rejection, and
ordering/length separation. A 130,908-part synthetic generation root must match
the independent reference within the unchanged 30-second statement budget.
Migration replay must preserve ownership, ACLs, and immutable/strict function
attributes. No production source is accepted merely because this fixture passes.

After committing, run the bounded comparison on its own disposable server:

```bash
python3 -m eval.storage_v2.schema.benchmark_linear_hash_parts \
  --repetitions 3 --output /tmp/storage-v2-hash-comparison.json
```

The comparison alternates old/new functions for 64, 1,000, 10,000, and 130,908
parts. A completed timed query must match the independently computed digest.
Baseline statement timeouts remain explicit censored observations with null
timings; no zero, invented completion time, or speedup ratio is substituted.
Every new-function query must finish and leave the fixture unchanged. Existing
outputs, dirty implementation files, and external database sockets are rejected.
Optional `TM_KENNZAHLEN` metrics use the separate `hash_parts` namespace.

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
