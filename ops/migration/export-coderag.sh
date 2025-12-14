#!/bin/bash
# MAINRAG Migration: Export from CodeRag SQLite
# Exports sources, files, and chunks to JSON for import into MAINRAG
# NOTE: Embeddings are NOT exported - they will be regenerated with TEI (768-dim BGE)

set -euo pipefail

CODERAG_DB="${CODERAG_DB:-/data/coderag/coderag.db}"
EXPORT_DIR="${EXPORT_DIR:-/work/mainrag/ops/migration/export}"
BATCH_SIZE="${BATCH_SIZE:-10000}"

echo "=== CodeRag Export for MAINRAG Migration ==="
echo "Source DB: ${CODERAG_DB}"
echo "Export Dir: ${EXPORT_DIR}"
echo "Batch Size: ${BATCH_SIZE}"
echo ""

# Check source DB exists
if [[ ! -f "${CODERAG_DB}" ]]; then
    echo "ERROR: CodeRag database not found at ${CODERAG_DB}"
    exit 1
fi

# Create export directory
mkdir -p "${EXPORT_DIR}"

# Get counts
echo "=== Database Statistics ==="
sqlite3 "${CODERAG_DB}" <<'SQL'
SELECT 'Sources: ' || COUNT(*) FROM sources;
SELECT 'Files: ' || COUNT(*) FROM files;
SELECT 'Chunks: ' || COUNT(*) FROM chunks;
SELECT 'Symbols: ' || COUNT(*) FROM symbols;
SELECT 'Call Graph: ' || COUNT(*) FROM call_graph;
SELECT 'Embeddings (will be regenerated): ' || COUNT(*) FROM chunk_embeddings;
SQL
echo ""

# Export sources
echo "[$(date)] Exporting sources..."
sqlite3 -json "${CODERAG_DB}" "SELECT id, name, type, path, config, last_synced, file_count, total_size, created_at, updated_at FROM sources ORDER BY id" > "${EXPORT_DIR}/sources.json"
echo "  Exported: $(jq length "${EXPORT_DIR}/sources.json") sources"

# Export files in batches (content is compressed, we'll decompress on import)
echo "[$(date)] Exporting files..."
TOTAL_FILES=$(sqlite3 "${CODERAG_DB}" "SELECT COUNT(*) FROM files")
OFFSET=0
FILE_BATCH=0

> "${EXPORT_DIR}/files.jsonl"  # Truncate

while [[ ${OFFSET} -lt ${TOTAL_FILES} ]]; do
    sqlite3 -json "${CODERAG_DB}" "
        SELECT
            id, source_id, path,
            hex(hash) as hash_hex,
            hex(content) as content_hex,
            language, size_original, size_compressed,
            last_modified, created_at, updated_at
        FROM files
        ORDER BY id
        LIMIT ${BATCH_SIZE} OFFSET ${OFFSET}
    " >> "${EXPORT_DIR}/files.jsonl"

    OFFSET=$((OFFSET + BATCH_SIZE))
    FILE_BATCH=$((FILE_BATCH + 1))
    echo "  Batch ${FILE_BATCH}: ${OFFSET}/${TOTAL_FILES} files"
done
echo "  Total: ${TOTAL_FILES} files exported"

# Export chunks in batches (content is compressed)
echo "[$(date)] Exporting chunks..."
TOTAL_CHUNKS=$(sqlite3 "${CODERAG_DB}" "SELECT COUNT(*) FROM chunks")
OFFSET=0
CHUNK_BATCH=0

> "${EXPORT_DIR}/chunks.jsonl"  # Truncate

while [[ ${OFFSET} -lt ${TOTAL_CHUNKS} ]]; do
    sqlite3 -json "${CODERAG_DB}" "
        SELECT
            id, file_id, chunk_type,
            hex(content_hash) as content_hash_hex,
            hex(content_compressed) as content_compressed_hex,
            start_line, end_line, parent_chunk_id, metadata, created_at
        FROM chunks
        ORDER BY id
        LIMIT ${BATCH_SIZE} OFFSET ${OFFSET}
    " >> "${EXPORT_DIR}/chunks.jsonl"

    OFFSET=$((OFFSET + BATCH_SIZE))
    CHUNK_BATCH=$((CHUNK_BATCH + 1))
    echo "  Batch ${CHUNK_BATCH}: ${OFFSET}/${TOTAL_CHUNKS} chunks"
done
echo "  Total: ${TOTAL_CHUNKS} chunks exported"

# Export symbols
echo "[$(date)] Exporting symbols..."
sqlite3 -json "${CODERAG_DB}" "SELECT id, file_id, name, type, line_start, line_end, context FROM symbols ORDER BY id" > "${EXPORT_DIR}/symbols.json"
echo "  Exported: $(jq length "${EXPORT_DIR}/symbols.json") symbols"

# Export call graph
echo "[$(date)] Exporting call graph..."
sqlite3 -json "${CODERAG_DB}" "SELECT id, caller_symbol_id, callee_symbol_id, callee_name, call_line, call_type, is_external FROM call_graph ORDER BY id" > "${EXPORT_DIR}/call_graph.json"
echo "  Exported: $(jq length "${EXPORT_DIR}/call_graph.json") call graph entries"

# Create manifest
echo "[$(date)] Creating manifest..."
cat > "${EXPORT_DIR}/manifest.json" << EOF
{
    "source_db": "${CODERAG_DB}",
    "export_date": "$(date -Iseconds)",
    "export_host": "$(hostname)",
    "counts": {
        "sources": $(sqlite3 "${CODERAG_DB}" "SELECT COUNT(*) FROM sources"),
        "files": ${TOTAL_FILES},
        "chunks": ${TOTAL_CHUNKS},
        "symbols": $(sqlite3 "${CODERAG_DB}" "SELECT COUNT(*) FROM symbols"),
        "call_graph": $(sqlite3 "${CODERAG_DB}" "SELECT COUNT(*) FROM call_graph")
    },
    "notes": {
        "embeddings": "NOT exported - will be regenerated with TEI (BGE-base-en-v1.5, 768-dim)",
        "old_model": "all-MiniLM-L6-v2 (384-dim)",
        "new_model": "BAAI/bge-base-en-v1.5 (768-dim)"
    }
}
EOF

echo ""
echo "=== Export Complete ==="
echo "Export directory: ${EXPORT_DIR}"
ls -lh "${EXPORT_DIR}/"
echo ""
echo "Next step: Run import-mainrag.py to import into PostgreSQL + Qdrant"
