# Storage-v2 generation schema checks

This suite proves the additive generation schema against a disposable
PostgreSQL cluster. It covers transactional bootstrap, idempotent migration
application, source-local generation allocation, immutable artifacts,
half-open membership intervals, atomic activation and requalification, direct
mutation rejection, cross-source rejection, and RLS isolation.

Run against the locally installed PostgreSQL binaries:

```bash
python3 -m unittest eval/storage_v2/schema/test_generation_schema.py
python3 -m unittest eval/storage_v2/schema/test_content_schema.py
python3 -m unittest eval/storage_v2/schema/test_content_graph_schema.py
python3 -m unittest eval/storage_v2/schema/test_shadow_ingest_schema.py
```

The shadow-ingest suite also exercises the additive storage-v2 intelligence
schema, named-generation commands, redacted export, and clean-source import.

For the release gate, point the suite at a separately started disposable
PostgreSQL 18.4 server over a private Unix socket:

```bash
STORAGE_V2_TEST_SOCKET=/path/to/socket \
  python3 -m unittest eval/storage_v2/schema/test_generation_schema.py
```

The suite creates uniquely named databases and removes them on exit. Fixtures
contain only synthetic identifiers and content.
