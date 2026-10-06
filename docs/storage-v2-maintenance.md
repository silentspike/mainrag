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
