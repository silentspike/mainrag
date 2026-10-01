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

Create a fresh protected read-only backup metadata observation before the
preflight:

```bash
python3 ops/storage-v2/backup-observe.py \
  --stanza mainrag \
  --info-output "$OPERATOR_EVIDENCE_DIR/pgbackrest-info.json" \
  --evidence-output "$OPERATOR_EVIDENCE_DIR/backup-evidence.json"
```

The evidence and raw pgBackRest information are create-only private sibling
files. The preflight checks their digest, latest completed nonerror backup,
stanza identity, observation age, and backup age. Evidence uses this shape:

```json
{
  "schema_version": 2,
  "status": "PASS",
  "stanza": "mainrag",
  "completed_at_unix": 0,
  "observed_at_unix": 0,
  "artifact_file": "pgbackrest-info.json",
  "artifact_sha256": "64 lowercase hexadecimal characters",
  "backup_type": "full",
  "backup_label_sha256": "64 lowercase hexadecimal characters",
  "restore_tested": false
}
```

This readback is reported as `backup-metadata-only`. It does not prove that the
backup can be restored and is never relabeled as restore, PITR, HA, or disaster
recovery evidence. A separate exercised restore is required for that claim.

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

## Requalifying an existing candidate

Keep the candidate's original producer commit, checkpoint and frozen gold suite.
When a previous normal qualification completed successfully for the same
generation, `release-candidate.py verify` can reuse its restart/replay proof with
`--completed-restart-evidence` and
`--expected-completed-restart-evidence-sha256`. The receipt must be an owned,
private regular file with the reviewed digest. Producer identity, input hashes,
profiles, verification manifest, counts and active pointer must still match.
Interrupted attempts and failed qualifications cannot supply this proof.

This avoids the repeated build endpoint and source traversal. Body/segment
integrity, live input review, frozen gold, search quality/latency, intelligence,
resources and the final qualification POST still execute. Original telemetry is
not emitted as a new replay measurement.

Use `--reader-package-receipt` and
`--expected-reader-package-receipt-sha256` to bind the new reader checks to the
local production installation. This mode checks the actual running service
executable against the accepted installation digest, retains the producer
commit separately, and rejects a package or API instance change during the run.
It applies only to the local production API. All receipts and checkpoints stay
in protected operator storage.

For a current-package aggregate audit, supply both
`--expected-reader-commit-sha` and `--expected-reader-binary-sha256` to
`candidate-aggregate-audit.py`. Every candidate must then contain matching reader
evidence. A historical qualification alone fails this check. The audit still
reports external gates separately; this package binding does not prove source
freshness, production activation, backup recovery or cleanup.

## Source-local final deltas

The authenticated pointer-neutral release-watermark endpoint scans the same adapter
profile and source bytes used by candidate construction. Managed append sources
receive a full manifest and segment comparison. It returns a source watermark,
item count, input byte count, and application read bytes when the adapter measures
them (`null` otherwise), without allocating a generation or changing a pointer.
The Git adapter may advance its local repository cache during observation; it
rejects a dirty cache, a mismatched origin, or non-fast-forward history rather
than using stale checked-out content. Git transport and cache I/O are not
measured, so its application read count remains `null`. Transport uses native
Git with a ten-minute deadline, no automatic tag download for existing caches,
and process-group cleanup on timeout or cancellation. Repository checks and
safe fast-forward checkout run off the async runtime. A failed fetch leaves the
checked-out commit unchanged; it is never accepted as a stale-source fallback. The default PDF adapter
reads a bounded in-memory snapshot and reports the actual PDF bytes read; the
optional MuPDF backend still reports `null`.
`final-delta.py plan` compares
those live values against a complete protected candidate audit. It retains Gs
only when its source watermark and adapter profile are unchanged; otherwise it
names that source for a Gs+1 rebuild. The plan refuses missing candidates,
pointer drift, and a changed registered source set.
Both phases require a fresh passing protected preflight bound to the exact
operator commit and schema hash. A blocked backup, writer, maintenance or
capacity check stops the procedure before any candidate build.

```bash
python3 ops/storage-v2/final-delta.py plan \
  --database mainrag --local-postgres \
  --baseline-audit PROTECTED_AUDIT --baseline-audit-sha256 EXACT_AUDIT_SHA256 \
  --preflight FRESH_PROTECTED_PREFLIGHT --preflight-sha256 EXACT_PREFLIGHT_SHA256 \
  --operator-commit-sha EXACT_OPERATOR_COMMIT --schema-sha256 EXACT_SCHEMA_SHA256 \
  --installed-binary-sha256 EXACT_INSTALLED_BINARY_SHA256 \
  --runtime-commit-sha EXACT_INSTALLED_RUNTIME_COMMIT \
  --output PROTECTED_FINAL_DELTA_PLAN
```

For each `REBUILD` entry, use the existing protected `release-candidate.py`
`build` and `verify` phases, including the pack reserve, API restart/resume,
reviewed generation-bound gold suite, exact comparison, intelligence, and
qualification gates. The controlled qualification function replaces the old
pointer-neutral candidate only after the new candidate passes. Do not reuse a
gold suite bound to Gs for Gs+1. Retained candidates are not rebuilt. Store
the changed-source qualification artifacts in a private
`mainrag.storage-v2.final-delta-receipts.v1` file with a `sources` array of
`source_id`, `artifact_path`, and `artifact_sha256` entries. Unchanged sources
have no receipt entry.

```bash
python3 ops/storage-v2/final-delta.py finalize \
  --database mainrag --local-postgres \
  --baseline-audit PROTECTED_AUDIT --baseline-audit-sha256 EXACT_AUDIT_SHA256 \
  --preflight FRESH_PROTECTED_PREFLIGHT --preflight-sha256 EXACT_PREFLIGHT_SHA256 \
  --operator-commit-sha EXACT_OPERATOR_COMMIT --schema-sha256 EXACT_SCHEMA_SHA256 \
  --installed-binary-sha256 EXACT_INSTALLED_BINARY_SHA256 \
  --plan PROTECTED_FINAL_DELTA_PLAN --plan-sha256 EXACT_PLAN_SHA256 \
  --receipts PROTECTED_FINAL_DELTA_RECEIPTS \
  --receipts-sha256 EXACT_RECEIPTS_SHA256 \
  --output PROTECTED_FINAL_CANDIDATE_COMMIT_MAP
```

Finalization re-observes every source, requires the planned watermark to remain
current, checks unchanged Gs identity, checks each rebuilt candidate is exactly
Gs+1 with matching qualification artifact and current runtime commit, and
requires one pointer-neutral release candidate per source. It emits a private
per-source commit map. Capture a fresh candidate inventory with
`candidate-inventory.py --candidate-commit-map` and its exact SHA-256, then
run the aggregate audit and external quality, benchmark, resource, writer,
backup, recovery, and legacy-state gates on the final set. The map and audit
are observed state, not aggregate acceptance or activation authority.

