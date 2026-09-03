# Storage-v2 database preparation gates

This directory implements issue #65's fail-closed preparation boundary. It
does not backfill a source, change an active generation, deploy an application,
or remove legacy PostgreSQL or Qdrant state.

## Read-only check

Run the check as a PostgreSQL inspection role that can read server settings,
activity, WAL inventory, and the data-directory filesystem:

```bash
python3 ops/storage-v2/preflight.py \
  --check \
  --local-postgres \
  --database mainrag \
  --backup-evidence "$OPERATOR_EVIDENCE_DIR/backup-evidence.json" \
  --output "$OPERATOR_EVIDENCE_DIR/storage-v2-preflight.json"
```

The output is deliberately redacted and validates against
`preflight.schema.json`. It contains versions, counts, hashes, timer state,
preload/configuration values, resource limits and totals, and
PASS/BLOCKED/FAIL states, but no database address,
hostname, account identity, data path, command line, or raw log. A missing
backup record, version/configuration drift, a stale collation usable by the
current database encoding, active or unknown
writer, active maintenance operation, insufficient free space, wrong extension,
or invalid selected index keeps the result `BLOCKED`.

The backup evidence input is a private operator artifact with this minimum
shape:

```json
{
  "schema_version": 1,
  "status": "PASS",
  "completed_at_unix": 0,
  "artifact_sha256": "64 lowercase hexadecimal characters",
  "restore_tested": false
}
```

`restore_tested: false` is reported as `backup-command-only`; it is never
relabeled as restore, PITR, HA, or disaster-recovery evidence.

The check exits 0 only for `PASS` and exits 3 for an honestly blocked state.
It performs no service, timer, database, package, index, or filesystem change.

## One approved apply gate

Live changes require a new preflight and a separately reviewed adapter for one
of these gates:

- `postgresql-minor-upgrade`
- `postgresql-configuration`
- `schema-extension-upgrade`
- `collation-refresh`
- `backend-index`

The selected backend is PostgreSQL's built-in GIN implementation. There is no
third-party backend package to install; the lock file's `none-built-in` package
format is enforced by the check.

An adapter is an operator-owned executable that performs exactly one reviewed
operation. The public coordinator refuses to invoke it unless all of the
following bind exactly:

- the checked manifest SHA-256;
- the adapter SHA-256;
- the gate name;
- the current live state SHA-256; and
- the literal approval string
  `APPLY:<gate>:<manifest-sha256>:<adapter-sha256>`.

Example invocation, only after explicit approval of those exact identities:

```bash
python3 ops/storage-v2/apply-gate.py \
  --apply collation-refresh \
  --checked-manifest "$OPERATOR_EVIDENCE_DIR/storage-v2-preflight.json" \
  --expected-manifest-sha256 MANIFEST_SHA256 \
  --adapter "$OPERATOR_EVIDENCE_DIR/reviewed-collation-adapter" \
  --expected-adapter-sha256 ADAPTER_SHA256 \
  --operator-approval APPLY:collation-refresh:MANIFEST_SHA256:ADAPTER_SHA256 \
  --backup-evidence "$OPERATOR_EVIDENCE_DIR/backup-evidence.json" \
  --output "$OPERATOR_EVIDENCE_DIR/collation-apply-evidence.json"
```

The coordinator rechecks the live state before execution, suppresses raw
adapter output, executes one adapter, immediately reruns the preflight, requires
the target check to become PASS, and rejects regression of any prior PASS. It
does not grant authority to run a gate: the owner/operator must explicitly name
the exact gate and candidate first.

## Required order

Run and accept at most one database gate at a time:

1. PostgreSQL minor version, if mismatched;
2. repository-owned PostgreSQL configuration, if drifted;
3. the locked schema prerequisite extension, if mismatched;
4. collation/index refresh, if stale;
5. built-in GIN index validation/build, if incomplete;
6. complete read-only preflight and trusted storage-v2 baseline.

