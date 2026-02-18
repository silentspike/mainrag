#!/bin/bash
# MAINRAG Migration Verification
# Compares counts and validates data integrity after migration

set -euo pipefail

CODERAG_DB="${CODERAG_DB:-/data/coderag/coderag.db}"
EXPORT_DIR="${EXPORT_DIR:-/work/mainrag/ops/migration/export}"

export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}"
PG_HOST="${POSTGRES_HOST:-localhost}"
PG_PORT="${POSTGRES_PORT:-5432}"
PG_DB="${POSTGRES_DB:-mainrag}"
PG_USER="${POSTGRES_USER:-mainrag}"

QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
QDRANT_API_KEY="${QDRANT_API_KEY:?QDRANT_API_KEY must be set}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-mainrag_chunks}"

echo "=== MAINRAG Migration Verification ==="
echo ""

# Function to format numbers
fmt() {
    printf "%'d" "$1"
}

# Source counts
echo "=== Record Counts ==="
printf "%-20s %15s %15s %10s\n" "Table" "CodeRag" "MAINRAG" "Delta"
printf "%-20s %15s %15s %10s\n" "--------------------" "---------------" "---------------" "----------"

# Sources
CR_SOURCES=$(sqlite3 "${CODERAG_DB}" "SELECT COUNT(*) FROM sources")
MR_SOURCES=$(psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" -t -c "SELECT COUNT(*) FROM sources" | tr -d ' ')
DELTA=$((MR_SOURCES - CR_SOURCES))
printf "%-20s %15s %15s %10s\n" "sources" "$(fmt ${CR_SOURCES})" "$(fmt ${MR_SOURCES})" "${DELTA}"

# Files
CR_FILES=$(sqlite3 "${CODERAG_DB}" "SELECT COUNT(*) FROM files")
MR_FILES=$(psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" -t -c "SELECT COUNT(*) FROM files" | tr -d ' ')
DELTA=$((MR_FILES - CR_FILES))
printf "%-20s %15s %15s %10s\n" "files" "$(fmt ${CR_FILES})" "$(fmt ${MR_FILES})" "${DELTA}"

# Chunks
CR_CHUNKS=$(sqlite3 "${CODERAG_DB}" "SELECT COUNT(*) FROM chunks")
MR_CHUNKS=$(psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" -t -c "SELECT COUNT(*) FROM chunks" | tr -d ' ')
DELTA=$((MR_CHUNKS - CR_CHUNKS))
printf "%-20s %15s %15s %10s\n" "chunks" "$(fmt ${CR_CHUNKS})" "$(fmt ${MR_CHUNKS})" "${DELTA}"

# Symbols
CR_SYMBOLS=$(sqlite3 "${CODERAG_DB}" "SELECT COUNT(*) FROM symbols")
MR_SYMBOLS=$(psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" -t -c "SELECT COUNT(*) FROM symbols" | tr -d ' ')
DELTA=$((MR_SYMBOLS - CR_SYMBOLS))
printf "%-20s %15s %15s %10s\n" "symbols" "$(fmt ${CR_SYMBOLS})" "$(fmt ${MR_SYMBOLS})" "${DELTA}"

# Call Graph
CR_CG=$(sqlite3 "${CODERAG_DB}" "SELECT COUNT(*) FROM call_graph")
MR_CG=$(psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" -t -c "SELECT COUNT(*) FROM call_graph" | tr -d ' ')
DELTA=$((MR_CG - CR_CG))
printf "%-20s %15s %15s %10s\n" "call_graph" "$(fmt ${CR_CG})" "$(fmt ${MR_CG})" "${DELTA}"

# Embeddings (CodeRag vs MAINRAG chunk_embeddings)
CR_EMBED=$(sqlite3 "${CODERAG_DB}" "SELECT COUNT(*) FROM chunk_embeddings")
MR_EMBED=$(psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" -t -c "SELECT COUNT(*) FROM chunk_embeddings" | tr -d ' ')
DELTA=$((MR_EMBED - CR_EMBED))
printf "%-20s %15s %15s %10s\n" "chunk_embeddings" "$(fmt ${CR_EMBED})" "$(fmt ${MR_EMBED})" "${DELTA}"

echo ""

# Qdrant verification
echo "=== Qdrant Verification ==="
QDRANT_COUNT=$(curl -s -H "api-key: ${QDRANT_API_KEY}" "${QDRANT_URL}/collections/${QDRANT_COLLECTION}" | jq -r '.result.points_count // 0')
echo "Qdrant points: $(fmt ${QDRANT_COUNT})"
echo "Expected (chunk_embeddings): $(fmt ${MR_EMBED})"
if [[ "${QDRANT_COUNT}" -eq "${MR_EMBED}" ]]; then
    echo "Status: OK (counts match)"
else
    echo "Status: MISMATCH"
fi
echo ""

# Embedding model verification
echo "=== Embedding Model ==="
OLD_MODEL=$(sqlite3 "${CODERAG_DB}" "SELECT DISTINCT model FROM chunk_embeddings LIMIT 1")
NEW_MODEL=$(psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" -t -c "SELECT DISTINCT model FROM chunk_embeddings LIMIT 1" | tr -d ' ')
echo "CodeRag model: ${OLD_MODEL}"
echo "MAINRAG model: ${NEW_MODEL}"

# Vector dimension check
echo ""
echo "=== Vector Dimensions ==="
OLD_DIM=$(sqlite3 "${CODERAG_DB}" "SELECT length(vector)/4 FROM chunk_embeddings LIMIT 1")
NEW_DIM=$(psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" -t -c "SELECT vector_dims(vector) FROM chunk_embeddings LIMIT 1" | tr -d ' ')
echo "CodeRag dimensions: ${OLD_DIM}"
echo "MAINRAG dimensions: ${NEW_DIM}"
if [[ "${NEW_DIM}" == "768" ]]; then
    echo "Status: OK (768-dim BGE)"
else
    echo "Status: WARNING (expected 768)"
fi
echo ""

# Sample search test
echo "=== Search Test ==="
echo "Testing hybrid search via API..."

SEARCH_RESPONSE=$(curl -s "http://localhost:3001/api/v1/search?q=function&limit=3" 2>/dev/null || echo '{"error":"API not available"}')

if echo "${SEARCH_RESPONSE}" | jq -e '.results' > /dev/null 2>&1; then
    RESULT_COUNT=$(echo "${SEARCH_RESPONSE}" | jq '.results | length')
    echo "Search returned ${RESULT_COUNT} results"
    echo "First result:"
    echo "${SEARCH_RESPONSE}" | jq -r '.results[0] | "  File: \(.file_path)\n  Score: \(.score)"' 2>/dev/null || echo "  (no results)"
else
    echo "API not available or error: ${SEARCH_RESPONSE}"
fi
echo ""

# Summary
echo "=== Migration Summary ==="
TOTAL_CR=$((CR_SOURCES + CR_FILES + CR_CHUNKS + CR_SYMBOLS + CR_CG))
TOTAL_MR=$((MR_SOURCES + MR_FILES + MR_CHUNKS + MR_SYMBOLS + MR_CG))
echo "Total records in CodeRag: $(fmt ${TOTAL_CR})"
echo "Total records in MAINRAG: $(fmt ${TOTAL_MR})"

if [[ ${TOTAL_MR} -ge ${TOTAL_CR} ]]; then
    echo "Status: COMPLETE"
else
    MISSING=$((TOTAL_CR - TOTAL_MR))
    echo "Status: INCOMPLETE (${MISSING} records missing)"
fi