## Atomic candidate-set activation boundary

Migration 057 adds `storage_v2_activate_candidate_set` and a compact activation
receipt table. Installing the migration does not call the function or change a
pointer. Migration 064 requires each source entry to name its own exact
`candidate_commit_sha`, allowing retained Gs and rebuilt Gs+1 to keep their
immutable qualification identities. The top-level `code_commit_sha` still
identifies the reviewed installed runtime. Its protected JSON manifest uses schema
`mainrag.storage-v2.activation-set.v1`, a unique `activation_id`, exact
`code_commit_sha`, `schema_sha256`, `backend_package_sha256`,
`aggregate_evidence_sha256`, and a `sources` array covering **every** registered
source, including the benchmark source. Each source entry binds `source_id`,
`candidate_generation_id`, `expected_active_generation_id` (or JSON null),
`evidence_id`, `evidence_manifest_sha256`, `candidate_commit_sha`, and
`source_watermark_sha256`. The expected manifest SHA-256 is calculated over
PostgreSQL `jsonb::text`, and a fresh approval must name that exact digest.

One function call locks the source registry, pointers, generations, and
qualification evidence against concurrent writes; rejects incomplete sets,
duplicate sources, stale pointers, non-candidates, and mismatched evidence; then
calls the controlled per-source activation function in one statement. An error
rolls the entire statement back. The returned pointer-set digest is a statement
result, not a COMMIT or application-switch proof. The caller must explicitly
COMMIT, read back every pointer/status and the receipt, and complete the coupled
default-read switch and post-activation ingest gate. The function cannot observe
live adapter watermarks, writers, actual package installation, representative
quality, resource headroom, or the application default selector. Those gates and
fresh owner approval must be proven before any live call.

The protected `activation-set.py` operator implements that database transaction
boundary once #66's complete persisted candidate set and separately accepted
external gates exist. `plan` requires exact SHA-256 values for a private
candidate-set audit, an aggregate-acceptance artifact, and a fresh PASS
preflight. The acceptance artifact has schema
`mainrag.storage-v2.aggregate-acceptance.v1` and binds the audit digest,
candidate-set digest, runtime code commit, preflight operator commit, live
schema digest, backend and installed-binary digests, a complete source count,
fresh per-source adapter watermarks, and all eight named external gates. A
`PASS` label in this artifact is an operator-reviewed assertion; the planner
does not create or infer missing gold, quality, recovery, writer, benchmark,
or legacy-state evidence. Volatile receipts and per-source watermarks must be
no more than five minutes old. The installed binary is hashed again locally.
The plan and apply phases also call the authenticated release-watermark API
for every source and reject stale adapter profiles, watermarks, or item counts.

```bash
python3 ops/storage-v2/activation-set.py plan \
  --database mainrag --local-postgres \
  --audit PROTECTED_AUDIT --audit-sha256 EXACT_AUDIT_SHA256 \
  --acceptance PROTECTED_ACCEPTANCE --acceptance-sha256 EXACT_ACCEPTANCE_SHA256 \
  --preflight PROTECTED_PREFLIGHT --preflight-sha256 EXACT_PREFLIGHT_SHA256 \
  --installed-binary /opt/mainrag/api/mainrag-api \
  --output PROTECTED_ACTIVATION_PLAN
```

Planning re-reads all registered sources, release candidates, evidence and
expected active pointers without writing. It asks PostgreSQL for the exact
`jsonb::text` activation-manifest digest. The protected plan is create-only,
mode 0600, and reports `READY_FOR_EXPLICIT_APPROVAL`; it is not an activation.
The plan also binds the exact service, installed binary path, drop-in,
environment file, and five-minute switch window for the coupled default gate.
An owner approval must bind the plan, activation manifest, runtime code,
schema, backend package, candidate set and old pointer-set hashes after all
final gates are reviewed. The separate protected approval document has schema
`mainrag.storage-v2.activation-approval.v1`, status `APPROVED`, those seven
exact digests and `approved_at_unix`. A protected admin-context document has
schema `mainrag.storage-v2.admin-context.v1` and `admin_user_id`. Neither
document is generated from a passing test or plan by the operator.

`apply` requires those exact files and their SHA-256 values, a fresh PASS
preflight, and the installed binary. It rechecks the live candidate and pointer
set and the PostgreSQL manifest digest before writing an attempt artifact. It
calls only `storage_v2_activate_candidate_set` inside one explicit transaction;
a database error leaves the connection to roll back. A separate connection
immediately checks the committed receipt, complete pointer set, sole active
generation for each source, and superseded previous active generations. A
lost response is `COMMIT_OUTCOME_UNKNOWN` and must be reconciled before retry.
Successful output is `DB_COMMITTED_DEFAULT_SWITCH_PENDING`, not #67 acceptance.
The same approved procedure must immediately run `default-read-switch.py`.
It requires the exact protected plan, approval, and committed attempt digests,
rechecks the committed database receipt, installed API binary, and executable
of the running API process, and rejects
an attempt more than five minutes old. It refuses an existing selector or
drop-in. As root, it creates a final `EnvironmentFile` drop-in, restarts the
API, reads the new process environment, checks the API's default-read-path
endpoint, and rechecks the database receipt. It reports
`DEFAULT_SWITCHED_POST_INGEST_PENDING` only after both paths agree. If the
service outcome cannot be verified, its private result says
`SWITCH_OUTCOME_UNKNOWN`; reconcile the live service and database before any
retry. An exact existing selector can be read back again, or the same approved
restart can be retried within the five-minute window; a differing or partial
selector requires manual reconciliation. The switch has no automatic rollback. The first
ordinary ingest and full search/intelligence/benchmark gate still follow
before #67 can be accepted. Do not invoke `apply` without this complete
approved procedure and a fresh explicit owner instruction for the exact plan.

```bash
python3 ops/storage-v2/activation-set.py apply \
  --database mainrag --local-postgres \
  --plan PROTECTED_PLAN --plan-sha256 EXACT_PLAN_SHA256 \
  --approval PROTECTED_OWNER_APPROVAL --approval-sha256 EXACT_APPROVAL_SHA256 \
  --admin-context PROTECTED_ADMIN_CONTEXT \
  --admin-context-sha256 EXACT_ADMIN_CONTEXT_SHA256 \
  --audit PROTECTED_AUDIT --acceptance PROTECTED_ACCEPTANCE \
  --preflight FRESH_PROTECTED_PREFLIGHT \
  --preflight-sha256 FRESH_PREFLIGHT_SHA256 \
  --installed-binary /opt/mainrag/api/mainrag-api \
  --output PROTECTED_ACTIVATION_ATTEMPT

sudo -n python3 ops/storage-v2/default-read-switch.py \
  --database mainrag --local-postgres \
  --plan PROTECTED_PLAN --plan-sha256 EXACT_PLAN_SHA256 \
  --approval PROTECTED_OWNER_APPROVAL --approval-sha256 EXACT_APPROVAL_SHA256 \
  --attempt PROTECTED_ACTIVATION_ATTEMPT \
  --attempt-sha256 EXACT_COMMITTED_ATTEMPT_SHA256 \
  --api-token-file PROTECTED_API_TOKEN_FILE \
  --output PROTECTED_DEFAULT_SWITCH_RESULT
```

