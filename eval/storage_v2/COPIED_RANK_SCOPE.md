# Authorized copied-rank scope and canonical result paths

Related work: #66, #67, #68. This block improves the common reader and fixes
result presentation; it does not qualify an entire production package.

## Changes

- Migration 117 adds a source-hinted rank helper. Hints restrict work only.
  Requested occurrence identity, source access, artifact identity and canonical
  document provenance are checked before returning copied or native evidence.
- Canonical document IDs are carried with the verified copied cohort. Small
  cohorts use bounded document probes; large cohorts retain a set intersection.
- Plain terms and positive conjunctions reuse verified copied evidence instead
  of computing a second normalized match aggregate for those occurrences.
  Compound queries and native evidence keep their existing matching behavior.
- Returned explanations still use the complete authorized corpus. Exact rank
  boundaries, optional stage scores and fragment grouping remain unchanged.
- Identity metadata is fetched through selected IDs after rank boundaries.
- Result formatting preserves the reader's canonical path, including legitimate
  leading dashes, and derives the displayed location from that same path.

## Validation

`AuthorizedCopiedRankScopeTests` compares the complete previous and candidate
reader envelopes for 432 requests, plus 12 exhaustive fragment boundaries.
It covers named and active reads, two actors, term/AND/OR/NOT/phrase/exact queries,
filters, optional stages and return limits. The migration is replayed twice,
with unchanged occurrences, generations and source pointers.

Additional differential checks cover 72 source-hint, duplicate and null cases,
9 empty or denied hint cases, a broad requested cohort and inconsistent
canonical document bindings. Replay must reject changed helper bodies,
unexpected owners or executors, missing execution authority and index drift.

Two Rust regressions verify canonical path identity and repeatable formatting.
The schema comparison is included in the required hosted CI job.

## Acceptance boundary

No source rebuild, activation, cleanup, backup or restore is part of this block.
Existing production query probes returned identical complete-result hashes.
One slow probe fell below the two-second target; another remains variable and
above that target. These diagnostic measurements do not establish current
package acceptance. Production installation and frozen acceptance checks remain
required before closing the related issues.
