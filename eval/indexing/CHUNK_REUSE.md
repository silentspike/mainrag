# Chunk probe reuse

Issue #48 removes the second chunker call when a non-streaming, existing,
changed file fails the chunk/version skip check. The original vector is moved
into write preparation, including an empty vector. New files and append-only
deltas still invoke the chunker once. Whole-file and version skips, streaming,
chunk limits, intelligence extraction, and persistence are unchanged.

## Correctness

`services::index::chunk_reuse_tests` checks invocation counts with a counting
chunker, vector and string allocation identity, empty-result reuse, exact
new-file/delta arguments, and complete ordered semantic-chunker output on public
synthetic Rust, prose, and conversation fixtures. Every chunk field participates
in the comparison, including byte/line ranges, metadata, parents, and context.
These tests exercise the shared write-preparation function; they are not a
database integration test. The surrounding pipeline wiring is reviewed with the
change, and existing API regressions remain required.

## Repeated measurement

Run the opt-in library test (using the locally required execution environment):

```sh
cargo test -p mainrag-api --features storage-v2-retrieval --lib \
  services::index::chunk_reuse_tests::benchmark_chunk_probe_reuse \
  -- --ignored --exact --nocapture
```

Require one executed test and 90 `CHUNK_REUSE_SAMPLE` JSON records: three public
fixtures, three groups, five paired repetitions, and two variants. Preserve the
whole log, including failures. Each pair must have matching fixture identity,
complete ordered result identity, and chunk count.

The clock covers chunk preparation and the content-hash work used by version
comparison. `double` chunks the probe, hashes its content, drops it, and chunks
again; `reuse` chunks and hashes the probe, then consumes it through the same
write-preparation function used in production. Variant order alternates; each
fixture has an untimed warmup. Complete-result serialization and equality checks
are outside the clock. Invocation fields describe these two algorithmic paths;
the separate counting tests verify actual invocation counts. Record the build
profile: the command above uses the unoptimized test profile, not release.

Compare group medians and retain all within-group observations. Do not treat
group repetitions within one process as independent host experiments. No SQL,
embedding call, outbox work, production service, or resource collector is part
of this microbenchmark. It cannot establish end-to-end ingest latency, lower
peak RSS, or production qualification. The probe stays live across the existing
cleanup transaction until the write-preparation step; there is no second vector
or text clone.
