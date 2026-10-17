# Maintenance boundary for storage-v2 activation

The preparation preflight requires mutating services to be stopped. Source
watermarks must therefore be captured through the authenticated live API before
entering that boundary. An offline API cannot provide a new HTTP readback.

The activation operator supports this sequence:

1. Finish the complete persisted candidate-set audit and measured quality,
   frozen-gold and benchmark checks. Capture the remaining volatile gates
   inside the maintenance boundary before accepting the final aggregate.
2. Use the `capture-watermarks` phase with the exact audit digest and installed
   binary. The receipt binds every source watermark, adapter and item count to
   the complete candidate set and to the running API process and binary.
3. Stop the API and other inventoried writers through the approved operational
   procedure. Capture a fresh passing preparation preflight. Watermark capture
   itself performs no service action and does not claim maintenance is active.
4. Pass the protected receipt and its digest through `--quiesced-watermarks`
   and `--quiesced-watermarks-sha256` to both `plan` and `apply`. Planning binds
   the receipt digest into the approved plan. Apply rejects a changed digest.
   Every observation must remain at most 300 seconds old. The API unit must be
   stopped successfully and its local listener must refuse connections; a
   timeout does not prove absence. The normal source, package, schema, pointer,
   quality, resource, backup and approval gates remain required.
5. Commit the complete pointer set through the existing controlled transaction.
6. Use the immediately coupled default-switch operator. For an API left stopped
   by this procedure, it installs the accepted default selector before starting
   the service and verifies the running binary, active read path and committed
   pointers. It does not start an intermediate API using legacy defaults.
7. Complete regular ingest, API/CLI search, intelligence and benchmark acceptance
   before any legacy cleanup. Keep the committed receipt if observation fails;
   reconcile its original operation ID rather than repeat activation.

The existing online watermark mode remains available for separately proven
read-only API arrangements. A fresh preflight file alone does not freeze writers
or external source content. The operational owner must preserve the approved
source snapshot and maintenance boundary across capture, commit and default
switch. Neither operator creates a backup or changes backup scheduling.

A watermark receipt is a bounded readback, not a substitute for aggregate
acceptance, source reconstruction or recovery evidence. Maintainer self-review
must remain explicitly distinguished from independent review.

## Legacy retirement and retention

The implemented [native complete-file interface](storage-v2/native-complete-files.md)
is a consumer of retained bodies and mappings, not evidence that a production
population has been preserved or that legacy retirement has run. Its stable
`legacy-file-v1:` identity includes the original source, file, revision, raw byte
length and SHA-256; the source-path digest and original metadata hash are checked
separately. `active` and canonical positive generation sequences retain their
source authorization and explicit current-versus-retained resolution semantics.

Complete-file preservation includes files with no hits and empty files. It must
be checked against independently captured original byte identities for the
entire retirement scope, alongside the separate all-hit mapping coverage. A
proof of the bytes retained by an adapter does not establish completeness of
source data that the adapter never captured.

### Commit, reconciliation and the legacy-table boundary

The local `mainrag-complete-file` CLI supports `register-existing`,
`register-zstd`, `read` and `reconcile`. Both registration operations check the
original legacy snapshot and mapping compare-and-swap, then call the verified
native reader before returning. They require the legacy content tables and must
finish before those tables are dropped. `read` and `reconcile` remain native
consumers after that boundary. Keep the resolver, complete-file component reader,
mapping and history-root dependencies in the accepted runtime inventory.

The CLI writes a create-only intent before dispatch and a separate committed
receipt after the transaction's COMMIT acknowledgement. A registration receipt
contains a verified-read proof; accepting it still requires its exact request,
manifest, identity, mapping state and caller authorization to match. Missing
acknowledgement leaves an unknown outcome. `reconcile` observes committed visible
mapping metadata without dispatching again or reading pack bytes; its result
explicitly leaves raw-byte acceptance pending. Neither a local cursor nor
reconciliation alone replaces a committed integrity proof. Preserve published
packs after errors or uncertain commits until the existing explicit recovery or
GC procedure establishes their disposition.

### KEEP and DELETE have different scopes

Use the existing catalog/runtime inventory and cleanup-manifest tools to bind
the actual objects and their disposition. An inventory or generated disposition
draft does not authorize application. An omitted object never inherits DELETE.

| Object class | Legacy-cleanup disposition | Subsequent maintenance boundary |
| --- | --- | --- |
| Active and retained generations, native graph, bodies and authorization state | KEEP | Reachability and reader safety are decided by the separate native GC contract |
| Durable old-hit and complete-file mappings, preserved history roots and their reader functions | KEEP | Legacy-row removal must not break resolution, reconstruction or source authorization |
| Native packs and retained exports | KEEP | Legacy cleanup cannot grant their deletion; pack reclamation requires its own accepted roots, integrity checks and removal receipts |
| Packs and artifacts pinned by a retained backup | KEEP with their exact bytes, paths and identities | Exclude pinned pack IDs from GC, repack and unlink while that backup retention remains in force |
| Explicitly inventoried legacy-only relations, functions, indexes and external components | DELETE only when included in the accepted exact manifest | First prove replacement consumers, complete preservation and applicable post-activation acceptance; unknown dependencies remain protected |

