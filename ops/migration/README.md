# MAINRAG Migration from CodeRag

Scripts for migrating data from CodeRag (SQLite) to MAINRAG (PostgreSQL + Qdrant).

## Important Notes

1. **Embeddings are regenerated** - CodeRag uses `all-MiniLM-L6-v2` (384-dim), MAINRAG uses `BAAI/bge-base-en-v1.5` (768-dim)
2. **Full re-embedding required** - ~114k chunks need new embeddings via TEI
3. **Estimated time** - ~2-4 hours depending on TEI throughput

## Prerequisites

Before migration:
- PostgreSQL running with `mainrag` database and schema deployed
- Qdrant running with `mainrag_chunks` collection created
- TEI running with BGE-base-en-v1.5 model loaded
- ~50GB free disk space for export files

## Migration Steps

### 1. Export from CodeRag

```bash
cd /work/mainrag/ops/migration
chmod +x export-coderag.sh
./export-coderag.sh
```

This creates JSON/JSONL files in `./export/`:
- `sources.json` - Source definitions
- `files.jsonl` - File metadata and content
- `chunks.jsonl` - Chunk content (embeddings NOT included)
- `symbols.json` - Symbol definitions
- `call_graph.json` - Call relationships
- `manifest.json` - Export metadata

### 2. Import to MAINRAG

```bash
# Activate Python environment
source /venv/bin/activate

# Set credentials
export POSTGRES_PASSWORD='<REDACTED_DB_PW>'
export QDRANT_API_KEY='<REDACTED_QDRANT_API_KEY>'

# Run import
python import-mainrag.py --export-dir ./export
```

Options:
- `--skip-prerequisites` - Skip service availability checks
- `--skip-embeddings` - Import metadata only (no TEI/Qdrant)

### 3. Verify Migration

```bash
chmod +x verify-migration.sh
./verify-migration.sh
```

This compares record counts and validates:
- Source/file/chunk/symbol counts match
- Qdrant point count matches embeddings
- Vector dimensions are 768
- Search API returns results

## Rollback

If migration fails, the original CodeRag SQLite database is untouched at `/data/coderag/coderag.db`.

To reset MAINRAG:
```bash
PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag << 'SQL'
TRUNCATE sources CASCADE;
TRUNCATE files CASCADE;
TRUNCATE chunks CASCADE;
TRUNCATE symbols CASCADE;
TRUNCATE call_graph CASCADE;
SQL

# Reset Qdrant collection
curl -X DELETE -H "api-key: <REDACTED_QDRANT_API_KEY>" \
  http://localhost:6333/collections/mainrag_chunks
```

## Incremental Migration

For ongoing sync after initial migration, use the API:

```bash
# Sync a specific source
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:3001/api/v1/admin/sources/1/sync
```

## Data Mapping

| CodeRag Table | MAINRAG Table | Notes |
|---------------|---------------|-------|
| sources | sources | type → source_type, path → base_path |
| files | files | content decompressed, hash as hex |
| chunks | chunks | content decompressed, new embeddings |
| chunk_embeddings | chunk_embeddings | Regenerated with BGE-768 |
| symbols | symbols | context → signature |
| call_graph | call_graph | caller_symbol_id → caller_id |
| embeddings (file-level) | NOT MIGRATED | File-level embeddings deprecated |

## Troubleshooting

### TEI timeout during embedding
Increase batch size or add retries:
```bash
export BATCH_SIZE=50  # Reduce from 100
```

### Out of memory
Run in smaller batches:
```bash
export BATCH_SIZE=25
```

### Qdrant connection refused
Check Qdrant is running:
```bash
systemctl status qdrant
curl http://localhost:6333/health
```

### PostgreSQL constraint violations
Check for duplicate entries:
```sql
SELECT path, COUNT(*) FROM files GROUP BY source_id, path HAVING COUNT(*) > 1;
```