Migration 058 adds `storage_v2_search_active` for one set-based exact query over
all authorized active generations. It requires the exact activation-manifest
SHA-256 from the latest committed activation receipt, a complete registered
source set with active pointers, unchanged benchmark classification, and the
existing source RLS policy. Ordinary
search excludes benchmark sources; an explicit benchmark scope requires an
administrator. Source filters remain available without naming a generation.
Installing the migration does not select this read path.

Migration 059 adds `storage_v2_active_source_state` for source inspection under
the same complete-set receipt guard. `mainrag source state NAME` requests this
active state only when the API has the exact manifest configured. An explicit
`--generation SEQUENCE` retains named candidate inspection. Active inspection
checks the entire source set before delegating to source RLS and benchmark scope;
it returns the activation digest and selected read path with the state counts.

Migration 060 applies the same exact pointer-set digest check to active search.
The previous set-based evaluator is private; SQL clients use
`storage_v2_search_active`, which rejects pointer drift before evaluating a
query. The migration does not change active pointers or the configured default.

Migration 061 adds manifest-bound active `card`, `explain`, `layers`, and
`ownership` commands. The active SQL function checks the complete activation
receipt, applies source read permissions, and excludes benchmark sources unless
an administrator requests test scope. The API's intelligence read-path endpoint
reports the configured default. These four CLI commands use `--read-path auto`
by default: legacy when no activation manifest is configured, active storage v2
when it is. `--read-path current` remains an explicit legacy rollback route;
`--read-path storage_v2_active` selects active storage v2 explicitly, and
`--generation SEQUENCE --source NAME` retains named candidate inspection.
The active command response includes the manifest digest and source-scoped
results. Installing migration 061 alone does not switch the default.

The API keeps legacy reads as the default unless
`MAINRAG_STORAGE_V2_DEFAULT_READ_MANIFEST_SHA256` names that exact reviewed
activation manifest. The coupled switch also installs
`MAINRAG_STORAGE_V2_ACTIVE_INGEST_COMMIT_SHA`, bound to the reviewed runtime
commit; the API requires both settings together. With that setting, an omitted
`read_path` uses the active storage-v2 set; `read_path=current` remains an explicit legacy rollback route
until cleanup. `read_path=storage_v2` still requires a named source and
generation for verification. `read_path=storage_v2_active` selects the active
set explicitly and fails if the manifest setting is absent. A stale receipt,
missing active pointer, new unactivated source, changed benchmark classification,
or unauthorized source fails
closed. This selector is a prepared application boundary; the actual default
switch still requires the #67 activation procedure, current package readback,
aggregate quality, and post-activation health evidence.
The CLI's `mainrag search` uses `--read-path auto` by default, so it follows
the API selection; `--read-path current` explicitly retains the legacy route.

Migration 065 permits the first ordinary source sync after activation. The
normal admin source and file-sync routes observe the registered source, build
and commit a sealed, verified storage-v2 generation when its watermark changes,
then reverify stored bodies and generation state. A short second transaction
checks the source registry and complete active set before advancing exactly one
active pointer with an immutable receipt. Active search, source
inspection, and intelligence require the latest chained pointer-set receipt.
No-change sync leaves the pointer intact. The file-sync route currently uses
a full-source observation for ordinary adapters and a verified prefix scan for
managed append; it reports the mode explicitly. Legacy mutable
index and Qdrant state remain untouched. Installing migration 065 does not
activate a generation or change the default read path.

Apply migrations 029–065 as the database runtime/table owner. Migration 066 is
an administrator-owned exception: it creates a dedicated NOLOGIN role and
transfers the two append frontier tables and their checked publishers to that
role. The role inherits the runtime owner's privileges to inspect sealed runs;
the runtime owner does not inherit frontier ownership or direct write rights.
Run 066 in one transaction after 065 and before another candidate build. Check
the two frontier table and publisher owners, API `SELECT`/`EXECUTE` rights,
denied direct API writes, and successful publication from a verified run.
Migration 067 applies the same owner separation to the active-ingest receipt
and its controlled pointer-advance function. It permits that dedicated definer
to update `logical_source` and `source_generation` through their guard, with
RLS and all activation, watermark, root, and receipt checks retained. Run 067
as an administrator after 066 and before postactivation ingest. Verify direct
API receipt writes are denied, the first verified ordinary ingest advances one
pointer with a receipt, and a failed ingest leaves the old pointer unchanged.
Migration 068 adds immutable, source-backed lexical segments for release
candidate search. It copies legacy chunk boundaries and rank keys only when the
legacy file hash and complete text equal the sealed artifact, and every chunk
is an exact substring of the bound search document. Otherwise the candidate
producer generates deterministic character segments from that document without
requiring an embedding tokenizer. Verification recomputes each
segment digest and FTS vector from the immutable text; candidate qualification
requires a passing `lexical_segment_integrity` check. Exact and active search
can use the projection for single-term PostgreSQL lexemes, including terms
whose punctuation differs from sparse-posting tokenization. Query-coverage
proof still requires a body match and a matching segment. Run 068 as an
administrator after 067, then build a fresh candidate; existing candidates
have no segment proof and must not be qualified under the new gate. Installation
does not activate a candidate or alter the active pointer. Before installation,
retain the current schema definition and database backup for rollback.
Migration 069 extends source-backed rank to bounded plain conjunctions. It
admits only hits with a matching verified segment for generations with that
projection; older generations retain their previous retrieval path. Its query
evidence checks immutable body text, a matching segment, current indexed hits,
path recall and retained order. Boolean, phrase and exact ASTs retain their
existing behavior. Install 069 after 068.
Migration 070 moves the lexical-segment presence check into a source-authorized
function with row security enabled. Install it immediately after 069 and before
building fresh candidates or using named and active storage-v2 search. A direct
segment-table scan inside the existing exact/active functions fails against
the forced RLS policy.
`schema.sql` is a historical bootstrap and intentionally does not apply these
administrator-only repairs. Do not run the API with an administrator account.

## Source release candidates

The authenticated operators `release-candidate.py`, `final-delta.py`, and
`activation-set.py` accept `--token-file` for a private regular file owned by
the invoking user (mode 0600). The existing CLI credential file can be used
without copying its contents into an environment variable or command line.
`--token-env` remains available for existing automation when no token file is
specified. Operators never print the token.

