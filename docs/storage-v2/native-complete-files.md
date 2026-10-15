# Complete historical files in native storage

The `mainrag-complete-file` binary registers and reads complete historical files
through existing storage-v2 bodies, packs and durable legacy-hit mappings. Build
it with the `storage-v2-retrieval` feature.

A complete-file identity contains source ID, original file ID and revision,
actual raw byte length and SHA-256, and the SHA-256 of the original source path.
Its mapping namespace is `legacy-file-v1:`. The original legacy metadata hash is
a separate registration CAS field; it need not equal the raw byte hash.

## Operations

- `register-existing` maps existing native fragments, then verifies the complete
  ordered bytes without storing another payload.
- `register-zstd` accepts bounded compressed input when payload is missing. An
  identical native body is verified and reused without consuming input.
- `read` stages and verifies every component, contiguous range, complete byte
  length and raw SHA before writing a private data output.
- `reconcile` reads visible mapping state after an uncertain outcome. It never
  retries a registration or reports raw-byte acceptance.

Every operation requires `--connection-file`, `--request-file`, `--pack-root`
and a new `--output`. Reads additionally require a new `--data-output`.
Connection and request files must be canonical, private files owned by the
process user. Database connections are restricted to Unix sockets or loopback.
The authenticated maintenance caller supplies the source principal and binds
the request to its original manifest and resource account.

The request contains `operation_id`, `manifest_sha256`, `work_origin_sha256`,
`principal`, `registration`, `fragments`, and
`maximum_encoded_input_bytes`. Registration binds the full identity, generation
selector, original legacy epoch and hash, expected mapping state, test scope,
and finite logical, pack and scratch limits. Existing fragments name native
occurrences with original byte offsets and lengths.

The outer maintenance operator must reserve the entire simultaneous scratch
and pack allocation before dispatch. Per-file limits do not renew the original
global time, read, write, WAL or storage budget.

## Outcomes and retirement

A durable create-only intent precedes mutation. A separate `.committed.json`
receipt follows an actual database COMMIT acknowledgement. Keep a published
pack on a failed or uncertain commit; only the existing orphan/GC procedures
may reclaim it. Preserve the original intent and reconcile before any retry.

Registration creates no generation or generation membership. The reader checks
source authorization, supports retained generation resolution, and keeps native
reader epochs throughout pack access. Component pages are bounded to 32 rows.

This service provides a complete-file consumer after legacy content tables are
retired. Its availability does not establish that all other legacy callers have
been removed, authorize cleanup, or prove physical storage savings. Those gates
remain bound to the exact cleanup manifest and accepted activation evidence.
