# Storage v2 architecture and migration contracts

> Status: planned, not active
>
> Contract baseline: public `main` at `b969dc7`, reviewed 2026-08-13
>
> Parent initiative: [#53](https://github.com/silentspike/mainrag/issues/53)

This document defines the normative target contracts for MainRAG storage v2.
The current supported PostgreSQL/Qdrant model remains documented in
[`architecture.md`](architecture.md). A table, state, or flow described here is
not evidence that it has been implemented, migrated, deployed, activated, or
released.

The design separates source snapshots, immutable content, reusable analysis,
retrieval identity, and source-bound locations. Its central rule is:

> Content may be deduplicated globally, but visibility, location, and external
> hit identity always resolve through a source-visible occurrence.

## Scope and non-claims

Storage v2 defines:

- immutable, source-local generations and atomic activation;
- stable source items and immutable artifact versions;
- content-addressed bodies and integrity-checked packs;
- lossless structured content graphs and deterministic reconstruction;
- globally deduplicated retrieval views with source-bound occurrences;
- durable, ordered compatibility mappings for legacy hit identifiers;
- exact search semantics with a complete fallback;
- versioned intelligence provenance; and
- additive migration, evidence, garbage-collection, and authority boundaries.

This document deliberately does not:

- choose a PostgreSQL search backend before the exact Top-K prototype and
  backend-qualification work are accepted;
- claim that storage v2 tables or runtime paths exist;
- authorize a database mutation, deployment, activation, cleanup, tag, RC, or
  release;
- claim availability, rollback, crash recovery, or disaster recovery without
  an exercised test bound to an exact candidate; or
- replace the current architecture before the accepted activation transaction.

Open runtime work may change current indexing, search, intelligence, schema, or
operations paths while storage v2 is developed. Every implementation issue must
refresh those paths and reconcile semantic conflicts instead of treating this
document's baseline commit as permanently current.

## Current baseline

At the contract baseline, `schema.sql` centers source state on mutable `sources`,
`files`, and `chunks`. Symbols, chunk embeddings, call-graph data, and an
`indexing_outbox` refer to those identities. The index service discovers and
parses files, writes chunk/search state to PostgreSQL, and coordinates Qdrant
work through the outbox.

The current keyword path builds `websearch_to_tsquery` or
`phraseto_tsquery` queries for `simple` and `english` vectors, ranks with
`ts_rank_cd`, and applies channel and per-source result limits after scoring.
Those output limits are not a proven upper bound on evaluated matches. Semantic
retrieval uses Qdrant, and the runtime fuses channels before formatting
chunk-bound results.

The intelligence service stores source/chunk-bound symbols, cards, relations,
and curated negative evidence. Storage v2 must migrate supported semantics and
provenance; parser-visible facts alone cannot reproduce source-profile-derived
domain fields.

Public PRs [#43](https://github.com/silentspike/mainrag/pull/43) and
[#47](https://github.com/silentspike/mainrag/pull/47) were open when this
contract was written and modify current runtime paths. Their contents are not
part of `main` and are not described here as implemented storage-v2 behavior.

## Normative language and evidence states

The words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** express contract
strength. Evidence states remain distinct:

1. specified;
2. source present;
3. syntactically validated;
4. tested against fixtures;
5. verified against a sealed candidate;
6. release candidate;
7. active;
8. deployed and observed;
9. legacy state cleaned up; and
10. released.

An earlier state never implies a later one. In particular, merge is not
deployment, activation is not cleanup, and cleanup is not release.

## Entity and identity contracts

The names below are logical contracts. Concrete PostgreSQL types, indexes, and
functions belong to the schema issue.

| Entity | Identity | Mutability and owner | Lifecycle |
| --- | --- | --- | --- |
| Logical source | Stable `source_id` | Mutable control row owned by source administration; it may point to one active generation | Created before ingestion; retained while the source and its durable mappings exist |
| Source generation | `(source_id, generation_seq)`, with a separate internal ID constrained to the same source | Membership snapshot is semantically immutable after sealing; only controlled state transitions are allowed | `building` through `superseded`, with controlled reactivation |
| Source item | `(source_id, item_key, item_kind)` | Stable logical identity owned by its source; path/location changes are represented explicitly rather than rewriting content identity | May have many artifact versions and disjoint membership intervals |
| Artifact version | Stable internal ID bound to exactly one source item and immutable witness | Immutable; owns exactly one content anchor | Created for a witnessed item version, then reused wherever the same version is visible |
| Generation membership interval | Source item, artifact version, and half-open source-local sequence range | A later generation may close an open interval or open a new interval; it MUST NOT change visibility at an earlier sequence | Visible for `[valid_from_seq, valid_to_seq)` or from `valid_from_seq` onward |
| Content body | Algorithm-qualified content hash, byte length, and verified bytes | Immutable global bytes; physical inline/pack placement may change without changing content identity | Reachable while retained roots reference it; reclaimed only by verified GC |
| Pack | Immutable pack ID plus manifest/integrity identity | Bytes are immutable after publication; replacement creates a new pack | Candidate, verified, published, retired, then reclaimed after reader safety |
| Content node | Domain-separated digest over type, logical length, leaf content identity, and ordered typed children | Immutable and globally reusable | Reachable through artifact roots and retrieval components |
| Retrieval view | Unique digest over its typed ordered component contract | Immutable and globally deduplicated; contains no source, path, authorization, or parent identity | Reused by any compatible occurrence; collected only when unreachable |
| Occurrence | Stable occurrence ID bound to an artifact version and retrieval view | Immutable source-bound location and role; parentage is occurrence-specific | Visible only through a generation membership and authorization scope |
| Search document | Stable document identity derived from indexed content and search-profile version | Immutable per search-profile version; does not own source visibility | Built and qualified before a generation can become a release candidate |
| Analysis profile | Stable profile ID plus immutable parser/rule/model versions | Versioned configuration; never silently reinterprets existing analysis | New semantics create a new profile/version and new provenance |
| GC epoch | Monotonic epoch ID with a root manifest | Append-only evidence owned by maintenance tooling | Plan, mark, verify, apply, retire, and audit |

Database-generated IDs are locators, not content identities. Digests MUST NOT
include unstable database IDs. Every digest encoding MUST be domain-separated,
canonical, length-delimited, and versioned so ordering or type ambiguity cannot
collapse distinct objects.

## Source generations and membership

### Source-local sequence

`generation_seq` is monotonically allocated per logical source. There is no
global generation number. Each generation represents one consistent state of
one source at a recorded witness/watermark.

Generation membership is represented by half-open intervals:

```text
(source_id, source_item_id, artifact_version_id,
 valid_from_seq, valid_to_seq)
```

`valid_to_seq = NULL` denotes an open interval. Intervals for the same source
item MUST NOT overlap. A generation reads the row whose interval contains its
source-local sequence.

When generation `n + 1` changes or deletes an item, it closes the prior open
interval at `n + 1`. A changed or new item opens a new interval at `n + 1`.
Unchanged intervals remain open. Implementations MUST NOT copy every unchanged
membership into every generation. The `A -> B -> A` case creates three
membership intervals while the two immutable artifact versions remain reusable.

All source/item/version/generation relationships MUST be enforceable with
source-bound foreign keys. A row from one source MUST NOT be attachable to
another source's generation merely because internal IDs exist.

### State machine

```text
building -> sealed -> verified -> release_candidate -> active -> superseded
                              ^                         |
                              |                         |
                              +-- controlled recheck <--+
```

The reactivation path is specifically:

```text
superseded -> release_candidate -> active
```

It requires the same current verification as a new candidate. It is not a
direct pointer rollback and MUST NOT be described as a tested recovery path
unless that exact path has been exercised.

- `building`: the only state in which owned membership and derived objects may
  still be added.
- `sealed`: the candidate is immutable; counts and root identities are fixed.
- `verified`: reconstruction, integrity, authorization, and semantic invariant
  checks passed for the exact sealed identity.
- `release_candidate`: all issue-owned qualification and migration gates passed.
- `active`: selected by controlled activation; at most one per source.
- `superseded`: previously active and retained according to policy.

Status and active-pointer changes outside controlled database functions MUST be
rejected by privileges and invariants. Sealing fixes the candidate membership;
later interval closures describe later source sequences and MUST NOT alter the
sealed generation's visible state.

### Atomic activation

The logical source pointer and generation state are one invariant. If a source
has `active_generation_id`, it MUST reference that source's only `active`
generation. A deferred commit-time constraint validates the final state so the
activation function may perform its ordered internal updates without exposing
an invalid committed state.

Final cutover accepts the complete set of per-source release candidates and
activates all of them in one database transaction. For every source it:

1. verifies the expected active pointer, candidate identity, state, witness,
   schema/code identity, and acceptance manifest;
2. marks the prior active generation `superseded` when one exists;
3. marks the candidate `active`; and
4. moves the logical-source pointer.

Any missing source, stale pointer, failed check, or invariant violation aborts
the entire transaction. Readers see the complete old set or complete new set,
never a partially activated mixture.

## Artifact witnesses, anchors, and exact reconstruction

An artifact version records an immutable source witness such as a commit object,
Merkle root/frontier, append frontier, or validator token. Witness type and
adapter profile are part of the interpretation contract.

Every artifact version MUST have exactly one content anchor:

- `content_root_node_id` for structured content; or
- `raw_body_id` for content without a structural graph.

Occurrences do not replace this anchor because not every stored byte range is a
search occurrence. For a structured artifact, traversing the ordered graph from
the root MUST reconstruct the exact artifact byte sequence. Verification
requires byte-for-byte equality, expected byte length, and the artifact's
algorithm-qualified content hash. Comments, whitespace, delimiters, and unknown
parser regions therefore remain reachable even when they are not searchable.

Parser failure MUST NOT hide otherwise valid source bytes. It may reduce the
verified analysis level, but the raw artifact remains reconstructable and
eligible for complete fallback text handling.

## Content bodies, collision handling, and packs

### Content identity

A content body stores an exact immutable byte sequence. Its logical key includes
the hash algorithm/version, digest, and byte length. On an apparent duplicate,
the ingest path MUST compare the complete bytes before reuse. A digest match with
different bytes or length is a collision event: ingestion fails closed, neither
object is merged, and public evidence contains no bytes.

This full-byte comparison is mandatory at the trust boundary even when the
selected hash makes collisions operationally improbable. Changing hash
algorithms creates a new algorithm-qualified identity; it does not rewrite old
identities in place.

### Physical placement

A body is stored either:

- inline; or
- in exactly one immutable pack entry identified by pack, offset, stored length,
  codec/dictionary version, and entry digest.

Neither both nor neither is valid. Decompression MUST be bounded by declared
logical length. Reads verify entry digest and reconstructed content identity
before returning bytes as trusted. Corruption fails closed and identifies the
object without publishing its content.

### Pack reclamation

Reference counters are advisory and MUST NOT decide reachability. GC performs
mark-and-sweep from active and retained generation roots, artifact anchors,
protected mappings/exports, and in-flight reader epochs.

Dead entries inside a shared pack do not reclaim space. A versioned maintenance
policy selects packs for repacking based on measured dead-byte ratio and resource
headroom; the numeric threshold belongs to the pack implementation and its
benchmarks. Repacking:

1. writes a new immutable candidate pack containing only live entries;
2. verifies every entry and the pack manifest;
3. atomically switches body placement metadata in one transaction;
4. retains the old pack until all pre-switch reader epochs are gone; and
5. reclaims the old pack only under an accepted GC manifest.

A crash before the metadata switch leaves an unreferenced candidate pack. A
crash after the switch leaves the old verified pack available for deferred
cleanup. Readers always observe a complete old or complete new pack.

Migration 055 orders reader registration against switch **commit**, not merely
the retirement timestamp. Registration takes a shared transaction advisory
fence; switching takes the matching exclusive fence before GC/pack row locks.
Both release at transaction completion. Registrations can proceed concurrently.
An epoch must be registered before placement is fetched in a subsequent SQL
statement, remain open through verified pack I/O, and finish only afterwards.
Registration, switch, drain and reclamation reject non-READ-COMMITTED isolation:
a retained snapshot could otherwise select an already retired placement.
Do not combine registration and placement selection into a single statement
or close an epoch merely because its registration transaction committed.

Cancellation or disconnection is not proof that file I/O finished. An abandoned
open epoch must retain bytes until reader quiescence is established; no elapsed
time or backend disappearance alone authorizes removal. The SQL concurrency
tests prove the commit fence, not complete reader integration or physical
reclamation. The composed maintenance operator and crash/recovery qualification
remain required by #58.

The Rust candidate verifier and existing-body reuse reader now register epochs
before querying placement and finish only after verification, including ordinary
error returns. Cancellation retains an open epoch for fail-closed recovery.
The streaming reuse pass shares one epoch across its body groups; it does not
register/finish once per body or materialize all source bytes at once. Whole
generation no-op reuse returns before this pack-read pass.
Packed reuse takes pack identity, offsets, codec and dictionary from the same
placement query, even if placement changed after the initial identity lookup.
The hosted Rust fixture combines real identity/zstd pack files with migrations
030/055, a concurrent metadata switch, rejected early reclamation, exact-byte
reads on both sides, delayed old-file removal, corruption rejection and retained
epochs on cancellation. It uses minimal prerequisite tables and does not replace
the full-schema authorization suite.

### Callable pack maintenance

`services::pack_maintenance::repack` accepts an administrator connection, pack
root, old pack ID, stable caller-persisted replacement ID, accepted global GC
epoch and explicit `RepackPolicy`. It copies only bodies still assigned to the
old pack. Bodies are conservatively retained until an authoritative graph sweep
has removed their placement; neither `live_bytes` nor reference counters decide
what is copied. Unchanged body IDs preserve all 1:n anchors.

Admission bounds entry count, total logical bytes, I/O buffers, dead-byte amount
and ratio, and measured filesystem free space plus an explicit reserve. There
are no claimed optimal defaults. The space check is conservative admission,
not an exclusive reservation against other applications. Write failures retain
the old pack. Dictionaries are loaded individually with a 1 MiB admission bound;
replacement entries currently use the selected codec without a dictionary.

The operator holds a root-wide filesystem lock and a reader registration in its
database transaction, rewrites one verified body at a time through bounded
staging, verifies the candidate, publishes its file, registers its metadata,
switches placements, verifies every replacement entry and then commits. A retry
can adopt an unregistered replacement file only after every entry matches the
newly reconstructed manifest. An occupied mismatching target is never replaced.
The persistent `.maintenance.lock` inode must not be deleted while maintenance
may run. All maintenance of the same root must participate in this lock.

`finish` re-verifies replacement bytes for an unfinished removal, requires
drained readers and GC sweeping/complete authority, commits removal permission,
unlinks the exact old file, syncs the directory and records an immutable receipt.
An interruption after unlink but before receipt is resumable without double
counting. Completed receipts are idempotent even after later replacement work.
Unfinished predecessor removals block rewriting their replacement pack, keeping
the supported recovery sequence explicit.

Migration 056 counts reclaimed **file bytes** only from removal receipts. Packs
with removal permission but no receipt remain in conservative stored-byte
accounting. Dead bytes use current placements instead of the advisory live-byte
field. These are tracked file lengths, not device allocation, filesystem
compression savings or proof that a hard-linked extent was physically freed.
The operator returns serializable size/count/buffer reports; its writer-buffer
field does not claim whole-process peak RSS or memory used by codecs/metadata.

The real-file/PostgreSQL fixture exercises dead entries, a nonzero source offset,
unchanged 1:n anchors, policy/lock rejection, SQL failures after publication and
during placement switch, reader/GC gates, corruption retention and receipt
failure after unlink. A Unix child-process fixture additionally pauses the real
operator before publication, after publication, after the transactional switch
but before commit, after commit, before unlink, after unlink and after the receipt.
The parent sends SIGKILL, reaps its exact child and waits for that child's database
connection to drain before checking atomic placements, every body, stable 1:n
anchors, reader retention and idempotent retry. The hooks exist only in test
executables. Before-publication staging is identified and recovery refuses the
live child writer. After SIGKILL and reaping, the fixture cleans that exact build
nonce through the exclusive-lease API and checks an idempotent retry; published
body bytes and stable anchors are verified afterward. The fixture owner removes
its complete disposable root only after its children are gone.
Hosted CI requires all seven crash-stage markers, not merely a zero exit code.
This qualifies application-process interruption on the tested filesystem, not
PostgreSQL-server loss, machine power loss or network filesystem durability.
Representative throughput and peak-RSS comparisons, production orphan recovery
and tuned policy selection remain open in #58. No
network-filesystem, out-of-band administrator mutation or production deployment
qualification follows. Unregistered published packs and unrelated staging remain
retained for explicit, quiescence-proven recovery; no automatic sweep occurs.

Verified-body staging takes per-file cleanup ownership immediately after exclusive
creation. Decode, logical-integrity and file-sync errors, as well as Rust panic
unwinding, close and best-effort remove only that invocation's temporary body;
successful verification retains the body until its owner is dropped. A focused
fault-injection test checks sync failure, unwind, digest/length failure and the
successful lifetime while preserving a neighboring file and the published pack.
This is not SIGKILL cleanup: process termination bypasses destructors, and a
filesystem refusing removal still requires explicit quiescent recovery.

Pack publication creates the final name with an atomic no-clobber hard link from
the already sealed candidate on the same filesystem, syncs the pack root, then
removes the candidate name. There is no existence-check/rename race: an existing
directory entry, including a dangling symlink, makes publication fail rather
than replace it. Filesystems without hard-link support fail closed. A crash may
leave both names referring to the same complete file; deleting a stale candidate
name must not be interpreted as reclaiming the published pack's bytes. Tests
race two different candidates for one pack identity and require exactly one
winner with unchanged bytes, plus preservation of a dangling destination link.

### Explicit incomplete-build recovery

`cleanup_incomplete_build(root, build_nonce, max_files)` operates only on the named
`.building/<build_nonce>` directory. Builders acquire a shared, permanent-inode
root lease before creating any build directory and retain it through sealing,
publication and candidate verification lifetimes. Cleanup requires an exclusive
nonblocking lease: an active participating writer makes it fail without unlinking.
The root must be operator-owned and all writers must use this protocol; older
binaries, out-of-band filesystem administrators and network filesystems require
external quiescence and are not qualified by the lease alone. Lock files must not
be removed while the root is in use.

Before unlinking, recovery bounds the entire inventory (1–65,536 files), requires
regular files with recognized candidate/raw/verified-body names, and rejects
symlinks, nested directories and unknown entries. It removes no published pack,
changes no database state and never recurses. Completion syncs the build parent;
retry of an absent nonce is successful. An I/O interruption can leave a partially
cleaned build for a later retry. The report counts removed names and their logical
file lengths, not allocated or reclaimed device bytes: a candidate may still
share bytes with a published hard link. Unit tests cover active writers, invalid
inventory, bounded preflight, preserved published aliases and retry; hosted real
SIGKILL coverage proves cross-process exclusion and recovery after writer death.
This API is not authorization to clean a live production root or old generations.

### Physical pack resource diagnostics

The ignored `services::content_store::resource_tests::pack_resource_matrix`
test runs 48 serial fresh-process measurements on Linux: three repetitions of
two size cohorts (4 KiB and 256 KiB plus 1 MiB or 16 MiB), two deterministic
patterns (repeated or pseudorandom bytes), identity/zstd output and 4/64 KiB
buffers. Codec/buffer order alternates between repetitions. Input is streamed;
the source pack always uses identity encoding for comparable rewrite work.

Build, physical rewrite and final verification have separate wall clocks. Every
replacement identity and digest is verified. `/proc/self/status` VmHWM supplies
whole-process lifetime peak RSS, with a 128 MiB fixture gate; it is not the writer
buffer or a per-stage incremental peak. File lengths are not device allocation.
SQL, ingestion, CPU attribution and device I/O remain unmeasured here.

`eval/storage_v2/pack_resource_report.py` requires all 48 cells, valid integrity,
finite measurements and an exact source revision. It exports individual values,
median/range/noise and the existing telemetry viewer summary structure. Output
creation is exclusive: existing summaries are never overwritten. Public metric
labels explicitly distinguish RSS, file bytes, clocks and failed integrity.
Use `--cohort random-size16777216` (or the matching pattern/size) to export only
four comparable settings for the existing five-column viewer. The complete
48-run matrix is validated before this display filter is applied.
CI retains the validated JSON as `pack-resource-summary`; raw private logs are
not uploaded. Compare only equal size/pattern cohorts and build profiles.
These debug/CI diagnostics do not choose production defaults. Optimized-build
measurements, larger real distributions, DB maintenance latency, concurrent
resource contention and tuned policy qualification remain separate work.

### Integrated maintenance resource diagnostics

The real PostgreSQL/file fixture also runs six fresh-process operator probes:
three repetitions each at 4/64 KiB buffers, with alternating order. Fixture setup
is outside the child measurement. Each case moves two repeated-byte bodies
(64 KiB and 1 MiB), excludes a 128 KiB entry already assigned elsewhere, preserves
eight anchors and verifies every body after completion. Generated byte values
differ between cases to keep immutable identities distinct in the shared fixture.

`pack_maintenance.repack_ms` covers the complete `repack` API, including admission,
file work, SQL and commit; `finish_ms` covers verification, reader/GC checks,
unlink and receipt. Neither is SQL-only latency. Client lifetime VmHWM excludes
fixture construction but is not PostgreSQL server memory. The child has a
128 MiB RSS gate. Dead entry bytes and receipted whole-file bytes are checked
separately, including an idempotent completion retry by the parent.

`eval/storage_v2/maintenance_resource_report.py` rejects missing/duplicate cells,
invalid observations, inconsistent accounting, extra public fields and a failed
parent test. CI retains the six-run JSON as `maintenance-resource-summary` in
the same viewer-compatible structure, with explicit integrated-operator labels.
This is a synthetic baseline, not production tuning, database-server resource
attribution, device-I/O measurement or a quiescent orphan-cleanup implementation.

## Lossless content graph

`content_node` is an immutable DAG node. A node digest covers:

```text
domain, digest schema version, node type, logical length,
leaf content identity when the node is a leaf,
ordered sequence of (edge type, child kind, child digest)
```

Only leaf nodes may own a body. Internal nodes own ordered typed edges. Parent
identity, source identity, external role, location, and database IDs MUST NOT be
part of the node digest. The ordering rule prevents distinct structures such as
`A,B` and `B,A` from collapsing.

Large child sequences MAY use a canonical packed representation, but the packed
form must decode to the same ordered edge sequence and therefore the same node
identity. Frequently queried relationships MAY also have normalized edge rows;
those rows are projections, not competing truth.

Analysis is keyed by `(content_body, analysis_profile)`, not by file or source.
This permits byte-identical content to reuse parser output while source-specific
symbol resolution and authorization remain occurrence-bound.

## Retrieval views and occurrences

### Retrieval-view identity

A retrieval view is an immutable composed search unit. Its `view_digest` is
unique over:

```text
view type, profile ID, language ID, tokenizer version, capability flags,
ordered sequence of (role, component digest, relative span)
```

Each component references exactly one content body or content node. The digest
includes order, role, relative range, and interpretation profile; views with the
same bytes but different semantics MUST NOT collide.

Views are globally deduplicated and have no `parent_view_id`. Parent/child
relationships belong to separate typed edges because the same view may have
multiple parents. Source, path, authorization, time, and absolute position do
not belong to a global view.

Structural boundaries depend only on content, adapter/parser profile, and
version. Corpus frequency MUST NOT change elementary decomposition. Frequency
may create additional composed views over existing components, such as bounded
sibling windows, without changing component identity.

### Occurrence-bound location and authorization

An occurrence binds a retrieval view to one artifact version and carries its
role, ordinal, parent occurrence, and typed locator. Locator forms include byte
and line spans, structured paths, page/block/message identifiers, embedded-text
position maps, and derivation recipes for content that has no physical source
location.

Authorization and source/time filtering resolve in this order:

```text
request principal and source scope
  -> visible source generation and item membership
  -> artifact version
  -> visible occurrence
  -> retrieval view and content
```

The system MUST NOT retrieve a global body or view first and infer visibility
from another occurrence afterward. Every returned result identifies one visible
occurrence as its primary location. Additional occurrences are returned only
after independent authorization checks.

Source-specific symbol occurrences and call sites also attach to artifact
versions/occurrences. A byte-identical function in two sources may reuse content
analysis but resolve an identical callee name to different symbol identities.

## Stable symbols and intelligence provenance

A stable symbol identity is source-bound and includes language, container,
qualified name, kind, and a normalized signature discriminator. A symbol
occurrence supplies the artifact version, content node, exact position,
signature, and visibility for one version.

Renames and moves create explicit succession edges with confidence and evidence;
they do not silently equate two identities. Call edges originate at symbol
occurrences and resolve to a source-bound symbol identity or remain explicitly
unresolved.

Intelligence fields have two classes:

- generic facts derived from parser-visible structure; and
- domain fields derived from an explicit source-bound analysis profile.

Every derived field records at least its stable subject identity, input
body/node digest, analysis-profile ID, rule/generator version, value state, and
derivation evidence class. Missing or inapplicable information remains `unknown`
or `unavailable`; it MUST NOT be replaced by a plausible default.

Reprocessing under changed rules creates new provenance instead of rewriting the
meaning of an old result. Protected curated evidence, including negative
evidence, migrates through stable symbol identities with an export hash and a
tested import path. Candidate acceptance measures coverage and quality by source
kind and exercises every supported intelligence command before activation.

## Legacy external hit compatibility

Existing external chunk IDs remain durable values after legacy chunk rows are
removed. They resolve through an ordered mapping:

```text
old chunk ID
  -> one or more legacy-hit mapping rows
  -> occurrence
  -> retrieval view
  -> content body or content node
```

The mapping supports:

- `exact`: one old hit maps to one occurrence;
- `split`: one old hit maps to multiple occurrences; and
- `merged`: multiple old hits may map to the same occurrence.

Mapping identity includes `(old_chunk_id, occurrence_id)` and each old ID has a
unique ordinal ordering. For callers that require one target, ordinal zero is
chosen deterministically by greatest byte overlap, then smallest source offset,
then stable occurrence identity. The response marks split/merged resolution and
can return the complete ordered target list.

The mapping deliberately has no destructive foreign key to the legacy chunk
table. It is retained through legacy cleanup and remains subject to occurrence
authorization; possession of an old ID does not bypass source visibility.

The authenticated `POST /api/v1/legacy-hits/resolve` surface accepts a source,
an explicit generation sequence or `active`, and an old hit ID. The CLI exposes
the same operation as `mainrag resolve-hit OLD_ID --source SOURCE --generation N`.
Resolution first returns targets visible in the selected generation. If none
remain, it can return mapped occurrences covered by an earlier retained verified
generation, labeled `retained_history` with their own generation identities.
Unverified, abandoned, future, and unauthorized roots are excluded. The primary
ordinal is the smallest returned mapping ordinal; a filtered split need not
retain ordinal zero. An unknown ID returns an explicit unresolved envelope.

An old hit absent from every retained generation can instead have a dedicated
native history root. `storage_v2_preserve_legacy_hit` checks the administrator's
source access, native body digest and length, publication state, complete old
chunk/file identity, and expected mapping hash. It creates the immutable native
artifact, occurrence, proof record, and mapping in one transaction. Identical
retries reuse the root; changed proof identities or stale mapping hashes fail.
These roots allocate no source generation, membership interval, or active pointer.
They resolve as `retained_legacy_hit` with a null target generation, never as a
current source hit. No legacy table is needed for their subsequent resolution.

The byte-alignment producer core decodes and hashes original legacy chunks,
checks the optional text projection, and scans a complete verified native file
for a bounded batch of patterns in one pass. It supports overlapping patterns
and fragment/window boundaries using UTF-8 byte coordinates. Only a unique exact
position with complete, gap-free fragment coverage yields native overlap targets.
Empty, absent, or ambiguous patterns require preserved history. A corrupt digest,
truncated file, inconsistent projection, or range gap produces no accepted plan.
The administrator producer endpoint processes one bounded batch of a named
verified generation and legacy file. The request binds the expected legacy file
SHA-256, file revision, global legacy mutation epoch, hit cursor, test scope and
admitted native source spool budget. The source writer and legacy snapshot locks
bind that inventory while the batch executes. Original
legacy chunks are decoded and verified before any mapping is applied. Existing
historical bodies are reused only after the native pack reader verifies them;
missing unique bodies use one verified pack per batch. Native mappings, history
roots and completion proofs commit together. Unknown commit outcomes are resolved
by observing the live source-writer lease before retrying the same bounded
request: committed proofs are reused, never inferred
from a local progress counter. Published packs are never deleted on an HTTP error.

A private source spool under the pack root caches at most one decoded native file
per source across its batches. Its key binds source, generation and the complete
ordered native body/range identities; every reuse checks the complete byte digest.
The producer removes its two owned spool files when that file is finished. An
interrupted file retains this owned cache until resume or explicit job cleanup.
This budget is independent of the chunk batch budget and requires caller resource
admission before a production run.

The inventory endpoint pages legacy files and returns exact hit/completion counts
for the named generation. Mapping mutations invalidate completion proofs in the
same transaction, so inventory cannot count a changed mapping as complete. Native
history rows are independent GC roots even if their mapping is later replaced.
Retire the bootstrap inventory/producer readers and snapshot-lock functions before
legacy table removal. Actual all-hit coverage, end-to-end producer validation and
frozen-package acceptance are still required before cleanup. The serial operator
freezes source/generation inventories and the installed reader package, records
committed cursors durably, checks physical capacity before each batch, and
rechecks every source's coverage at the end. Its completion receipt proves that
plan's old-hit coverage; it does not activate generations or authorize deletion.

Administrator mapping batches are source scoped, bounded to 512 old IDs and
2,048 total targets, and require the observed mapping hash for every old ID.
They share ordered transaction locks with single-hit replacement, reject drift
atomically, and cannot move an existing old ID to another source. These surfaces
read native mappings and roots, so resolution does not require legacy chunk or
file tables. They do not automatically construct mappings, prove source byte
overlaps, or authorize legacy deletion; the compatibility inventory and its
source-backed mapping producer must establish those before cleanup.

## Exact retrieval contract

Storage v2 indexes unique content/search documents and relates their postings to
composed retrieval views. A query whose terms span multiple components must be
able to find the composed view even when no single component satisfies the full
query.

The query planner operates on an explicit AST for AND, OR, NOT, phrase, grouping,
and exact identifiers:

- AND may seed from a selective branch and test remaining branches on that
  candidate set.
- Every OR branch contributes candidates; branches are unioned.
- NOT filters a positively established candidate set and never creates an
  unbounded universe by itself.
- A phrase cannot cross a component boundary unless a separately indexed view
  explicitly materializes that byte adjacency.
- Exact identifiers use a typed exact channel rather than lossy natural-language
  tokenization.

For a term, a view counts its best weighted component contribution once. View
length normalization uses the complete view length. Role weights and every later
boost that can affect ordering are part of the scoring/upper-bound contract.

Candidate pruning is allowed only with a proven monotone upper bound such as a
validated WAND/MaxScore plan. A fixed per-term or per-channel candidate cap is
not correctness-preserving. Where no safe bound covers role weights, graph
expansion, fusion, or reranking, execution MUST use a complete path.

Qualification compares Top-K exactly with a complete reference evaluator,
including deterministic tie-breaking by external hit identity. It records
evaluated candidates separately from returned result limits and captures the
actual SQL plan. The prototype decides whether a backend can preserve exact
composed-view Top-K; this architecture does not preselect that backend.

The additive implementation selected by the prototype is compiled with the
`storage-v2-retrieval` API feature. It uses native PostgreSQL GIN materialization
and complete scoped-view evaluation; no unsafe candidate cap is enabled. The API
and CLI select it only when `read_path=storage_v2`, a source, and a positive named
generation sequence are supplied. For example:

```bash
mainrag search 'alpha AND "beta gamma" NOT decoy' \
  --source synthetic-source \
  --read-path storage_v2 \
  --generation 1
```

Omitting the selector keeps the current path. Storage-v2 generation and filter
arguments are rejected on the current path rather than silently ignored.

### Fragmented artifact result groups

Native filesystem inputs can represent one physical file with several immutable
artifact occurrences. The adapter marks these occurrences with the Boolean
locator field `fragmented=true`. Ordinary storage-v2 search returns the best
matching fragment per authorized `(source_id, source_path)` group, alongside
individual unfragmented occurrences. Only occurrences with `role=artifact` and
that Boolean flag participate. Conversation and other logical records remain
separate even when they share a path or carry the flag.

This is an explicit result-unit policy for fragmented artifacts. It does not
claim unchanged occurrence Top-K for those inputs. Each group retains its best
fully scored matching occurrence, ordered by final score, precise segment tie
key, stable external-hit identity and occurrence identity. The retained hit keeps
its original content, locator, score, explanation and successor mappings; no
new or synthetic hit identity is introduced.

The complete evaluator still scores every authorized matching view, including
all later graph, semantic and rerank contributions. The score boundary is drawn
from the complete result-group population, not a fixed oversampling window.
Every occurrence at that boundary remains available for exact tie resolution
before selecting the best fragment. Ordinary inputs preserve the complete
response. `total` counts matching occurrences and `fully_scored_views` counts
all evaluated scoped views; grouping changes returned result slots, not these
work and coverage counters.

The exhaustive reference first orders all matching occurrences, retains the
first occurrence of each fragmented-artifact group, keeps other records
separate, and then takes Top-K. Tests compare complete responses against that
reference for named and active reads, including ties, ordinary same-path
artifacts and same-path conversation records. Frozen quality expectations are
not changed to accommodate this policy.

### Bounded native rank work

Native lexical matching establishes query membership without calculating every
segment rank. The complete view score still determines the score boundary.
Precise native segment ranks are restored for every native occurrence at that
boundary, including all ties, before the existing external-hit ordering.
Copied projection ranks and all returned score explanations are preserved.

Copied canonical-document verification uses only authorized canonical document
identities. Separately materialized provenance branches share the copied query
projection so forced-RLS reads are not repeated per projection row. Migration
replay accepts only exact predecessor or successor definitions and rejects
unexpected helper bodies, owners, executor grants and fragment-index identity.

### Unavailable code grammars

A recognized filename does not establish that a compatible grammar is
registered. Candidate ingestion preserves the complete body, search text and
locator for such inputs while recording `parser_availability.status=unavailable`
in the analysis cache. It does not fabricate symbols or calls or count this as
a parser pass. The custom JSONL parser remains available independently of
tree-sitter registration. Errors from a registered parser still fail explicitly.
Checkpoint resume exercises an unavailable Dockerfile grammar after a durable
prefix and verifies the same generation, exact bytes and unknown intelligence.

## Migration and authority phases

Storage v2 is additive until cleanup. The required phase order is:

1. publish architecture and migration contracts;
2. freeze a reproducible current-state baseline and validation harness;
3. prove exact composed Top-K and select a viable search design;
4. add generation/activation DDL and invariant tests;
5. implement bodies, packs, lossless graphs, views, mappings, ingestion,
   intelligence, and retrieval;
6. qualify and reproducibly package the selected search backend;
7. build a complete shadow slice and compare legacy/new reads;
8. prepare database and maintenance gates;
9. build and verify a release candidate for every source without changing
   active pointers;
10. derive final source-local deltas and atomically activate the complete
    candidate set under fresh activation authority;
11. verify the first ordinary post-activation ingest while retaining all legacy
    state; and
12. remove legacy runtime/data and reclaim unreachable storage only under a
    separate exact destructive-cleanup approval.

Build, seal, verify, release-candidate transition, activation, deployment,
cleanup, and release are separate authorities. An implementation PR author does
not acquire any later authority. A failed or missing gate leaves active/default
reads unchanged.

### Database preparation interface

[`ops/storage-v2/preflight.py`](../ops/storage-v2/preflight.py) is the
non-mutating boundary for database and maintenance preparation. Its redacted
manifest binds PostgreSQL/client/backend versions, schema and repository
configuration hashes, extension and preload state, collation/index state,
writer/timer activity, backup evidence level, and resource headroom. Missing or
drifted evidence is `BLOCKED`; backup-command evidence is never presented as a
restore or recovery test.

Live preparation uses
[`ops/storage-v2/apply-gate.py`](../ops/storage-v2/apply-gate.py). It invokes at
most one separately reviewed adapter and only when the exact gate, checked
manifest digest, adapter digest, approval string, and fresh live-state digest
match. It then requires immediate post-readback and rejects regression of any
previous PASS. The coordinator does not itself grant upgrade, service, reindex,
package, activation, deployment, or cleanup authority.

## Bounded shadow-slice interface

The `storage-v2-retrieval` feature exposes only explicit, named-generation
shadow operations. A permanent public fixture source is created with
`is_test=true`; legacy sync and watch entry points reject that source. Search,
source-state, card, explain, layers, and ownership reads require both the exact
generation sequence and the admin-only `include_test` scope. Neither the current
selector nor an omitted selector can infer the fixture generation.

The shadow writer uses the real filesystem adapter, verified pack files under
`MAINRAG_STORAGE_V2_PACK_ROOT`, content nodes/views, analysis cache,
intelligence records, exact lexical documents, membership intervals, sealing
and verification. A repeated semantic manifest reuses the verified generation;
a delta verifies existing packed bytes before reusing a body. Optional graph,
semantic, and rerank stages remain explicitly `unavailable`, not silently zero.

Dual-read evidence is submitted through the supported admin API after both
search APIs have returned. The server recomputes query-set identity, binds the
artifact to the verified generation witness and its recorded production or
explicit-test scope, classifies every difference into the closed taxonomy, and
rejects unexplained differences. Abandoned test-only
building runs may be cancelled and marked with an unreadable lifecycle
tombstone; immutable staging rows remain as audit evidence and no membership or
active pointer is changed.

## Source release-candidate interface

The same feature provides a distinct production build surface for #66. It
accepts any registered source adapter, captures a canonical source watermark,
builds and verifies one immutable generation, and leaves qualification separate.
The production path does not inject the fixture's controlled parser retry and
does not infer or update an active generation.

Filesystem candidate discovery is metadata-only. Candidate construction reads
one item at a time, captures its content identity in the canonical watermark,
and verifies that identity on every later content-store, reconstruction, and
analysis pass. Accepted filesystem text files above one MiB are represented as
deterministic, contiguous, UTF-8-aligned byte ranges of no more than one MiB
plus three UTF-8 continuation bytes. Where possible, the boundary moves
backward within a bounded 64-KiB window to retain complete lines. The ranges
retain the original source path, cover
every byte exactly once, and receive stable source-item keys that include their
byte boundaries. A gap, overlap, mixed whole-file/fragment layout, truncated
read, or changed physical length fails closed. Byte offsets remain exact;
fragment locators deliberately leave global line numbers unknown instead of
reporting fragment-local lines as source-global locations.

Immediately before sealing, candidate construction rediscovers and rehashes
the complete source so additions, removals, and late changes fail the run. This
bounds filesystem source-content memory by the largest accepted unfragmented
file or one fragment, plus pack/reconstruction buffers; drift after watermark
capture never becomes a mixed candidate. The filesystem adapter profile binds
the fragment size, and telemetry reports both the fragment count and largest
item size. The production watermark is domain-separated over source type,
registered path, adapter profile, and the content manifest, so a source
configuration recut cannot silently reuse an older candidate. Adapters that
cannot provide a lazy source path remain bounded by their own complete adapter
result and must pass the per-source resource gate before qualification.

The qualification surface accepts protected evidence only after supported
current and named-generation reads have produced accepted dual-read evidence.
The same evidence endpoint accepts registered production candidates and
explicitly scoped test candidates; it never changes the default read selector.
One transaction records an opaque evidence UUID and manifest hash, rechecks
source/generation ownership, watermark and profile identity, item/membership
reconciliation, complete analysis, resource and quality gates, and active-pointer
stability, then transitions `verified` to `release_candidate`. A partial unique
index permits at most one current release candidate per logical source.

Completed sources are restart/resume checkpoints: rerunning the same semantic
snapshot reuses the verified or release-candidate generation and immutable
content. Other sources can continue after one source fails, but aggregate
activation remains blocked until every in-scope source has exactly one accepted
candidate.

## Evidence and privacy contract

Public evidence may contain repository commits, schema/package/profile versions,
opaque fixture IDs, hashes, aggregate counts, timing distributions, query plans
over synthetic data, and pass/fail states. It MUST NOT contain private source
bytes, paths, account identities, infrastructure identifiers, addresses,
credentials, database dumps, or raw private logs.

Every acceptance record binds to the exact code commit, schema identity,
candidate generation manifest, analysis/search profile, fixture/corpus manifest,
and command version. A self-referential manifest cannot contain its own future
commit; the enclosing acceptance record binds manifest hash to commit.

Temporary clusters, packs, fixtures, processes, and branches require an owner and
cleanup point. Cleanup evidence states exactly what was removed and whether it is
recoverable. An absent current object is not proof that historical or external
copies were erased.

## Child dependency map

The parent epic tracks authoritative issue state. This map describes intended
semantic ordering, not completion evidence.

```text
#69 governance and worker bootstrap
  -> #54 architecture contracts
  -> #55 baseline and validation harness
  -> #56 exact composite Top-K prototype
       -> #57 generation and activation DDL
       -> #63 selected search-backend qualification

#57 -> #58 content bodies and packs
#57 + #58 -> #59 lossless graph, retrieval views, stable hit mappings
#57 + #58 + #59 -> #60 generation-aware ingestion
#59 + #60 -> #61 intelligence regeneration
#56 + #59 + #60 -> #62 production retrieval path
#55 + #57..#63 -> #64 complete shadow slice and dual read
#55 + #63 + #64 -> #65 database and maintenance preparation
#64 + #65 -> #66 verified release candidates for every source
#66 -> #67 final deltas and atomic activation
#67 -> #68 separately approved legacy cleanup
```

## Parent decision mapping

Every binding architecture decision in the parent epic has a normative home:

| Parent decision | Normative section |
| --- | --- |
| One active immutable generation per source; source-local monotonic sequence | [Source generations and membership](#source-generations-and-membership) |
| Membership intervals belong to item/version membership and avoid full copies | [Source-local sequence](#source-local-sequence) |
| Exactly one structured-root or raw-body artifact anchor | [Artifact witnesses, anchors, and exact reconstruction](#artifact-witnesses-anchors-and-exact-reconstruction) |
| Content-addressed inline/pack storage with integrity checks | [Content bodies, collision handling, and packs](#content-bodies-collision-handling-and-packs) |
| Globally deduplicated views use an ordered typed component digest | [Retrieval-view identity](#retrieval-view-identity) |
| Source, location, authorization, and time bind to occurrences | [Occurrence-bound location and authorization](#occurrence-bound-location-and-authorization) |
| Legacy hit mappings persist and support ordered split/merge resolution | [Legacy external hit compatibility](#legacy-external-hit-compatibility) |
| Unsafe pruning falls back to complete retrieval | [Exact retrieval contract](#exact-retrieval-contract) |
| Generic and profile-derived intelligence retain field provenance | [Stable symbols and intelligence provenance](#stable-symbols-and-intelligence-provenance) |
| Shadow reads name candidates; activation does not imply cleanup | [Migration and authority phases](#migration-and-authority-phases) |

## Implementation references

Later issues must refresh these current paths before mutation:

- [`schema.sql`](../schema.sql): current mutable source/file/chunk schema and
  outbox contracts;
- [`api/src/services/index.rs`](../api/src/services/index.rs): current discovery,
  parsing, chunking, persistence, embedding, and outbox coordination;
- [`api/src/services/search.rs`](../api/src/services/search.rs): current FTS,
  semantic, fusion, filtering, and result formatting;
- [`api/src/services/intelligence.rs`](../api/src/services/intelligence.rs):
  current intelligence derivation and persistence;
- [`ops/migration/README.md`](../ops/migration/README.md): current migration
  operating boundary; and
- [`architecture.md`](architecture.md): supported current architecture until
  accepted activation.

## Acceptance summary

Storage v2 cannot become active unless all of the following are true for the
exact candidate set:

- every artifact reconstructs byte-for-byte from its declared anchor;
- membership intervals and source-bound references satisfy all invariants;
- content/view collision checks and pack integrity pass;
- retrieval Top-K equals the complete reference result for the accepted corpus;
- occurrence-bound authorization and source isolation pass negative tests;
- legacy hit mappings resolve exact, split, and merged cases deterministically;
- intelligence provenance, protected evidence migration, coverage, and command
  behavior pass their gates;
- every source has a verified release candidate and current final delta;
- resource/maintenance gates pass; and
- the complete activation transaction commits and its post-commit readback plus
  first regular ingest succeed.

Legacy data remains intact after activation until the separately approved
cleanup issue proves that no supported reader/writer depends on it.

### Candidate staging and progress

The candidate writer groups node/view/item/search binding and unavailable scores
in one dependent statement. Structural cards are submitted in groups of at most
64 through the existing immutable, authorized scalar function. Migration 083
computes each bounded lexical group's first-match location, hash and weighted
vector once and preserves the installed compatibility-rank copy hook. The
character chunker counts UTF-8 boundaries and newlines in one pass; content,
overlap and line semantics remain unchanged, and byte locators use actual bytes.

An optional candidate-build `progress_id` selects a private, create-only attempt
record under the configured pack root. The administrative progress endpoint
requires the registered source and exact attempt/commit. Counts represent staged
work in an uncommitted transaction. Only the handler's successful commit readback
marks the record committed; an interrupted/restarted reader requires witness
reconciliation. The operator saves the attempt identity and observations beside
its checkpoint every 30 seconds and drains its owned POST before reporting a
monitor identity failure. The source batch exposes this cursor separately from
completed items and never restarts a writer because progress is unavailable.

Migration 084 materializes generated/copied provenance before joining segments.
The lexical read policy retains FORCE RLS and the existing write check, while
computing the authorized visible source set once per scan. Explicit source checks
and immutable-document/rank semantics remain required for both provenance paths.

Migration 092 probes each query term once through the existing term index and
intersects those postings with authorized corpus bindings before computing
document frequency or scores. Both the original term and its digest remain
required. Segment presence returns one authorized occurrence if either its
copied projection or its correctly bound generated segments exist. It does not
enumerate every segment. Complete result envelopes, full corpus statistics,
rank precision, ties, function ownership, grants and configuration are unchanged.
Definition guards reject unexpected function bodies and permit exact replay.

Migration 093 bounds each global term probe to 4,097 matching postings. A probe
with at most 4,096 rows is complete. An overflowing probe is discarded for
scoring; the full posting set is then read through the primary index for every
distinct document in the authorized corpus. No result set is truncated. This
retains the rare-term benefit while preventing common terms in other sources
from dominating a small source's search cost. Both branches keep the term/digest
checks and intersect with corpus bindings before document frequency and scoring.

A lost build checkpoint can be reconstructed read-only for a sealed verified or
release-candidate generation, including the PDF adapter. This retains the
original build commit and fixture identity, requires an exact live watermark
and adapter match, and proves that the API restarted after the original build.
Reconstruction does not qualify a candidate: frozen gold, source drift,
integrity, latency and current package qualification still run normally. The
durable source batch accepts PDF sources as well as filesystem, Git and managed
append sources without changing their phase or result acceptance checks.

Both PDF backends read one bounded source snapshot through application read
accounting before parsing. The native MuPDF parser receives memory bytes rather
than reopening the source file. Its structured page ranges, chunk content and
path hashes retain the existing native format. Storage-v2 discovery returns the
actual source bytes consumed; the final watermark scan contributes another
measured read. These are application bytes, not physical device I/O. The native
feature and composed watermark accounting are covered by required hosted CI.
Switching from the fallback backend to MuPDF changes the existing backend-bound
adapter profile and requires fresh PDF candidates; old candidates and their
original profile evidence remain historical.

Migration 085 validates bounded groups at explicit character locators in one
source window. Rust retains canonical first-match positions using byte searches
and one shared UTF-8 walk. The independent SQL check compares each exact segment
against the immutable document, retaining all hash/vector/collision checks. The
previous writer remains available when a canonical group spans over eight million
characters. Existing immutable segment identities remain compatible.

## Explicit filesystem cuts and complete-file proof

An explicitly configured `btrfs-cut-v1` filesystem source retains its registered
root, relative item identities and exact file filters. Its release profile is
`mainrag.fs-release-candidate.v4.btrfs-cut-v1.scope-<digest-or-unfiltered>.fragment-1048576-newline-65536`.
The privileged producer accepts only an allowlisted root digest. Its inspection
mode additionally accepts an opaque cut UUID, never a caller-selected path or
policy. Unprivileged readers use that fixed helper for the kernel inspection and
independently verify root-owned immutable descriptor history, actual snapshot
and origin UUIDs, and the kernel read-only property. Service write access is
restricted to the owned cut registry. A mutable current descriptor cannot redirect an
ongoing read. Nonrecursive snapshots with nested source subvolumes are rejected.
Nontext conversation files are rejected rather than silently omitted.

An immutable conversation source may explicitly register
`conversation_text_projection: "utf8-nul-space-v1"`. This keeps the registered
root, filters, fragmentation and original body bytes. Its adapter profile adds
`.text-utf8-nul-space-v1`; other source profiles remain unchanged. Valid UTF-8
JSON/JSONL bodies containing NUL are retained byte exactly, while searchable
text substitutes one ASCII space per NUL. Byte and character offsets are
unchanged. Each affected locator records the projection kind, NUL count,
original body digest and projected text digest. Binary signatures and invalid
UTF-8 still fail. Other extensions and legacy ingestion retain their previous
binary policy. Original malformed conversation fragments report unavailable
parser intelligence explicitly; projected JSON is never treated as repaired
parser input. Analysis uses a separate projection-bound profile to avoid
reusing analysis under a different text contract. Config/profile changes require
the existing reviewed configuration transition and full frozen source proof.

The producer prepares reader permissions only in a root-private unpublished
snapshot inside a private container. Its closed policy binds the reader identity and exact compiled file
scope. It grants directory traversal and read access to selected regular files,
rejects selected hard-link aliases, then seals the snapshot read-only before
publishing the container. The snapshot keeps its original Btrfs parent identity;
the reader also accepts historical flat snapshot descriptors. Original source
permissions and bytes remain unchanged. Failed
cuts retain their exact pending and published ownership paths for manifest
cleanup. The policy uses `mainrag.fs-cut-policy.v2`; descriptor and source
manifest formats remain unchanged. Snapshot metadata and ownership journals are
synced before publishing the current descriptor.

The administrative configuration transition uses root and configuration digest
comparison, preserves every other configuration value, and rejects a profile
change on an active source. Source review reads the same pinned cut independently.
The full fixture digest, item count, byte count, registered-root digest and source
watermark bind that review to the build witness and aggregate qualification.
Different captures of identical complete source bytes may reuse an immutable
build; the original build cut and commit remain preserved in the evidence.

Changed fragmented files need more than one fragment hash. A requested complete
file proof verifies each stored body under a pack reader epoch, checks contiguous
byte ranges, and hashes their ordered decoded bytes with bounded row and I/O
buffers. The resulting whole-file digest must match the independently frozen
source review. Query support still requires a matching returned body and lexical
segment. Qualification caches this file proof within its attempt. A missing,
reordered, overlapping, corrupted or foreign-generation proof fails acceptance.

Legacy compatibility ranks use a double precision tier so close original REAL
FTS ranks remain distinguishable. The historical REAL function stays available.
Query matches are gathered once within authorized requested sources, with narrow
rank tuples; immutable hit identities are hydrated only at the exact score and
tie-key boundary. Corpus normalization, complete scoped evaluation, authorization,
final total order and latency limits remain unchanged.

Immutable lexical fallback gathers matching requested occurrences once through
query and authorized source predicates. A dedicated non-login definer has a
narrow SELECT policy for the lexical GIN lookup; ordinary policies and forced
RLS remain enabled. Its private caller binds requested occurrence membership,
source/artifact identity and immutable root-document checks. Matching rows
materialize identities and scalar ranks rather than complete vectors. Both
rank surfaces preserve copied/generated tiers, precision and complete tie order.
The role, policy and source index identities are validated on migration replay.

Exact and active reads evaluate complete Boolean evidence before hydrating
matching occurrences. Complete corpus normalization and numeric term scores
remain unchanged; detailed score explanations are serialized only for returned
rows. Large scoped posting queries choose a term-index membership plan while
small scopes retain document-key probes. Plan selection does not truncate
postings or change the logical corpus.

New search documents store exact postings in immutable blocks of at most 256
terms. Each block keeps complete term strings and BIGINT frequencies in
canonical byte order. A GIN index on 16-bit fingerprints only selects candidate
blocks: every match must also pass the complete term comparison. Oversized
terms and fingerprint collisions therefore preserve their exact meaning.
Existing flat documents retain their identities and postings. Named search,
active search and candidate query evidence share complete mixed-layout reads,
including the scoped fallback when the bounded global probe overflows.

New searchable text, exact identifiers, FTS vectors and posting arrays use LZ4
column compression. This does not rewrite existing immutable values or establish
physical reclamation. After compact documents have been written, a binary
rollback must retain compatible mixed-layout readers; removing their blocks or
restoring flat-only readers would discard required logical postings. Data
removal remains subject to the governing manifest and integrity gates.

Managed prefix reuse locks its trusted frontier through a source-authorized
function owned by the dedicated non-login frontier owner. The prefix copier
keeps its ordinary ingest ownership and all generation, prefix, analysis and
immutable reuse checks. Direct frontier changes remain forbidden to the API
role; the lock helper does not publish or modify a frontier.

Interactive exact and active reads materialize the shared narrow corpus binding
once and disable JIT only inside those two functions. Full corpus normalization,
query classes, authorization and complete result envelopes remain unchanged.

Regular active ingest captures a fresh cut after checking the complete active
set. Legacy incremental filesystem ingest cannot substitute for this contract.
Cuts, failed capture intents and original descriptors remain owned until their
explicit manifest cleanup. This feature alone does not establish production
qualification, activation, recovery or cleanup acceptance.


### Active source inspection and runtime retirement

Migration 109 provides source metrics bound to the complete activation receipt.
File counts group physical witness paths, so fragmenting a large conversation
file does not inflate its file count. Input byte counts sum visible artifacts;
view, symbol and resolved/unresolved call counts follow active membership.
Source list/detail, administrative statistics and MCP source inspection use
these metrics after the default switch. They do not use legacy cached counters
or require the former files/chunks/symbols/call_graph table names.
Administrative `chunks` counts represent visible retrieval views under this
read path. The source statistics identify the active path and return a null
legacy Qdrant vector count, rather than presenting an unused backend as zero.

An explicit `current` search selector follows the configured active default.
MCP code search uses the same checked active API retrieval handler. MCP card,
layers, explain and ownership requests use the source-authorized active
intelligence commands and return their source-bound envelope. An unknown or
inaccessible requested source fails; it never becomes an unrestricted search.
Semantic-only MCP search requires an accepted storage-v2 semantic profile.

Migration 110 applies card/layer limits inside the source SQL query before JSON
aggregation. Active requests share one result budget across authorized sources
in source-ID order; exhausted sources keep an empty envelope without reading
another card collection. Stable symbol/profile/item/occurrence ordering makes
bounded prefixes deterministic. MCP defaults are 10 cards and 20 layer results;
layer limits are 1–100, card limits are 1–200. Active HTTP requests default to 100
and accept 1–200. Named-generation commands with no limit and intelligence
exports retain their full collections. An explicit `current` intelligence
selector follows the configured active default. A null optional MCP source is
equivalent to omission; invalid or inaccessible explicit sources do not broaden
the search.

Migration 111 extends the checked commands with bounded `symbols`, `callers`,
and `callees` reads. MCP symbol search and caller/callee inspection use the actor
RLS context and one shared result budget. HTTP symbol search uses the same
active adapter. Call results retain call-site evidence, candidate keys and an
explicit `proven` flag; unresolved names are not promoted to resolved edges.
Active symbol IDs are negative occurrence IDs and active file IDs are negative
source-item IDs, disjoint from positive legacy IDs. Results also expose stable
source-bound symbol keys and generation sequences. These IDs do not establish
legacy symbol-ID compatibility.

Migration 112 resolves active graph and file IDs against the complete receipt,
source authorization and visible membership in one database snapshot. HTTP and
MCP callgraphs retain resolved/unresolved call evidence and expose conservative
completeness flags. File-symbol and card inspection use the same native IDs.
Migration 113 provides bounded incoming/outgoing ownership with one shared
active request budget, actual relation evidence and nullable confidence. These
source-level intelligence relations explicitly identify their metadata scope.

Migration 114 traverses resolved stable identities across multiple call levels,
in either direction, within a single generation-bound snapshot. Each path has
its own visited identities; cycles terminate that path without suppressing
independent branches. Unresolved calls remain terminal evidence even when a
same-named symbol exists. A resolved target without a visible occurrence is
reported without reading an unsealed artifact. Requested depth is 1–10 and the
request-wide fact/work budget is 1–200, default 100. Roots are additionally
bounded to ten per source. SQL applies bounds before aggregation, and exhausted
sources are not traversed. Limits mark completeness conservatively and remain
visible as path termination reasons; reaching a limit never claims a complete
graph. Annotations retain provenance and distinguish unknown confidence from
numeric confidence. HTTP call-chain/path inspection, MCP path explanation and
CLI active/named explanation forward the requested depth. Code snippets are
returned when stored in call-site evidence; absent snippets remain unknown.

Migration 115 provides native HTTP and MCP dead-end notes with authenticated
ownership for global notes and source read/write ACLs for source notes. A
`created_by` display label never grants ownership. Existing source evidence
remains readable with explicit symbol-key provenance. New note IDs use an
odd-negative namespace; source evidence uses even-negative IDs; imported legacy
notes preserve their positive IDs. Administrative import accepts digest-bound,
bounded batches, rejects conflicting identities atomically, and preserves the
entire original record, including unknown fields and nulls. Historical global
notes without provable ownership remain protected for administrators. Full
protected exports are independent of interactive search limits. Installation
does not import, delete, or certify production retention by itself.

Migration 116 orchestrates native Explore in one statement snapshot with bounded
domain expansions, shared card and traversal budgets, exact occurrence roots,
source-scoped dead-end demotion, and visible completeness flags. Stable symbol
keys are not mistaken for display names. Independent call branches remain in
the output; unknown classification confidence is reported as unknown. HTTP,
CLI and MCP results carry the active manifest and read provenance. Formatted
output retains warnings, termination reasons, and partial-result status.

Active startup skips legacy vector collection creation, TEI/reranker probes,
query expansion, and outbox processing/purge. It preserves legacy data for the
accepted activation boundary. Legacy backfills are rejected under the active
runtime. Registry additions and deletion of retained sources require a complete
activation/retention procedure; an ordinary administrative rename remains
available and returns active metrics.

Active health checks use PostgreSQL connectivity and the exact complete-set
receipt; pointer or source-inventory drift degrades health. They do not probe
retired Qdrant/TEI services. Health identifies the active read path separately
from CPU/full process mode, and CLI health renders these backends as disabled.
Active model information returns null for unused legacy embedding/reranker
metadata instead of reporting configured values as serving models.

These routes are part of runtime retirement. They do not establish that every
other intelligence/MCP route is retired, authorize deletion, or demonstrate
production activation, normal ingest, latency, or physical space reclamation.

## Shared legacy rank payloads

Migration 128 factors the immutable native legacy-rank projection into a
source-bound payload dictionary and occurrence/chunk bindings. Fragmented
documents retain every existing chunk association, file witness and vector
weight, while repeated vectors and their GIN entries are stored once per source
and canonical vector digest. The compatibility view retains the original
reader columns and applies the underlying source policies to direct callers.
Controlled readers retain their explicit source and artifact authorization.
The rank evaluator scores each matching dictionary payload once before
expanding requested occurrence bindings, preserving the original rank ties and
canonical-document checks.

The installation requires the expected preceding materializer, one quiescent
writer boundary and an atomic transaction. It compares the complete old and new
relations, including actual vectors, before removing the redundant native
relation. A missing association or different vector aborts the transaction.
Legacy files/chunks, immutable bodies, generations and active pointers are
preserved. The materializer retains its original fragment fallback and replay
semantics. One source advisory fence and two relation locks protect the legacy
snapshot without accumulating a row lock for every chunk; the limited writer
receives no new mutation privilege on legacy files or chunks.

The native regression fixture compares complete search envelopes, weighted
Unicode vectors, source isolation, failed-install rollback and repeated staging
of one hundred fragments. Fixture byte reduction demonstrates the storage
layout only. Production resource admission, latency, complete source acceptance,
activation and legacy retirement remain separate outcomes. SQL installation
can retain the existing application binary because reader signatures and the
logical projection remain compatible.

### Reusing file rank snapshots across fragments

Migration 129 caches the complete legacy chunk ID to native payload ID map at
an authorized file identity and revision. Warm fragment staging reads this map
instead of rereading and hashing every legacy vector. Each hit still validates
the source, file hash and write authorization under the existing relation fence.
Statement transition tables invalidate the affected old and new file IDs on
insert, update or delete, including changes that keep the file hash and row
count unchanged. Truncation changes a global epoch. Cache creation, completeness
proof and occurrence bindings remain transactional; existing bindings retain
their immutable replay behavior. Cached maps contain IDs, counts and hashes,
with no retained legacy text or foreign keys to legacy files/chunks.

Migration 130 adds a covering payload-to-occurrence index so a sparse matching
payload can expand its bindings directly. The reader definition, authorization,
ranking, query matching and tie order remain unchanged. Native tests compare
complete search envelopes, check mutation invalidation and exercise a sparse
binding probe over 100,000 requested occurrences. They also capture nested
execution plans for 100 warm native fragments and require zero executed legacy
chunk scans. Both migrations retain the compatible application binary.

### Filtering native reader inputs before scope expansion

Migration 131 makes the compact fingerprint predicate directly indexable for
simple conjunctive queries before joining the requested occurrence set. Every
emitted segment still matches the complete weighted vector; fingerprint
collisions or terms spread across different segments never establish a match.
Phrases, disjunctions and negation retain complete vector evaluation. Existing
source authorization and forced row policies remain in effect.

Canonical-document matching uses separate small-ID and broad-ID branches so
the exact ID restriction is visible to the planner. Both retain the original
canonical provenance, complete query predicate and legacy-segment fallback.
Presence checks read immutable native bindings directly, whose source and
payload foreign keys already prove dictionary membership. The outer source
and occurrence checks stay intact. Exact preceding definitions and execution
permissions are required before any reader replacement.

Migration 132 adds fixed-width covering indexes for view bindings and document
token counts. Corpus size, token totals, floating point calculations, score
components and rank ties are unchanged. Installation and workload-specific
latency remain separate from the native equivalence and execution-plan tests.
# Bounded request and presence work

The named-generation and active readers evaluate the simple-AND request shape
once per call. Row evaluation, candidate selection and score explanations reuse
that request value without changing the AST, corpus population or ranking.

Compact segment presence uses immutable occurrence keys. The validated,
nondeferrable occurrence foreign key proves source and artifact identity, and
the validated block-order check proves a nonempty segment array. Installation
checks these constraints and their predicate function before removing redundant
array reads. Forced RLS and the outer source authorization remain in place.


### Native regular sync after legacy retirement

Regular active-source sync builds successors from native lexical inputs. Its
staging statement omits the legacy bootstrap routine completely, so PostgreSQL
can prepare it after that routine and the legacy table names have been retired.
Pre-activation release-candidate construction retains its explicit compatibility
bootstrap. Native successor witnesses and idempotency keys identify the native
lexical path; candidate verification still reconstructs bodies, roots, segments
and intelligence evidence before any active-pointer commit.
Native verification derives bounded positive query probes from verified native
membership without consulting retired file or chunk tables. Pre-activation
comparison seeds retain their independent legacy expectations.

CLI source sync and file watchers accept both `ACTIVE_INGEST_COMMITTED` and
`NO_CHANGE` responses. Native JSON preserves generation identity, item counts,
source I/O, telemetry and the activation receipt without inventing legacy chunk
or embedding counts. Watch statistics use complete active membership and exclude
test sources. These runtime contracts do not establish production activation or
cleanup acceptance; the real complete-set and post-cleanup gates remain required.

### First native lexical candidates

Migration 137 reduces matching native segments to their first matching order per
occurrence, source and artifact before the rank reader materializes candidates.
The ordinary branch reduces source-local exact matches before joining requested
IDs. Compact branches retain fingerprint filtering and full per-segment vector
evaluation. Mixed storage remains supported; the parent reader reduces both
representations with its existing score and tie rules. Full lexical enumeration
is unchanged, and precise native scoring still evaluates the selected hits.

Generated-segment detection uses ordinary order zero or compact block zero.
Validated block constraints prove the latter contains segment zero without
fetching segment arrays. Installation checks exact preceding definitions,
execution authority and compact identity constraints before replacing either
rank-reader overload. Native fixtures compare full named/active search envelopes,
independent complete rank results and authorization under ordinary, compact and
mixed storage. These proofs do not replace production latency acceptance.
## Posting-derived native identifiers

Migration 138 avoids storing a second complete copy of native word identifiers.
The constructor selects this representation only when the normalized supplied
identifier set equals the complete word-posting set containing underscores or
ASCII digits. Custom, partial and nonword identifier sets remain explicit.
Full canonical identifiers are available through
`storage_v2_document_exact_identifiers`; scoped identifier membership uses
`storage_v2_document_has_exact_identifier`. Both named-generation and active-set
search retain the original complete result and score contracts.

Existing materialization hashes, content identities, generation roots and
profiles remain unchanged. Migration installation does not convert existing
documents. Completed new posting sets and converted older sets are sealed
against subsequent flat or block INSERTs. Direct flag changes also require a
seal bound to the unchanged document identity. The seal is derived metadata owned by
its document, so native GC removes it only with an unreachable document.
The administrator-only conversion function processes at most 256
documents per call, reports a durable cursor and proves full equality before
selecting the derived representation. Keeping existing values allows the read
path to change before their removal. Removing values reports logical bytes only;
physical space requires a separately verified database reclamation operation.

The matching bounded restoration function reconstructs the full original arrays
before the previous reader/constructor definitions can be restored. Restoration
needs capacity and WAL admission. The immutable document boundary permits only
these proven representation transitions; edits to text, profiles, components,
hashes and other semantic fields continue to fail. Posting immutability is an
installation prerequisite.

## Indexed first positions for ordinary lexical vectors

Migration 139 materializes the exact minimum original segment order for each
ordinary-vector lexeme and occurrence. It stores full source/artifact provenance
and explicit coverage, including occurrences with no ordinary vectors. A bare
single-lexeme query reads this index for covered occurrences and the original
vectors for uncovered occurrences. Phrases, Boolean queries, negation and other
query forms keep the complete vector path. Compact-vector handling is unchanged.
The read helper resolves source authorization before inspecting metadata; both
projection tables have forced row security. Private maintenance tables are not
directly readable or writable by the application role.

Migration 140 applies the established isolated lexical reader policy to both
first-position projection tables. The definer role cannot log in and has no
members. Application users read this metadata through existing functions that
authorize the complete requested source set before querying. Application table
grants, forced row security, source policies for other roles, and all function
definitions remain unchanged. Installation rejects changed role membership,
reader authority, table grants, or row-security configuration. This avoids
repeating the same authorization lookup for every projected lexeme.

The administrator-only materializer processes at most 128 occurrences per call
and returns scanned/materialized counts, inserted terms and a committed cursor.
It uses the established lexical writer advisory lock before locking the parent
occurrence. A real ordinary-vector INSERT atomically invalidates that occurrence's
projection and coverage. Supported ingestion continues through the full fallback
until the projection is rebuilt; an idempotent insert leaves coverage intact.

Both tables are occurrence-owned derived metadata in native GC. Retained graphs
retain their projections; unreachable occurrences lose both tables' rows before
their parent is collected. Projection installation/materialization does not
change vector witnesses, search-document identities or generation roots, and
does not replace production latency or complete candidate-set acceptance.

## Early conjunction candidate rejection

Migration 141 moves the existing lexical-presence exclusion ahead of document
term aggregation for the bounded plain-AND query shape. It probes only visible
occurrences with query postings and no matching lexical rank. An occurrence
with authoritative lexical data that does not satisfy the conjunction cannot
become a document fallback result. An occurrence without lexical data retains
that fallback, and copied/native lexical matches keep their established ranks.

The complete corpus and document frequencies remain available for scoring;
only candidates already excluded by the original Boolean gate lose document
aggregation work. Other query shapes retain their original evaluation.
Installation binds both readers and the conjunction/presence helpers to exact
definitions, checks reader authority, and changes no source, generation, body,
posting, active pointer or table policy. Whole search envelopes and authorization
are compared before and after installation; production acceptance remains a
separate gate.

## Isolated lexical presence reads

Migration 142 keeps the existing presence function body and its source
authorization unchanged. It gives that function an isolated reader owner and
SELECT policies on its immutable occurrence and lexical metadata. Authorized
source IDs are still resolved before any requested occurrence is inspected.
This avoids repeating the same source authorization for each occurrence and
each physical lexical representation.

The role cannot log in, inherit privileges, bypass row security, create roles
or databases, or write the metadata tables. No application role can assume it.
Its only owned function remains callable by the existing application and
frontier roles. The source policy retains its original checks; the role can
read only the user ID and administrator flag required by that policy.

Installation rejects function, EXECUTE, role and row-security drift. Focused
checks compare complete named and active search envelopes and direct presence
results across authorization scopes, absent IDs, empty inputs and null inputs.
No source data, generation, active pointer or stored representation changes.
Live latency and complete candidate-set acceptance remain separate gates.

## Batched reader metadata

Migration 143 gives large unfiltered named and active searches a set-based
metadata join. Small scopes and requests with path, role or time filters keep
their indexed lookup path. The planning choice uses the authorized generation
item counts; both paths retain the same occurrence membership, bindings, token
counts, corpus frequencies and complete scoring inputs.

The legacy segment fallback resolves one occurrence, its artifact, canonical
view binding and document through bounded correlated lookups. Authorization,
generated-projection rejection, source/artifact identity, text hash and exact
vector predicates remain unchanged. Installation rejects reader definition and
EXECUTE authority drift and changes no representations or role policies.

The regression fixture compares full named and active search envelopes for
both metadata plans. A lower planning threshold is used only in its disposable
database to exercise the alternate plan without changing fixture counts or
roots. Functional equality does not establish production latency acceptance.
