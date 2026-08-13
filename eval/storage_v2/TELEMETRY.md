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
- body, node, view, and analysis reuse counts
- parser passes, created artifacts and occurrences, opened and closed intervals
- errors, managed peak buffer bytes, and writer concurrency

The command under measurement must write this file to the path supplied in
`TM_KENNZAHLEN`. A comparison series uses at least three runs per state:

```bash
for run in storage-v2-before storage-v2-before-2 storage-v2-before-3; do
  sudo TM_DESC="storage-v2 synthetic shadow ingest before change" \
    ops/telemetry/run.sh "$run" -- <shadow-ingest-command>
done
ops/telemetry/summarize.sh
```

Use only a disposable PostgreSQL instance and a synthetic/public fixture for
this gate. A benchmark result is valid only when the generation remains
non-active, seals with the expected item/root counts, reconstructs exactly,
and reports zero errors. Kernel/cgroup samples remain in `samples.jsonl`; the
stage and dedup counters remain in `kennzahlen.json`, so system cost and inner
pipeline work can be compared without conflating them.
