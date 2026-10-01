# Search-document constructor token reuse

Related work: #66. This block reduces search-document materialization work
for resumable source ingestion.

## Implementation

Migration 118 tokenizes the word and punctuation-preserving classes once each.
Word contributions produce the existing token count; combined frequencies
produce the existing exact postings. Canonically ordered, aligned arrays feed
the same complete 256-term compact blocks.

The constructor retains its administrator check, component lookup, identifier
normalization, materialization hash, indexed reuse lookup and insert-conflict
readback. Existing documents, profiles, generations and active pointers are
preserved. The migration accepts only the known preceding or successor function
identity and the expected function owner.

## Verification

`ConstructorTokenReuseTests` compares complete document fields, generated FTS,
materialization hashes and all compact terms, frequencies and fingerprints for
15 inputs. Fixtures cover empty text, punctuation, Unicode, repeated words,
long individual terms, block boundaries, many distinct terms and a projection
larger than one MiB. Canonical fixture components are bounded independently
of their extracted search projections.

The same gate checks migration replay, existing row counts, idempotency,
unauthorized writes, conflicting materializations and rejected function/owner
drift. Real concurrent backends cover identical and conflicting inserts for
both body and node components, including the uncommitted-winner readback.
The regression runs in required hosted CI.

A transactional production diagnostic using an existing one-MiB projection
returned the same complete document/block hash with both constructors. The
single observed timings were 615 ms and 494 ms. Both transactions rolled back.
This observation does not establish sustained throughput or prove that a
previous statement cancellation cannot recur.

## Operational boundary

Function replacement requires retained previous definition and authority,
exact package identity and unchanged semantic state. Existing recovery evidence
can be reused within its established scope. Source completion, grouped current
package qualification, activation and legacy cleanup remain separate gates.