Candidate construction is source-bounded and never changes an active pointer.
First capture the complete registered source and generation state through a
read-only PostgreSQL snapshot. Use a committed operator checkout and an owned
protected output path outside Git:

```bash
python3 ops/storage-v2/candidate-inventory.py \
  --database mainrag --local-postgres \
  --operator-commit-sha FULL_OPERATOR_COMMIT \
  --candidate-commit-sha FULL_INSTALLED_CANDIDATE_PACKAGE_COMMIT \
  --protected-output PROTECTED_INVENTORY
```

The output file is create-only and mode 0600. It contains private source
registration, configuration, benchmark classification, active pointers and
generation/evidence identities, including persisted qualification manifests and
their database-recomputed digest comparisons. The operator commit identifies the
checked-out inventory tool; the candidate commit separately identifies the
package against which each release candidate is compared. Package installation
and health still require an independent current readback. Standard output contains only counts, random
opaque source references and a digest of the protected snapshot. The command
does not read source content, certify a current adapter watermark or qualify any
candidate. Refresh writer, source, resource and installed-package state before
each source run; the snapshot alone is not an acceptance gate.

Audit that snapshot without printing its private source or manifest data:

```bash
python3 ops/storage-v2/candidate-aggregate-audit.py \
  --inventory PROTECTED_INVENTORY \
  --expected-inventory-sha256 REVIEWED_INVENTORY_SHA256 \
  --protected-output PROTECTED_AGGREGATE_AUDIT
```

The audit creates a mode-0600, create-only per-source blocker report. Standard
output contains only counts, blocker classes and protected artifact hashes. It
checks candidate presence, exact candidate package commit and stored profile IDs,
qualification-manifest digest consistency, verification identities, query gate
results, resource and restart receipts, intelligence command evidence,
benchmark classification and gold-suite binding. When every persisted candidate
passes, the protected audit includes the source-ordered candidate set and its
digest. This is an observed snapshot for the later final-delta procedure, not
an activation manifest or approval. Per-class query counts remain protected;
the public summary contains aggregate counts only. Its status
remains `BLOCKED` even when those persisted proofs are complete: a database
snapshot cannot prove current adapter watermarks, writer state, package identity,
representative gold review, per-class aggregate quality, resource/recovery
budget, benchmark results, or unchanged legacy state. Those external gates need
their own current evidence before a candidate-set manifest can be accepted.

Run construction only with an exact deployed commit and the protected inventory:

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
  --checkpoint PROTECTED_CHECKPOINT \
  --maximum-build-bytes REVIEWED_PER_SOURCE_PACK_PEAK_ESTIMATE \
  --maximum-pool-growth-bytes REVIEWED_TOTAL_THIN_POOL_GROWTH_ESTIMATE

python3 ops/storage-v2/release-candidate.py verify \
  --source-id SOURCE_ID --commit-sha FULL_DEPLOYED_SHA \
  --checkpoint PROTECTED_CHECKPOINT --output PROTECTED_EVIDENCE \
  --gold-suite PROTECTED_GOLD_SUITE \
  --expected-gold-suite-sha256 REVIEWED_SUITE_SHA256
