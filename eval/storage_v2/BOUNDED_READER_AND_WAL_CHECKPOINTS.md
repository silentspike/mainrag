# Bounded reader planning and durable WAL checkpoints

Migrations 125 and 126 support issues #66 and #67. Existing generation identity,
source cuts, native matches, rank and authorization remain unchanged.

## Reader work

Broad occurrence scopes use keyed compact-presence probes instead of embedding
large constant arrays in a custom PostgreSQL plan. Source authorization is
resolved once. An empty native match skips expansion of the requested IDs.
Migration replay rejects unexpected function bodies, settings, ownership and
execution grants. The native PostgreSQL suite compares complete result envelopes
and executor work against independent references, including 130,908 requested IDs.

## Optional build backpressure

By default checkpoint behavior is unchanged. Operators can explicitly configure
`MAINRAG_V2_LOCAL_WAL_HIGH_BYTES` and `MAINRAG_V2_LOCAL_WAL_LOW_BYTES`, with
`0 < low < high`. `MAINRAG_V2_LOCAL_WAL_MAX_WAIT_SECONDS` defaults to 7,200 and
must be between 1 and 86,400. Partial or invalid configuration rejects a new build.
The administrator must install migration 126 before enabling this configuration.

The observer exposes only the byte count of local `.ready` WAL segments using
the configured segment size. The application receives no directory access,
filenames, archive configuration or archive control. No remote repository is
contacted. Existing scheduled backup and archival tasks remain independently owned.

After COMMIT, the build publishes its durable item count before checking the
backlog. At the high threshold it reports `waiting_local_wal_budget`, holds no
transaction or row locks and polls every five seconds until the low threshold.
The source session lock and shared pack-maintenance fence remain held. The next
transaction rechecks source identity, pointer and authorization before writing.
Observation failures and the finite wait limit stop safely at the retained
checkpoint; they do not trigger an automatic restart or a replacement generation.
An interrupted source resumes with its persisted item count visible during the
content reuse prepass. Process counters alone never replace the committed ledger.

These thresholds supplement independent physical-capacity admission and the hard
WAL limit. A checkpoint can itself generate additional WAL, so leave enough space
between the high threshold and that limit. They do not establish backup or restore
acceptance, waive integrity checks or guarantee a source latency threshold.

Rollback disables the opt-in configuration and restores the previous executable
and the retained reader definitions. Neither migration rewrites stored source data
or activates a generation. The observer must not be removed while an opted-in
writer still uses it.

## Validation

The SQL tests check complete result equality, RLS, keyed work, administrator-only
installation, restricted observation authority and rejection of definition drift.
Rust unit tests cover threshold validation, hysteresis, timeout and observation
errors. The isolated native producer integration test proves committed visibility,
no open transaction during a wait, exclusive source ownership and rejection of a
source changed during that wait, followed by the existing interruption/resume proof.
