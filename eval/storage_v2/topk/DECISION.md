# ADR: storage-v2 fixture search backend

- Status: accepted for additive prototype/schema work only
- Decision date: 2026-08-13
- Decision owner: storage-v2 Top-K prototype
- Production status: not installed, deployed, or active

## Decision

Proceed with native PostgreSQL GIN as the storage-v2 prototype and initial
additive-schema search backend. Use complete, occurrence-scoped composed-view
evaluation whenever a pruning bound cannot include every later contribution.

This is a bounded GO for the generation-schema child, not production acceptance.
The checked-in result artifact must show exact SQL/reference Top-10 equality,
tenant/source isolation, no candidate cap, at most 500 fully considered search
documents per fixture query, and warm p95 below 200 ms.

## Why

Native GIN can represent the component-term/phrase match surface while explicit
postings and view membership provide deterministic composed scoring. The
fixture's graph and rerank contributions are evaluated completely after Boolean
coverage, so correctness does not depend on an unproved WAND/MaxScore bound.

Selecting the simplest backend that passes the executable contract avoids
coupling generation DDL to an extension before packaging, durability, and
platform qualification exist.

## Alternatives

### Fixed per-channel or per-term cap

Rejected. An output cap after scoring is not an evaluation bound, and a cap
before composed scoring can omit the true Top-10. The prototype contains no
`LIMIT 500` correctness path.

### PostgreSQL search extension

Deferred, not rejected on relevance quality. No extension is required to prove
the current synthetic contract. An extension would add package, ABI, WAL,
restart, restore, and index-integrity questions that belong to backend
qualification. At most one extension may later be compared against these exact
frozen inputs without changing the reference evaluator.

### Production WAND/MaxScore pruning

Deferred. The fixture proves an upper bound over its known lexical, graph, and
rerank contributions, but it does not prove a safe bound for future learned or
graph-expanded scoring. Until such a bound is executable, the complete fallback
is normative.

## Consequences and unresolved gates

- The additive schema may model native GIN/postings and composed view membership.
- Fixture success is not evidence of production-scale selectivity or latency.
- The fixture creates native GIN but its small scoped relations may plan as
  sequential scans; index presence is not reported as observed index use.
- Backend qualification must still prove reproducible packages, compatible
  PostgreSQL/platform behavior, maintenance headroom, interruption/restart,
  restore/rebuild, and index integrity.
- Shadow evaluation must repeat exact Top-10 and work-count comparisons on the
  accepted public/private-safe corpus identities.
- Any later Top-10 divergence, scope leak, unsafe bound, or gate that passes only
  after truncation changes this decision to NO-GO.
