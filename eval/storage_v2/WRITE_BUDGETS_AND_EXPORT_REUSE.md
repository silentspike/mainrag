# Bounded writes and reusable intelligence export proofs

This block addresses two measured costs: a native search-document insert
cancelled at the previous two-minute write deadline, and complete intelligence
exports repeated by otherwise unchanged candidate verification.

## Detached writer settings

The checkpoint writer applies transaction-local settings at admission and after
each durable checkpoint: a ten-minute statement deadline, 128 MiB of maintenance
memory, a 16 MiB GIN pending-list threshold and warning-level client messages.
Readers retain their own deadlines and settings. Dropping the detached connection
rolls back unfinished work and releases its source lock. The existing physical,
pack, local-WAL and source-identity gates remain required. These settings bound
individual operations; they do not prove a throughput improvement or source
completion. GIN can exceed a pending threshold during one large insertion.

## Reusable export proof

Migration 127 stores only the complete public export envelope, its original
serialized-byte count and its exact identity. The identity includes the verified
generation, verification manifest, source revision, native digest protocol,
exporter and resolver definitions, PostgreSQL version, encoding and collation
version. Every cache lookup repeats source authorization. A reader package
change alone does not invalidate the proof.

Statement transition triggers advance the affected sources' revisions whenever
exported records or their membership/identity dependencies change. Updates with
unchanged counts, deletes, source reassignment and transaction rollback are
covered. Truncation invalidates all cached sources. A changed revision or exporter
definition requires one new complete export. A source change during export
refuses storage of that snapshot's proof. Other sources keep their proofs.

The first proof for a generation still requires the complete ordered record
stream. Historical envelopes without the required revision identity are not
silently imported as current proofs. Verification reports proof reuse and bytes
actually streamed separately from the size of the retained proof.

Artifact reconstruction, body/pack integrity, source watermarks, authorization,
lexical integrity, current search quality and the intelligence command surface
retain their existing gates. Export reuse does not accept a candidate, activate
sources, import data or authorize cleanup. The cache is derived metadata and
must not become an independent retention root.

## Focused evidence

The SQL fixture exercises identical proof reuse, same-count card updates,
source-local invalidation, stale-proof rejection, rollback, exporter changes,
truncation, unauthorized reads and incomplete envelopes. The existing native
producer/checkpoint fixture checks write settings across COMMIT, reader deadline
isolation, source writer exclusion, durable interruption/resume and an actual
Rust export-cache hit. These are fixture results; production source completion
and the full #66/#67/#68 gates require their own observed evidence.
