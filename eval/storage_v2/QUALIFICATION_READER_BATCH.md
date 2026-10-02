# Complete intelligence verification and scoped search work

Related work: #66; prerequisites for #67 and #68.

## Intelligence export

The previous complete export assembled a source-wide PostgreSQL text datum.
A source can exceed PostgreSQL's approximately one GiB datum bound even when
every individual record is small. Closing the reader epoch after that error
can then report an aborted transaction and conceal the original failure.

Migration 119 exposes all eight protected v1 collections as ordered native
JSONB record text. Candidate verification consumes one SQL statement so the
collections share one statement snapshot. It hashes each record incrementally,
with the original collection order, delimiters, fields and serialization.
The public envelope retains the v1 counts, source identity, generation and
protected payload SHA-256. PostgreSQL still serializes and hashes the small
public payload. No protected record is returned by the verification endpoint.

Collection and record ordinals are checked while consuming the stream.
Missing, repeated or reordered records fail verification. The existing v1
export/import functions remain intact. Reader epoch protection continues to
cover body reconstruction and export. The bounded integrity deadline covers
both lexical reconstruction and the complete intelligence stream, then the
previous statement timeout is restored.

The additive `intelligence_export_serialized_bytes` field records how many
canonical protected payload bytes were hashed. It is not an input-read count
or a claim that protected records were published.

Named layers/card/explain/ownership qualification requests use the supported
200-result limit. Their evidence explicitly describes bounded command
responses; complete intelligence integrity remains a separate server check.

## Search

Migration 120 retains keyed posting reads for scopes of at most 1,024 entries.
Larger scopes use a deduplicated set. Ordinary postings retain the dependent
term hash check. Compact blocks are restricted to requested document IDs
before their arrays are expanded. Every exact matching term/frequency entry
is retained, including repeated entries and fingerprint collisions.

Migration 121 couples source IDs and FTS vectors in the same copied lexical
GIN index. Typed source predicates can then restrict the bitmap before phrase
position rechecks. The index is replaced transactionally under its existing
name; relation ownership and index identity are checked before replacement.
The implementation requires `btree_gin` in the public schema. Existing source
authorization, canonical provenance, rank calculation and result limits remain
part of the normal reader.

## Diagnostics and validation

Reader epoch finalization preserves the original error when cleanup also
fails. The verification handler reports only a static operation label, primary
SQLSTATE, epoch-close SQLSTATE and whether retention remains required. Database
messages and private input values are excluded from the HTTP diagnostic.

Required regression coverage includes complete v1 export equivalence for all
eight classes, generation scope and import round trips, native Unicode and
numeric serialization, repeated ordering keys, a digest stream exceeding one
GiB, malformed streams, failed finalization, authority drift and replay.
Search checks compare full result envelopes and independent posting references
over 50,000 documents, including sparse/dense scopes, duplicates, null IDs and
fingerprint collisions. A disposable fixture proves that the GIN can combine
the typed source and FTS predicates.

These checks prove component behavior. Production resource admission, controlled
installation, retained recovery evidence, rollback identity and candidate
qualification are separate gates. This block changes no generation, active
pointer or payload. It does not establish final acceptance of any of the three
issues by itself.
