# Scoped metadata and shared query posting reads

Related work: #66; prerequisites for #67 and #68.

## Metadata presence

The expanded lexical view joins compact block arrays to one logical row per
segment. A correlated generated-marker predicate can become a hashed subplan
that scans and expands the complete compact corpus. A retained diagnostic
observed more than 100,000 unrelated block expansions for six required items.

Migration 122 uses keyed ordinary and compact metadata probes. Checking whether
segment zero exists only needs the segment-order array. Checking any segment
presence only needs occurrence/source/artifact identity and a nonempty array.
Neither check needs vector expansion. The original function owners, execution
grants, source authorization and canonical document checks remain in place.

## Shared query postings

Fetching every global full-term row before restricting the document scope
causes unnecessary heap reads. Probing every requested document separately is
also expensive for large scopes. Migration 123 adds a covering digest/document
index and a query-wide posting function. It reads fixed-width matching keys and
frequencies first, restricts them to the requested document set, then loads and
checks the complete term only for those candidates. Digest collisions cannot
produce an exact-term result.

Compact candidate blocks are selected once for all query terms. Full arrays are
loaded only after document scope is established. Exact array positions preserve
all matching term/frequency pairs, including duplicates and physical ordinal
pairing. Small scopes retain the existing point lookup. Query terms and document
IDs are sets; repeated/null input entries do not duplicate results.

Both exact-generation and active readers use this function. Existing single-term
interfaces and indexes are retained. No content, generation, active pointer,
legacy row or compatibility mapping is rewritten. These SQL changes reuse the
installed API and require no new Rust release binary.

## Validation and installation boundary

Tests compare complete preceding-reader result envelopes, authorization, compound
queries, phrase semantics, ties and replay. Independent posting references cover
50,000 documents, multiple terms, both scope thresholds, duplicate/null inputs,
false digest collisions and nonstandard array lower bounds. A separate plan
checks four metadata probes against 50,000 rows without vector expansion.
Function/index identity and authority drift abort migration replay.

The new index needs a bounded maintenance window and fresh physical resource
admission. Preserve its previous absence and the exact preceding function
definitions for rollback. A deployment receipt binds the retained binary,
migrations, function/index identities and unchanged semantic state. Component
tests and a transactional diagnostic do not establish production qualification.
Retain valid reconstruction/export evidence within its proven scope, and rerun
only affected reader gates before the required complete candidate-set gate.