Stop on drift or failure. Do not begin candidate construction (#66) until the
final manifest is PASS and its evidence boundary has been accepted. Search/read
availability during an adapter is determined by that reviewed adapter; the
preflight does not silently claim it.

Apply the storage-v2 migrations as the database runtime/table owner. The
controlled-write triggers deliberately require the table owner and the
`SECURITY DEFINER` functions to have the same identity. Applying migrations as
a superuser while leaving the new tables and functions owned by that superuser
will make the ordinary API runtime fail closed. Before candidate construction,
verify that the storage-v2 base tables, sequences, enum types, view, and all
`storage_v2_*` functions have the same owner as the existing `sources` table.
Do not work around an ownership mismatch by running the API with a privileged
database account.

## Source release candidates

Candidate construction is source-bounded and never changes an active pointer.
Run it only with an exact deployed commit and a protected source inventory:

```bash
mainrag source build-candidate SOURCE --commit-sha FULL_DEPLOYED_SHA
```

The response identifies a `verified` generation and includes phase telemetry.
Repeat the same command after a client or service restart to prove idempotent
resume: the generation identity must be reused and semantic row counts must not
increase.

The protected operator drives that sequence without accepting unchecked PASS
labels. Run `build` under `ops/telemetry/run.sh`, restart the exact API binary,
then run `verify` under telemetry as well:

```bash
python3 ops/storage-v2/release-candidate.py build \
  --source-id SOURCE_ID --commit-sha FULL_DEPLOYED_SHA \
  --checkpoint PROTECTED_CHECKPOINT

python3 ops/storage-v2/release-candidate.py verify \
  --source-id SOURCE_ID --commit-sha FULL_DEPLOYED_SHA \
  --checkpoint PROTECTED_CHECKPOINT --output PROTECTED_EVIDENCE
```

The server-side verification phase recomputes the generation root, decodes and
hashes every referenced body/pack entry, reconciles membership/search/analysis
counts and the active pointer, checks the public intelligence export contract,
and returns protected query seeds. The operator then executes supported current
and named-generation reads, the applicable intelligence commands, latency and
resource gates, records accepted dual-read evidence, and only then submits the
qualification envelope. Raw checkpoints, seeds, result sets, and evidence stay
outside Git with mode `0600`.

Filesystem discovery for this command returns metadata and file paths rather
than retaining the entire corpus. The builder reads files serially, rechecks
each content hash after the source watermark is captured, rediscovers and
rehashes the complete source immediately before sealing, and reports a
conservative source/pack buffer peak in phase telemetry. Changed, added, or
removed content aborts the candidate; it is never accepted as a mixed snapshot.
The production watermark also binds the registered source type/path and adapter
profile without publishing those protected values.

Current and explicitly named generation reads must then be compared through the
supported APIs. The evidence endpoint binds the source's recorded production or
explicit-test scope to the verified generation witness. Record the accepted,
fully classified dual-read envelope before qualification:

```bash
mainrag source dual-read SOURCE --evidence PROTECTED_DUAL_READ_JSON
mainrag source qualify-candidate SOURCE --evidence PROTECTED_QUALIFICATION_JSON
```

Qualification requires all of these checks to be `PASS`: artifact-root
reconstruction, authorization, body/pack integrity, dual-read classification,
intelligence, membership intervals, legacy-intelligence exportability,
resource budget, restart/resume, and search quality. The database independently
checks the sealed ingest identity, item and membership counts, complete analysis,
accepted dual-read evidence, unchanged active pointer, and the one-current-RC
invariant before transitioning `verified` to `release_candidate`.

Source names, IDs, paths, watermarks, queries, result sets, and raw resource
measurements are protected operational evidence. Public progress may contain
only source counts, type counts, aggregate sizes, hashes, outcomes, and opaque
evidence UUIDs. A candidate is not activation authority.
