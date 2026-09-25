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

The feature-gated Storage-v2 adapter validates the manifest and rereads every
listed segment in this version. It rejects missing, replaced, truncated,
invalid-size and digest-mismatched segments, including same-length edits. The
legacy index path rejects this source type. The source watermark includes the
epoch through the logical item keys. This version establishes the controlled
producer and full-read baseline; it does not claim incremental I/O or advance
the persisted append frontier. Subsequent optimization must verify that
frontier and periodically compare all segments before reusing old artifacts.