```

The build operator checks the existing pack root before the build POST. Its
reviewed positive `--maximum-build-bytes` estimate must fit alongside the
`--minimum-free-bytes` reserve (40 GiB by default). Missing estimates, a
missing pack root, or insufficient free bytes stop the build before any API
request. The private checkpoint records the observed free bytes and estimate.
For an LVM thin-volume pack root, the operator also requires a positive
`--maximum-pool-growth-bytes` estimate covering packs, database, WAL, indexes,
and expected growth of other volumes during this source. It checks the physical
pool before build and after verification, requires active monitoring, and caps
projected data and observed metadata use below the configured autoextend
threshold. A missing estimate or failing physical gate blocks the build POST.
This is a local resource guard, not evidence that writer, watermark, package,
backup, or cumulative budgets passed. Refresh those issue #66 gates before each
source. The estimates do not authorize a build when any live reserve is below
its minimum.

For a Git source that advances during an independently reviewed source scan,
`build` accepts `--git-snapshot-commit-sha` and
`--expected-source-watermark-sha256` together with the protected
`--source-snapshot-review`, its exact `--source-snapshot-review-sha256`, and
`--source-snapshot-gold-review`. The commit must be an ancestor of the current
registered branch. The operator checks the frozen same-byte gold cases and a
live observation of that commit before posting. The API reads that immutable
commit for both the build and final source check; the checkpoint binds the
review identities for resume. `verify` reobserves the pinned commit from the
checkpoint. Later upstream changes require a separate final delta.

The server-side verification phase recomputes the generation root, decodes and
hashes every referenced body/pack entry, reconciles membership/search/analysis
counts and the active pointer, checks the public intelligence export contract,
and returns protected query seeds. The operator then executes supported current
and named-generation reads, the applicable intelligence commands, latency and
resource gates, records accepted dual-read evidence, and only then submits the
qualification envelope. Raw checkpoints, seeds, result sets, and evidence stay
outside Git with mode `0600`.
The lexical projection check reconstructs every segment digest and FTS vector.
It materializes bounded, overlapping windows of each immutable search document
once, so a large document is not detoasted once per segment. For large
generations its PostgreSQL statement has a 30-minute transaction-local
deadline; the prior deadline is restored before subsequent verification queries.
The integrity comparison and required zero-error counts are unchanged.

Verification also requires a protected, mode-0600 gold-suite JSON file whose
raw SHA-256 was reviewed and supplied explicitly. Schema
`mainrag.storage-v2.gold-suite.v1` binds `source_id`, `generation_id`,
`commit_sha`, `source_watermark_sha256`, `adapter_profile_id`,
`analysis_profile_id`, `search_profile_id`, a nonempty `source_class`, and
`cases`. Each case has an opaque 64-character hexadecimal `id`, a `query`, an
`expected_path_sha256`, and boolean `expects_match`. At least one positive and
one negative case with distinct queries are required; all cases must pass the
existing quality, latency, and degradation gates. Single terms and bounded
plain conjunctions use the server's body-backed segment coverage proof; other
queries require identical ordered current and candidate paths. Automatic
positive seeds are selected only when their path is in the current keyword
top ten for that query. Gold cases join the dual-read query set.
The suite digest and class are recorded in private qualification evidence.
Digest matching does not itself establish representative class coverage; the
reviewed suite and later all-source aggregate must prove that separately.
Failed automatic seeds still fail qualification even if gold cases pass.
Before the restart/resume build request, verification rechecks the pack reserve
and records its free-byte readback in protected attempt evidence. It waits for
an authenticated readback from a new API instance after restart and validates
the complete qualification manifest against the aggregate-audit proof contract
before submitting an immutable candidate.

A failed query gate writes a protected `FAIL` artifact to the requested output
before returning nonzero. It records each query's quality, measured latency,
degradation, expected-hit presence, missing path counts, and ordered identity
hashes, along with the checkpoint and verification needed for diagnosis. Empty
query sets fail. No dual-read or qualification request is submitted on failure;
an existing output is never overwritten. Use a new output path for each retry
and keep failed attempts out of successful performance aggregates. The 2000 ms
default latency gate is unchanged.

Migration 053 evaluates `card`, `layers`, `explain` and `ownership` directly
against their complete required records instead of building and hashing a full
protected export for every command. Generation authorization, all card fields,
filter semantics, ordering and source-wide ownership scope remain unchanged.
Unfiltered layers still returns every visible card; no result cap is introduced.
The full public/protected export and import contracts are unchanged. The owned
`eval.storage_v2.schema.benchmark_intelligence_commands` comparison checks full
result hashes and declared nonempty record counts in alternating repeated runs.
Its server clock includes command execution and complete-result hashing; its
client clock additionally includes connection and a second complete execution.
Synthetic timing is not an installed API or all-source qualification claim.

Transport and runtime failures also retain a private failure artifact: the
current phase, completed verification/intelligence hashes and query proofs, and
the pending query ordinal, hash and ranked identities. Pending result bodies and
exception messages are not copied. Only exception type and any HTTP status are
recorded. A failure before qualification leaves it `NOT_ATTEMPTED`; a lost
qualification response is explicitly `UNKNOWN`, since the server might have
accepted the request. Inspect the named candidate before retrying an ambiguous
POST. A failure artifact never qualifies a source or becomes successful timing
evidence, and existing successful or failed output remains untouched.

Literal query seeds use `literal-coverage-non-inferiority-v1`. This policy allows
additional relevant paths only with read-only server evidence bound to the
source, verified generation, commit, query, and every returned hit identity.
For each candidate hit, the server hashes the complete search text against its
immutable body digest and independently counts the literal term from that text;
the count must equal the collision-checked posting frequency. Unsupported
multi-component projections, mismatched bodies, and out-of-scope identities fail
closed. Token boundaries and case folding follow the lexical profile's database
locale. The proof endpoint supports one to eight plain
alphanumeric/underscore terms up to 128 UTF-8 bytes and at most ten unique
identities per retrieval path.

Source-backed lexical search evaluates sealed segment slices with the current
chunk FTS weights. It does not require the whole-document vector to match:
tokenization at a chunk boundary can produce a valid chunk lexeme absent from
the complete document vector. The evidence fallback verifies that the matching
slice still hashes to the immutable segment witness. Migration 075 restores
ranking on the existing immutable segment vector after 074 temporarily used a
body-only auxiliary vector. The installed chunk projection includes body,
context prefix, and chunk type. Query-time search does not read legacy chunks
or rebuild the vector from document text. Qualification evidence may read a
legacy chunk to prove that a context-only hit is an exact copied segment from
the same-byte legacy file; it labels this separately from a body-text match.
Install 075 after 074 and build fresh candidates for the corrected package.
Migration 076 gives copied legacy segments a rank tier ahead of generated
segments for matching plain lexical queries. The copied segment keeps its
original FTS vector and chunk-order tie key; generated segment sets carry an
order-zero marker. The rank tier uses only immutable candidate segments, so
named and active search retain their ordering after legacy chunk cleanup.
Generated scores are capped below the copied tier; copied scores keep their
uncapped relative order.
Install 076 after 075 and qualify candidates against its exact package.

Every distinct legacy Top-10 path must remain in the candidate Top-10 in the
same relative order, and the seed's expected path must be present. Repeated
chunk hits from one legacy file count as one path. Every candidate hit must
have independent body or exact copied-legacy-segment support. Negative seeds
still require both result sets to be
empty. Additional paths are classified using legacy chunk counts, FTS matches,
and independent literal matches as not indexed, lexical projection gaps, content
gaps, or ranking expansion. The full proof is retained privately; its digest is
bound into both the dual-read query-set identity and qualification manifest.
Missing proof retains the strict ordered-path comparison and cannot justify an
additional hit. This literal coverage policy does not establish phrase, Boolean,
semantic, or general relevance quality; the broader benchmark gates remain
separate. Query seeds are not removed or replaced to make this policy pass.

For a filesystem or Git source whose legacy rows refer to changed or removed source
files, capture `source-snapshot-review.py` before freezing the gold suite. The
review hashes each in-scope registered legacy file against the current source root and
checks the read-only release-adapter watermark before and after that scan. It
stores only path/content hashes and byte states in a protected file. Freeze a
private gold review whose cases match the suite, then bind both review hashes
into the suite as `source_snapshot_review_sha256` and
`source_snapshot_gold_review_sha256`. Pass both protected reviews to
`release-candidate.py verify` with `--source-snapshot-review`,
`--source-snapshot-review-sha256`, and `--source-snapshot-gold-review`. Verification
requires the same live watermark before and after the query set. The protected
review is bounded at 64 MiB and each live watermark request at 10 minutes so
large registered file sets can complete without dropping either identity
check. Only paths proved to have identical bytes retain the ordered legacy
Top-10 requirement; changed or missing paths are classified separately.

Migration 104 replaces the bounded global posting probe and scoped retry with
one authorized scoped posting lookup per query term. It changes the exact and
active readers together, leaves persisted source rows untouched, and retains
the complete result envelope. Install it after 103 and qualify the affected
sources against its exact package; a faster component query alone does not
establish the 2000 ms production latency gate.

Registered filesystem `file_patterns` are positive, case-sensitive globset
patterns evaluated against relative paths; `*` spans directories, so `*.jsonl`
includes nested JSONL files. Selection is applied before content reads and keeps
existing ignore rules and fail-closed traversal checks. Absent filters retain
the previous v2 profile. A configured filter binds its canonical patterns and
compiled byte expressions into a v3 adapter profile and therefore the watermark.
The source review checks the compiled matcher against the registered config;
an older adapter that ignores a configured filter cannot produce this review.
Historical rows outside the filter receive `outside_configured_scope`, without
opening their source files. Ordered recall still applies to every same-byte
in-scope baseline path, and reviewed positive gold expectations must remain
in scope. This classification never authorizes deleting legacy rows: the legacy
sync retains excluded history for the separately reviewed cleanup manifest.

Git review also verifies the registered origin, clean supported branch and
exact checkout commit before and after the scan. It reads the adapter's cache,
not the registered URL as a filesystem path, and never fetches on its own.
Every automatic query still executes. If its original positive path is proved
changed or missing, bind its expectation to the first unchanged path in the
current legacy result before reading the candidate, and persist both hashes
and the review digest. If no unchanged result exists, the original expectation
still fails. This binding never changes a reviewed gold expectation. A
positive gold expectation
pointing to a changed or missing legacy file still fails and must be reviewed
and frozen before the run. Without this review the strict legacy comparison
continues to apply. The review digest, watermark, per-query classification,
and gold binding enter the immutable qualification manifest and aggregate
audit. The source review does not exempt a candidate hit from the independent
query-coverage proof.

Filesystem discovery for this command returns metadata and file paths rather
than retaining the entire corpus. Files above one MiB are split into
deterministic, contiguous, UTF-8-aligned byte ranges of no more than one MiB
plus three UTF-8 continuation bytes. Where possible, each boundary moves
backward within a bounded 64-KiB window to retain complete lines. The ranges
preserve the original path and must cover the physical file
exactly once; any gap, overlap, truncation, or length drift aborts the build.
The builder reads items serially, rechecks each content hash after the source
watermark is captured, rediscovers and rehashes the complete source immediately
before sealing, and reports the fragment count, largest item, and conservative
source/pack buffer peak in phase telemetry. Changed, added, or removed content
aborts the candidate; it is never accepted as a mixed snapshot. The production
watermark also binds the registered source type/path and adapter profile,
including the fragment-size contract, without publishing those protected
values.

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

The candidate integrity verifier hashes both the stored entry and its complete
decoded body without writing a temporary delivery file. It retains pack identity,
bounds, dictionary, codec, decoded-length and full-digest checks. This path never
delivers content: reconstruction and byte-for-byte reuse comparisons still use
verified staging and check the bytes before delivery.

An explicit filesystem comparison exercises the staged-to-sink and integrity-only
paths in alternating order for three rounds, with 128 real verifications per
variant. Linux evidence includes per-thread write-byte and write-call deltas;
ordinary tests also require zero write calls for integrity-only reads. Run the
comparison serially on the selected benchmark host:

```bash
cargo test -p mainrag-api --lib integrity_only_comparison_benchmark -- --ignored --nocapture --test-threads=1
```

Its public synthetic data and host timings do not qualify a production source,
prove an end-to-end latency target, or resolve an intelligence-export SQL timeout.

Migration 051 avoids constructing the complete protected intelligence JSONB tree
for public digest-only exports. Every record still uses native JSONB serialization;
ordered collection text preserves all eight v1 classes, empty arrays, counts,
and the full protected payload SHA-256. Protected export still returns the full
JSONB payload and supports the existing importer. Generation/authorization scope
and the existing array ordering are unchanged. This is not constant-memory or an
unlimited-size streaming format: PostgreSQL TEXT/JSONB size limits still apply.

The differential tests compare both redactions against the original function,
including provenance/import round trips, Unicode/escaping/numeric serialization,
large collections, repeated order keys, error behavior, and migration reapply.
A separate comparison runs complete public and protected exports in alternating
order for three rounds on a disposable database:

```bash
python3 -m eval.storage_v2.schema.benchmark_intelligence_export --output export-comparison.json
```

It requires a committed implementation, retains complete payload hashes, and
writes private evidence plus `TM_KENNZAHLEN` metrics when requested. Its client
milliseconds include connection, transaction, export, and hash validation; they
are not SQL execution-only time or production API latency. Failed or incomplete
work cannot produce a successful timing summary. Actual source qualification is
still required on the installed package under the original operational limits.

Source names, IDs, paths, watermarks, queries, result sets, and raw resource
measurements are protected operational evidence. Public progress may contain
only source counts, type counts, aggregate sizes, hashes, outcomes, and opaque
evidence UUIDs. A candidate is not activation authority.

## Complete search aggregate comparison

### Query-specific compatibility projection fallback

Migration 094 keeps an occurrence's historical compatibility rank when its
immutable legacy projection matches the requested query. Merely having a legacy
projection does not disable generated segment matches for other queries: older
indexing may cover only a prefix of the complete source file. The fallback
collects matching projection identities once within the authorized requested
sources, then excludes those identities from the generated branch. It does not
enumerate every legacy chunk separately for each requested occurrence.

Both the precise search surface and the historical scalar evidence surface use
the same rule. Existing compatibility scores, immutable-body guards, generated
score tiers, owner, privileges and security settings remain unchanged. Migration
replay is guarded against a differing definition. No generation, source
watermark, stored projection or active pointer is rewritten.

The PostgreSQL regression requires a previously hidden suffix conjunction to
appear with source-backed query evidence, while complete existing search
envelopes and legacy ranks remain identical. Duplicate requests, denied sources
and replay are included. Real source quality and latency still require normal
qualification with the original build witness and independently frozen Gold.

Migration 052 materializes the four complete per-occurrence search aggregates
once and uses function-local custom planning for parameter-sensitive source
and generation cardinalities. It does not change scope, Boolean matching,
scoring, ordering, tie breaks, returned content, or candidate limits. Migration
replay preserves function identity, owner, ACL and security configuration;
only the explicit planner policy is added. The caller's policy is restored on
return. Partial/missing rewrite anchors or conflicting planner settings fail closed.

The differential SQL suite compares full results for terms (including oversized
terms), phrases, exact identifiers, Boolean combinations, missing matches,
source filters, and unavailable optional profiles. Real generic-plan assertions
check aggregate execution counts and retain late content materialization.

After committing the implementation, run the isolated comparison:

```bash
python3 -m unittest eval.storage_v2.schema.test_materialized_search_aggregates eval.storage_v2.schema.test_search_aggregate_benchmark -v
python3 -m eval.storage_v2.schema.benchmark_search_aggregates --repetitions 3 --views 96 --output comparison.json
```

Each variant must score the complete declared view set and produce identical
full-result hashes for all three query classes in every alternating round.
The comparison measures the original generic-plan policy against the combined
materialized/custom-plan policy, not materialization in isolation. It owns a
disposable PostgreSQL server and rejects a configured
external test socket. Optional `TM_KENNZAHLEN` output publishes separate
`search_aggregates` server-execution and client-wall metrics. The latter includes
a second execution to check complete result identity; the clocks are not
interchangeable. This synthetic projection is neither full authorized API
qualification nor an isolated production resource measurement. Materialized
results can still use memory or spill; no constant-memory claim is made.

## Protected legacy cleanup inventory

`cleanup-plan.py` captures a read-only PostgreSQL catalog and optional Qdrant,
tracked runtime-source, and retained-generation reachability inventories for
issue #68. Write its output to a private directory outside Git. The output is
created once with mode 0600; an existing artifact is never replaced.

```bash
python3 ops/storage-v2/cleanup-plan.py \
  --database "$DATABASE_NAME" --local-postgres \
  --count-relation indexing_outbox \
  --retain-all-generations \
  --export-root "$PRIVATE_EXPORT_DIR" \
  --output "$PRIVATE_EVIDENCE_DIR/cleanup-catalog.json"
