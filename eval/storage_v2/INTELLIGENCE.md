# Storage v2 intelligence gate

Storage-v2 intelligence is additive and selected only through an explicit
source plus generation selector. `current` resolves the active pointer; a
positive generation sequence reads that sealed shadow generation. Legacy
intelligence tables and commands remain available when no generation is given.

## Compatibility ownership

- Issue #51 remains owned by the legacy `process_raw_file` skip path. The new
  occurrence/profile analysis state makes storage-v2 failures retryable but
  does not modify that legacy path and does not claim #51 closed.
- Open PR #43 currently overlaps API routes, intelligence/parser services, and
  CLI entry points. This slice owns only the feature-gated named-generation
  additions; it must be rebased and reverified if PR #43 lands first.
- No legacy intelligence table, current default command, active generation,
  source data, or search ranking is mutated by this slice.

Generic structural cards contain parser-visible names, kinds, signatures,
documentation, visibility, spans, and structure. Domain fields default to
`unknown`. A non-unknown domain value is accepted only with a source-bound
profile ID/version, rule ID, and concrete field evidence. Profile retries and
versions do not invalidate immutable content.

Exports use schema `mainrag.storage-v2-intelligence-export.v1`, canonical JSONB
payloads, and a payload SHA-256. The public default contains only per-class
counts and a protected-payload hash: no cards, names, paths, rules, authors, or
content. Protected exports require an explicit file, owner, and retention date
and are written mode `0600`. Import rejects public summaries, expired
retention metadata, and files accessible by group or other. Never commit raw
exports or attach them to issues/CI artifacts.

```bash
DATABASE_URL=... scripts/storage-v2-intelligence-transfer.py export \
  --source-id 7 --generation 3 --redaction protected \
  --owner migration-operator --retention-until 2026-09-30 \
  --output /protected/location/export.json

DATABASE_URL=... scripts/storage-v2-intelligence-transfer.py import \
  --source-id 8 --generation 1 --input /protected/location/export.json
```

The schema fixture proves deterministic cards, profile provenance, retry after
analysis failure, stable symbols across generations, proven versus unresolved
calls, public redaction, authorization, idempotent import, and equality of all
exported record classes after a clean-source round trip.
