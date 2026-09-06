# Intelligence retry regression

Issue #51 separates intelligence completion from non-streaming chunk skips.
The existing migration-028 ledger is reused; no migration is introduced.

`api/src/services/index/intelligence_retry_tests.rs` invokes the production
`process_raw_file` method with the real parser and real PostgreSQL writes.
Only the chunker is controlled, to force a matching-version or empty result.
The schema fixture is intentionally minimal, not a substitute for migration,
RLS, streaming, deployment, or production qualification tests.

The opt-in test requires `MAINRAG_INDEX_TEST_DATABASE_URL` and refuses every
database name except `mainrag_index_fixture`. It creates a UUID-named schema
with no public-schema fallback. Normal completion, returned errors, and the
60-second operation timeout drop that schema; the ephemeral CI service owns
panic/process-termination cleanup. Never point it at a persistent shared DB.

CI explicitly runs the exact ignored test and requires one pass, zero failures,
and zero ignored tests. An ordinary aggregate run only compiles this test and
reports it ignored; that is not real-database execution evidence.

Covered transitions:

- Pending unchanged file -> injected symbol INSERT failure -> pending -> retry
  success -> unchanged success does not rewrite symbols or completion time.
- Analyzed changed file -> matching chunk versions -> injected call INSERT
  failure -> partial symbols with pending completion -> same-content retry
  replaces partial state without duplicate calls. Both normal and >5 MiB
  metadata-only file writes are exercised.
- Supported comment-only file -> successful zero-symbol analysis -> no repeat.
- New nonempty file with no chunks -> intelligence still completes.
- All skip paths preserve the original chunk ID and keep the outbox empty.
  A bound, unserved loopback endpoint detects accidental vector connections.

The unfixed baseline returns before the second hash-skip attempt can mark
completion, and fails the first successful-recovery assertion. This is the
expected negative-control failure, not a claim of an executed baseline run.

Analysis failures remain warnings, and partial intelligence writes are not
atomic. The next indexing attempt is the recovery point. Concurrent file
writers, stale historical completion markers, and other ingestion paths are
not repaired by this change. No latency or resource-utilization improvement
is claimed by this correctness regression.