The original cleanup validator protects native and authorization objects and
rejects generation, pack and export deletion through legacy cleanup. The
maintenance owner must also carry external backup retention into the protected
scope; a GC command being available does not prove those backup pins were included.

Apply only the exact accepted cleanup plan after its live identity and dependency
checks. Preserve the original operation and receipts if its outcome is uncertain;
do not regenerate a plan to hide a partial application. Post-cleanup acceptance
must use the retained active/named readers and compatibility consumers. It is
separate from the earlier candidate, activation and ordinary-ingest evidence.

### Native GC, repack and physical accounting

[`native-gc.py`](../ops/storage-v2/native-gc.py) marks from active and retained
membership, durable mappings/history, explicit export roots and other protected
references. Reference counters and a superseded status alone are not deletion
authority. Unknown incoming references remain roots. Native graph sweeping does
not itself reclaim dead entries in a shared pack.

Use the existing [pack-maintenance contract](storage-v2.md#callable-pack-maintenance)
for verified replacement, placement switching, reader-epoch drainage and exact
old-file removal. Retained backup packs must remain excluded from those changes.
Legacy cleanup, native GC and repacking have distinct manifests and outcomes;
none inherits deletion authority from another merely because it completed.

Representation conversion or row deletion does not prove physical release.
Measure the same retained population before and after maintenance, including
packs, heap/TOAST, every search projection and index, compatibility metadata,
receipts and backup retention. Distinguish permanent allocation from temporary
WAL, scratch and staging. Report only measured physical reclamation and total
net savings; a smaller datum or an acknowledged DELETE is insufficient.

## Local logical backup and recovery evidence

[`local-backup.py`](../ops/storage-v2/local-backup.py) produces one bounded local
custom-format database dump and schema from an exported snapshot, plus globals
with role-identity checks before and after the dump. Planning is metadata only.
Actual execution requires an exact plan, fresh resource admission, a quiescent
writer boundary and a retention decision
covering the external immutable pack closure. The output cap is an admitted
budget, not a promise that compression will fit. Preserve an existing intent or
failure rather than start another dump to conceal an uncertain result.

The dump does not copy the external pack payloads. Recovery therefore depends on
the retained exact pack bytes and paths as well as the database artifacts. The
completion validator binds the database/schema identity, successful dump exits,
streaming digests, artifact identities, readable archive table of contents and
the current external-pack retention decision. Its evidence level is
`local-logical-dump-with-retained-external-packs`; it explicitly reports
`restore_tested: false`, `self_contained: false` and
`credential_recovery_external: true`.

A valid completion is not a tested restore, crash recovery, point-in-time
recovery or disaster-recovery claim. Retained files on the same storage do not
prove independence from that storage's failure. Scheduling or remote backup
policy remains a separate operational concern. Do not repeat a completed dump
merely to refresh a status label: reuse its exact completion while its age,
database/schema, artifact identities and external retention satisfy the next
consumer's contract. Changed or expired evidence must be reported explicitly.


## Legacy compatibility capacity checks

The legacy-hit operator supports `--kernel-capacity-readback` as an explicit,
plan-bound choice. The reviewed plan and apply invocation must select the same
provider. Existing plans continue to use the original full LVM checks.

The faster provider retains the root and pack-filesystem free-space checks on
**every batch**. It admits the full reviewed growth estimate and reserve through
LVM, then reads current physical data and metadata occupancy from the kernel on
**every batch**. Occupancy is never cached. The pool UUID, active writable state,
segment count, geometry and capacity must match the admitted pool. Data use plus
the reviewed growth must remain within the same ceiling; metadata must remain
strictly below its existing limit.

The full LVM identity, monitoring, autoextension policy and admission are
rechecked at least every 60 seconds between batches. Changed configuration,
missing readback, a stopped observer, timeout, malformed response or exhausted
space aborts before another producer request. A long producer request is still
subject to its existing bounded transaction and spool contract; the operator
rechecks capacity before the next request.

One owned privileged observer serves only the fixed Linux device-mapper table
status read for the admitted UUID. It accepts sequence numbers, not arbitrary
commands or ioctl operations. It receives no source content or credentials.
Normal owner exit closes its input pipe and releases its descriptor; failed or
unresponsive owned processes are not accepted as capacity evidence.

Read-only timing of the complete staged provider covers filesystem checks,
current kernel occupancy, response validation and bound calculations. Timing is
not a production write-budget admission or a completion estimate for all-hit
mapping. Mapping completeness, package binding, candidate watermarks, durable
proofs, source content reconstruction and cleanup approval remain separate.

## Concurrent immutable mask proofs

Migration 179 permits distinct occurrence proofs to be checked concurrently
under a shared advisory transaction lock for their source. Ordinary source
writers still require the exclusive lock. The exact existing reconstruction,
authorization, canonical identity and block-manifest checks remain in place.
Each proof retains its own transaction and post-commit acknowledgement.

Existing migration-177 proof keys and row bytes remain valid through a private
immutable semantic binding. This records the original verifier definition and
dependency digest separately from the approved current implementation hash.
Changed code or semantic dependencies fail closed. The application cannot
modify the binding. The migration changes one public function and its sealed
contract hash; it does not rebuild generations or rewrite existing proofs.

An operator must bound concurrency and memory, preserve ordered results and
reconcile unknown commit outcomes before resuming. Concurrency alone does not
establish source completeness, performance acceptance or activation readiness.
