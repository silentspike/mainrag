# Managed append source contract

`tools/managed_append.py` owns a `managed_append` source root. Initialize an
empty root with `python3 tools/managed_append.py init ROOT`, then append a
complete JSONL segment with `python3 tools/managed_append.py append ROOT INPUT`.
Each input is at most 1 MiB, ends with a newline, and contains JSON objects.

The writer holds one local lock, writes a new segment without replacing an
existing name, syncs it, then atomically replaces and syncs `manifest.json`.
The manifest binds an epoch, ordered sequence numbers, byte lengths and
SHA-256 digests in a versioned chain. A crash before manifest publication can
leave an unreferenced segment; readers ignore it and operators retain it until
its ownership is checked. The root must be writable only by the designated
producer. Other writers, file edits, manifest edits, and automatic rotation
violate the trusted append contract. A new epoch requires a new root and an
explicit source-configuration decision.

The feature-gated Storage-v2 writer initially verifies all segment bytes. After
that run is sealed and verified, migration 063 binds its epoch, manifest chain,
segment count, run, and generation to a source-local frontier. A later run
checks the entire manifest chain and each segment's file metadata. If the
prefix matches the verified frontier, it copies the prior staged artifacts and
occurrences in one database operation and reads only newly published segment
content. The final watermark scan repeats these checks before sealing. A
scheduled full comparison rereads every segment after 32 successful delta
runs; a content mismatch fails the run. Shrink, epoch change, and prefix-chain
drift fail closed. A newly selected adapter profile starts with a full read.

The trusted interval relies on exclusive producer ownership of the root. An
out-of-contract same-size edit to an old segment is detected at the next full
comparison, not during a delta read. Telemetry counts manifest reads, verified
segment reads, and subsequent writer reads separately from logical input
bytes; filesystem metadata and device I/O are outside application-read bytes.
The legacy index path rejects this source type. Managed append does not change
any active-generation pointer.