```

The retained-generation selector can instead name specific generation IDs.
It must include every active generation and active pointer. The inventory also
protects legacy-hit mappings, stored intelligence occurrences, and items in
building ingest runs. It reports outbox action/status classes and per-pack
body and entry counts. The root set is incomplete for external export retention
and historical run/identity records; bodies outside it are not deletion
candidates. The optional Qdrant lists are read twice but cannot share the
PostgreSQL snapshot. Text matches in tracked runtime files are candidates for
manual caller review, not proof about an installed binary.

Explicit export roots are hashed file by file with symlinks rejected. Their
scan is bounded and non-atomic; every required root must be named and reviewed.
`cleanup-manifest.py` turns a protected catalog into a create-only disposition
draft. Its object list includes observed identities, sizes or counts where
available, and `UNREVIEWED` for every object without an exact decision. An
optional private decisions JSON uses schema
`mainrag.storage-v2.cleanup-decisions.v1`, the exact catalog file SHA-256, and
an `objects` array of `{key, disposition, reason, authority}` entries. A draft
always has `apply_allowed: false`; decisions are review input, not approval.

This capture has status `OBSERVED_ONLY`. It does not produce an approved cleanup
manifest, verify body/pack integrity, authorize GC, remove runtime callers, or
provide an apply path. Accepted activation, fresh owner approval for an exact
manifest, and post-cleanup verification remain separate gates.
# Durable source batches

`source-batch.py` runs one source at a time from a private, frozen JSON plan.
Each source has an adapter, failure group, planned item count, and ordered
steps. Supported steps are `source-review`, `candidate-build`,
`candidate-reconstruct`, and `candidate-verify`. The plan binds an exact
package commit, source IDs, step arguments, and create-only result paths. Keep
the plan, state, logs, checkpoints, and qualification artifacts outside Git in
a private directory. Run the normal preflight, backup, writer, and resource
gates before starting or resuming the batch.

`run --plan PLAN --state STATE` persists phase transitions and completed item
counts. `status --state STATE` reports phase progress. `stop --state STATE`
requests a stop after the currently running source; `run --resume --plan PLAN
--state STATE` clears that request and continues the same frozen plan. A
failed source blocks later sources in its failure group while independent
groups continue. The run exits zero only when every planned source passed;
failures or reconciliation needs exit one, and a requested stop with unfinished
sources exits two. A completed run is reported as `PASS` or `FAIL`, rather than
an ambiguous completion status. A phase left running after a crash requires
reconciliation; the operator does not retry a possible write automatically.
`reconcile --plan PLAN --state STATE --source-id ID --step-name NAME --outcome
passed|retry --evidence-file EVIDENCE` binds the decision to a private
evidence digest. A passed result is rechecked against source and package
identity. A retry requires no result at the original path. Preserve failed
artifacts and create a new plan for a changed package or result location.

When a local build checkpoint was lost, `reconstruct-candidate-checkpoint.py`
can recover it from a verified generation's immutable build witness, sealed
ingest run, matching adapter observation, and a subsequent API process start.
For a slow live watermark read, the observation can be a digest-bound protected
source review; qualification then requires that exact review and rechecks the
live watermark. The recovered checkpoint retains the original build commit
and marks the missing original capacity observation explicitly. Qualification
still rechecks current resource reserve, generation identity, restart, search,
intelligence, integrity, and source drift before promotion.

### Durable candidate build progress

`release-candidate.py build` saves a private `<checkpoint>.progress.json` before
starting its write. The attempt UUID and exact build commit bind the supported
progress endpoint to this POST. `staged_items` counts work in the current attempt;
`committed_items` reports complete items observed after a database commit.
`transaction_committed` becomes true only after the final verified generation
transaction commits. A full staged count alone is not qualification evidence.

The supported candidate endpoint uses a dedicated connection and retains a
source advisory lock across transactions. Each checkpoint restores transaction-
local authorization and rechecks source configuration, active pointer and access.
A shared maintenance lock excludes native pack replacement/removal while the
builder is using pack locations. A dropped connection cannot return session
locks or transaction state to the application pool.

The content-store phase commits published immutable bodies and the building
run. Complete items then commit after at most 128 new items, 32 MiB of item
input, or 30 seconds checked at item boundaries. A single expensive item can
exceed those thresholds. Membership transitions, sealing and verification stay
in the final transaction; partially built generations never become active.

For a reconciled interrupted build, use a new checkpoint path and
`--resume-run-id RUN_ID`. The backend rejects a different run derived from the
source watermark, profile or build identity. It checks persisted item identities
against the observed input and skips their complete projections. Keep the old
attempt receipts. A lost observation alone does not establish that a writer
stopped; the source lock also rejects concurrent attempts.

Failure receipts retain the last phase, counts, classified database failure and
SQLSTATE without copying database messages or source content. The monitor makes
one final authorized observation after a failed POST, including failures before
the first periodic poll. Missing diagnostics still require reconciliation.

Migration 105 bounds lexical constructor locks by source while preserving the
shared flat/compact identity exclusion. Its replay guard validates the original
function body even after migration, so later body drift cannot be silently
accepted as an idempotent replay.

### Source-backed expectations and bounded rank projection

Migration 086 follows 085 and changes the rank function without changing its
signature or immutable data. Rank the matching compatibility segments first,
then validate the best row's immutable document once per occurrence. The body
predicate is shared by all segments of that occurrence, so the selected score,
tier and tie order are retained. Evaluate compatibility projection presence
once per requested occurrence before scanning generated segments. Authorization
and the existing forced lexical RLS policy remain required.

An automatic query can still name a changed source file when no unchanged
legacy result exists. Such an expectation keeps its original query and path.
It passes only when the candidate's complete body hash equals the independently
frozen current source-file hash, differs from the legacy hash, and has both a
body FTS match and matching verified segment. Missing files, excluded files,
context-only hits, fragment-only hashes and mismatched source hashes fail.
Every unchanged baseline path must still be retained in order. Reviewed gold
positives still require unchanged source bytes; their expectations are never
rebound. The aggregate validates the additional digest-bound source-body proof.

The independent Git review reads a trusted clean cache as its existing owner,
with optional index writes and filesystem-monitor commands disabled. It retains
Git's ownership check and verifies the registered origin, branch and commit.
It rejects a checkout owned by a different account than its cache, or writable
by another account. It does not change global Git trust configuration.

For an operator/SQL-only package, preserve the installed binary's original build
identity and bind the new schema/operator head separately. A new Rust release
build is unnecessary when Rust inputs and the executable are unchanged. Capture
the previous function definition, owner and grants, retain failed trials, execute
a transactional rollback/readback, and requalify preserved generations with the
same frozen gold and unchanged latency ceiling before admitting further runs.

### Complete scoring and late hit identities

Migration 087 keeps both named and active reads on the complete authorized
scoring scope. Shared posting/statistics bindings carry only their needed
columns. Generated-segment provenance is checked after excluding legacy
projections, and occurrence/document guards precede the segment expansion.
The lexical query is constructed once per rank call.

External hit identities are hydrated after finding the exact top-k boundary
in score and lexical sort key. All ties at that boundary are included before
applying the unchanged external identity and occurrence ID order. Full counts,
normalization, score explanations, content and authorization remain unchanged.
This does not reuse a failed latency result or relax qualification limits.

### Requalification of immutable candidates

Migration 088 permits dual-read recording for verified generations and existing
release candidates. The API keeps the original generation, fixture and build
witness checks. No activation or rebuild follows from this eligibility change.

A fresh passing qualification may replace the current evidence slot only with
unchanged source, generation, build commit, watermark and adapter/analysis/search
profiles. The previous complete row is archived before the replacement in a
source-isolated history ledger. Replays retain their identity; a changed manifest
under the same ID, changed build identity, historical ID reuse, failed gates and
history mutation are rejected. The current slot remains unique per generation.

### Restore original candidate projections

`restore-candidate-projections.py` plans and resumes deterministic lexical and
compatibility rank projections for a sealed, verified or candidate generation.
It reconciles the live native adapter watermark against the original witness;
source drift requires a different plan or a real source build. It preserves the
original generation, build commit, membership and inactive pointer.

The private plan freezes the generation/run/registration identities, projection
function definitions and authority, operator bytes and canonical Rust character
chunker. Apply requires an exact private plan digest and a fresh passing
maintenance/resource/backup preflight. Plans and preflights expire after fifteen
minutes at admission; renew them before resuming longer runs. A refreshed plan
can resume the same cursor only when its original generation, source watermark,
operator, canonical planner and projection functions retain their exact identities.
The state retains previous plan digests. Projection completion does not claim a
backup restore, body/pack verification, search qualification or activation.

Groups contain at most 32 original documents and 16 MiB of source text; one
source body cannot exceed 8 MiB. Generated segments retain the accepted Unicode
character chunking, overlap and first-match positions and are submitted in groups
of at most 256 to the existing immutable writer. Existing segment projections
receive compatibility ranks only. Empty legacy file sets remain empty.

Every batch locks the source registry, logical source, original generation and
sealed run before checking frozen identities and invoking the authorized writers.
The operator stores a private, fsynced pending intent before the transaction. If a
commit reply is lost, resume retains the original projection mode, reconciles
immutable source identities and repeats only idempotent projection writes. It
never repeats source construction. Signals stop at the current batch boundary.
The source batch tool accepts the `candidate-projections` phase and requires
`PASS_PROJECTIONS_ONLY`; the subsequent normal frozen-gold qualification remains
mandatory. All temporary database tables belong to their transaction and drop at
commit or rollback; private plans and states belong to the operational evidence
owner and follow its retention manifest.

### Filter lexical work before ranking

Migration 101 retains complete scoped evaluation and exact result identity.
Broad posting lookups use array membership semi-joins. Native lexical matches
retain the GIN-selected identities, apply requested membership, and fetch their
vectors by the complete segment primary key before computing ranks. Matching
copied projections retain their original provenance and ranking tiers.

Broad copied-projection reads resolve their document predicate through the
existing document GIN index once, while retaining complete source/artifact
identity checks and the verified segment fallback. Source aggregation groups
the small authorized source set before sorting it. Small scopes retain their
bounded document reads; duplicate and null requested identities retain the
same result semantics.

Corpus normalization uses a covering token-count index. Large intermediate
rows carry only scoring identities; immutable paths and locators are fetched
after the complete score boundary. Optional stages join nonzero scores during
ranking and recover every returned hit's original status afterward, including
available zero scores, unavailable and failed stages. These changes do not
relax the latency or quality gates.

### Scoped ranks and deferred explanations

Migration 103 bounds copied-projection authorization to the requested sources
and carries the already authorized immutable view through ranking. Complete
canonical flat and compact projections are rejected by the offset fallback
before fetching body text or weighted vectors. Noncanonical restored offsets
retain the original source, root, digest, RLS and vector checks.

Copied projections supply their complete lexical ranking tier before the
result limit. Their term contributions and explanations are calculated for
the returned rows afterward, using the original corpus normalization and
component tie rules. Generated and unprojected rows remain completely scored
before pruning. Scores, statuses, explanations and external identities retain
the same result contract.

Verification now writes a private `<output>.progress.json` at phase boundaries.
It contains phase timestamps, cumulative phase durations, completed query
counts and opaque pending-query identities. A lost qualification response
remains `UNKNOWN`; a running journal is not proof that its process is alive.
Each journal belongs to one attempt and is retained with that attempt's
evidence. An existing journal requires a new output name.

Large parser cache entries retain every symbol, call, signature and source
span in a lossless Zstandard envelope. Serialization streams borrowed parser
fields into compression, avoiding an intermediate expanded JSON array. The
envelope binds the source digest, complete serialized size, compressed digest
and complete result digest. Cache reads retain the old inline representation
and validate every new envelope before reuse. Parser and analysis profiles,
card identities and normalized outputs are unchanged.

Structural-card groups are bounded by both 64 records and an 8 MiB serialized
target. An indivisible larger record is submitted alone. Records and fields
are never trimmed or dropped. Once encoded entries exist, native ingest
requires a producer that understands this cache format; retain a compatible
package for operational recovery.

### Lossless generated lexical blocks

Migration 102 stores newly generated canonical segment groups in immutable
blocks of at most 64 rows. Character boundaries, overlap, first-match locators,
digests, context, chunk types and weighted vectors remain unchanged. Existing
flat projections are not rewritten. `storage_v2_lexical_segment_all` provides
the complete logical relation under invoker security and forced source RLS.
Projection restoration freezes the available lexical relation in its original
package snapshot: older schemas retain the flat relation, while migration 102
uses the complete logical relation rather than physical rows. A layout change
invalidates the frozen plan before any write; it never silently falls back to
flat-only counts when compact rows exist.

Block fingerprints are only a candidate filter for plain positive queries.
Every match and rank uses the full original vector. Queries with other syntax
retain complete scoped evaluation without an unsafe fingerprint shortcut.
Canonical located writes, noncanonical flat writes and old constructor replays
serialize by occurrence and reject conflicting immutable identities. A replay
through an old constructor does not duplicate an already compact row.

Migration replay checks the column layout, generated fingerprint expression,
payload constraints, composite identity, required indexes, forced source
policies and immutable trigger. Lexical verification materializes the visible
generation's complete logical segments and prevents nested rescans of its
segment and chunk sets. It retains every original digest, weighted-vector and
missing-projection check.

Installing these migrations does not qualify, activate or retire a generation.
After compact rows exist, rolling the SQL schema back to a reader that only
understands flat projections is forbidden. Keep compatible readers and verified
backups. Capacity admission must include measured database/index growth, packs,
retained WAL/archive growth, backup growth and the final-delta reserve. A small
vector compression result alone is not a full-source resource gate.
