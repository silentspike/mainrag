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
artifact to the verified generation witness, classifies every difference into
the closed taxonomy, and rejects unexplained differences. Abandoned test-only
building runs may be cancelled and marked with an unreadable lifecycle
tombstone; immutable staging rows remain as audit evidence and no membership or
active pointer is changed.

## Source release-candidate interface

The same feature provides a distinct production build surface for #66. It
accepts any registered source adapter, captures a canonical source watermark,
builds and verifies one immutable generation, and leaves qualification separate.
The production path does not inject the fixture's controlled parser retry and
does not infer or update an active generation.

The qualification surface accepts protected evidence only after supported
current and named-generation reads have produced accepted dual-read evidence.
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
