# Empty native stores and shared compact posting scopes

Migration 124 addresses two remaining costs of broad reader scopes:

- Resolve authorized source identities and check their keyed ordinary/compact
  lexical metadata before expanding occurrence arrays or invoking text search.
  Each nonempty store retains its original forced RLS, requested occurrence
  membership, source and artifact provenance checks.
- Retain the covering ordinary posting index and exact full-term comparison.
  An ordinary-only document scope skips global compact term matching. For scopes
  with compact data, materialize narrow fingerprint candidate keys once, join
  the requested document set, and load payload arrays only for scoped keys.

The small document scope path, complete term frequencies, duplicate terms,
ordinal pairing for unusual array bounds, canonical scores, fragment grouping,
result identity, ACLs and generation semantics are preserved. Both function
bodies and their execution authority are checked before replacement and replay.
Retained helper and index identities are checked as well.

The regression suite uses disposable PostgreSQL databases, complete exact and
active result envelopes, an independent original native candidate function,
and a 50,000-document physical flat/compact corpus. Actual nested executor plans
prove that absent native stores avoid occurrence expansion and that compact
candidate selection happens once as a set. These bounds do not claim production
latency acceptance. Production probes run after the source writer settles.
