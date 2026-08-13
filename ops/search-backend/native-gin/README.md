# Native-GIN backend qualification

Storage v2 uses PostgreSQL's built-in GIN access method. It does not install a
search extension, add a preload library, or produce a separate backend package.
The qualified artifact is therefore the pinned PostgreSQL binary family plus the
versioned schema/query inputs in `backend.lock.json`.

The current supported PostgreSQL minor is 18.4. A local 18.3 package is not an
accepted substitute. The build helper creates an isolated 18.4 prefix and adds
pgvector 0.8.2 only because the complete MainRAG schema depends on that type;
pgvector is not the selected lexical backend.

## Build an isolated target binary

Both arguments must name paths that do not yet exist. Never point them at a
system or production PostgreSQL directory.

```bash
ops/search-backend/native-gin/build-target-postgres.sh \
  /tmp/mainrag-pg18.4-build \
  /tmp/mainrag-pg18.4-prefix
```

The helper downloads the two locked public source archives, verifies SHA-256,
builds PostgreSQL with the recorded feature flags, and installs only into the
given prefix. This is a disposable qualification binary, not a production
package.

## Run the complete disposable-cluster scenario

```bash
python3 ops/search-backend/native-gin/qualify.py \
  --bindir /tmp/mainrag-pg18.4-prefix/bin \
  --commit-sha "$(git rev-parse HEAD)" \
  --output eval/storage_v2/backend/results/native-gin-pg18.4.json
```

The runner fails closed unless all locked inputs match and the server reports
18.4. It checks binary dependencies and the built-in GIN symbol, starts an
empty checksummed cluster with no preload library, runs the frozen exact Top-10
prototype and final storage-v2 schema test, cancels a concurrent GIN build,
detects and removes its invalid artifact, rebuilds the index, performs an
immediate-stop/WAL restart, verifies catalog state and forced GIN query plans,
runs offline page checksums, reindexes, and removes the temporary cluster.

The checked-in evidence contains public versions, hashes, synthetic result
identities, and gate states only. It omits hostnames, addresses, account names,
credentials, temporary paths, and raw logs.

## Production boundary and rollback

There is no backend extension to install or remove and no backend preload entry
to roll back. Production installation, PostgreSQL minor upgrade, restart,
reindex, deployment, or active-read switch all require separate owner approval.
If a future operator accidentally adds a storage-v2 search preload entry, remove
that entry from the candidate configuration and prove ordinary startup in an
isolated cluster before proposing any live change.

Safe removal of a failed disposable index is:

```sql
DROP INDEX IF EXISTS qualification_interrupted_gin;
CREATE INDEX qualification_interrupted_gin
  ON qualification_interrupt USING GIN (fts);
```

Never label a copied prefix, exploratory shared object, or disposable evidence
run as production-ready.
