# Storage v2 shadow-ingest telemetry

The storage-v2 shadow writer emits one `kennzahlen.json` file per measured
run. Its shape is directly consumable by `ops/telemetry/run.sh` and
`ops/telemetry/summarize.sh`:

- `phase.lesen_hashen_ms`
- `phase.content_store_ms`
- `phase.strukturprojektion_ms`
- `phase.analyse_ms`
- `phase.db_staging_ms`
- `phase.intervall_delta_ms`
- `phase.sealing_ms`
- `ablauf.latenz_ms`
- `ablauf.eingang_bytes`, `unique_bytes`, and `stored_bytes`
- body, node, view, analysis, and complete-generation reuse counts
- parser passes, analysis retries, created artifacts and occurrences, opened and closed intervals
- errors, configured and managed peak buffer bytes, and writer concurrency

The command under measurement must write this file to the path supplied in
`TM_KENNZAHLEN`. The supported `shadow_slice.py --phase ingest` command now
validates the telemetry object returned by the API and writes it atomically to
that path. Missing phases, missing optimization counters, or invalid values
fail the measured command instead of producing an apparently valid run. A
comparison series uses at least three runs per state:

```bash
commit=$(git rev-parse HEAD)
api_pid=$(pgrep -n -f '^target/debug/mainrag-api$')
pg_pid=$(head -1 "$DISPOSABLE_PGDATA/postmaster.pid")
pg_shared_buffers=$(psql -h "$DISPOSABLE_PGSOCKET" -d "$DISPOSABLE_PGDB" -tAc 'SHOW shared_buffers')
for run in storage-v2-noop storage-v2-noop-2 storage-v2-noop-3; do
  sudo MAINRAG_TOKEN="$MAINRAG_TOKEN" \
    TM_ONLY_PIDS=1 \
    TM_PIDS="shadow-api=$api_pid,shadow-postgres=$pg_pid" \
    TM_API_BINARY="$PWD/target/debug/mainrag-api" \
    TM_PG_SHARED_BUFFERS="$pg_shared_buffers" \
    TM_WATCH_INTERVAL="disabled-for-isolated-run" \
    TM_DESC="storage-v2 unchanged shadow ingest at $commit" \
    ops/telemetry/run.sh "$run" -- \
    python3 eval/storage_v2/shadow_slice.py \
      --phase ingest \
      --commit-sha "$commit" \
      --checkpoint "/tmp/$run-checkpoint.json"
done
ops/telemetry/summarize.sh
```

`TM_ONLY_PIDS=1` is mandatory for isolated development runs. Without it the
collector also observes installed systemd services and the resource series no
longer describes the API and PostgreSQL processes that handled the fixture.
`run.sh` rejects missing/stale explicit PIDs, records the exact test binary and
database buffer setting, propagates a failing harness status, and excludes a
failed command or collector from `summary.json`.

Explicit PID measurements aggregate the complete process tree. This is
required for PostgreSQL because query work runs in backend child processes,
not in the postmaster alone. The dashboard therefore exposes process-tree CPU
time, logical and physical I/O, RSS, and proportional-set memory alongside the
inner pipeline phases. Comparing only the root PID is not accepted as resource
evidence for an isolated run.

Cold, unchanged/no-op, one-delta, and return-to-prior-content states are
separate workload families. Never compare them as repetitions of one state or
interpret a cross-family difference as a tuning effect. A cold series needs an
independently reset disposable database for every repetition; otherwise global
body and analysis reuse silently turns the second run into a warm/deduplicated
measurement. Prepare each source root with `prepare_shadow_fixture.py prepare`,
retain that same root for the no-op series, use its guarded `delta` action for
the one-delta series, and use `reset` for the A -> B -> A return series. The
preparer manifest is deliberately extensionless, so the real filesystem
adapter does not ingest measurement metadata as source content.

An A -> B -> A return is not a semantic no-op. It must allocate a third
generation, close and open exactly one membership interval, create no new
artifact or occurrence, perform no parser pass, and reuse all 13 bodies, nodes,
views, and analyses. Only the immediately following unchanged ingest may reuse
that complete generation with zero interval work.

For tuning, hold the workload family, fixture identity, database reset/warm-up,
binary, PostgreSQL settings, and concurrency constant. Give each candidate
configuration its own three-run group, for example `storage-v2-delta-default`,
`storage-v2-delta-default-2`, `storage-v2-delta-default-3`, followed by the same
names under `storage-v2-delta-candidate`. Compare the two complete groups in the
HTML viewer. A dirty-tree or one-run group is diagnostic evidence only. It may
find correctness bugs, but it cannot establish optimal latency or resource
settings.

`MAINRAG_STORAGE_V2_PACK_IO_BUFFER_BYTES` is the first supported writer tuning
dimension. It accepts 4096 through 1048576 bytes and defaults to 65536. The
configured value is emitted as `ablauf.io_buffer_bytes`; the largest buffer
actually used during the run remains separately visible as
`ablauf.peak_buffer_bytes`. Change only one tuning dimension per comparison
series.

Accepted CPU, I/O, and proportional-memory comparisons require root collection
so `/proc/<pid>/io` and `smaps_rollup` are available for the complete API and
PostgreSQL process trees. Non-root measurements remain useful for inner phase
and dedup correctness, but are not sufficient for resource optimization.

Use only a disposable PostgreSQL instance and a synthetic/public fixture for
this gate. A benchmark result is valid only when the generation remains
non-active, seals with the expected item/root counts, reconstructs exactly,
and reports zero errors. Kernel/cgroup samples remain in `samples.jsonl`; the
stage and dedup counters remain in `kennzahlen.json`, so system cost and inner
pipeline work can be compared without conflating them.
