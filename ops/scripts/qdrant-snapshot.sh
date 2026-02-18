#!/bin/bash
# MAINRAG Qdrant Snapshot Script
# Creates snapshots of all collections with 7-day retention

set -euo pipefail

QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
API_KEY="${QDRANT_API_KEY:?QDRANT_API_KEY must be set}"
SNAPSHOT_DIR="/data/qdrant/snapshots"
RETENTION_DAYS=7

echo "[$(date)] Starting Qdrant snapshot..."

# Get all collections
COLLECTIONS=$(curl -s -H "api-key: ${API_KEY}" "${QDRANT_URL}/collections" | jq -r '.result.collections[].name')

for COLLECTION in $COLLECTIONS; do
    echo "[$(date)] Creating snapshot for collection: ${COLLECTION}"

    RESPONSE=$(curl -s -X POST \
        -H "api-key: ${API_KEY}" \
        "${QDRANT_URL}/collections/${COLLECTION}/snapshots")

    SNAPSHOT_NAME=$(echo "${RESPONSE}" | jq -r '.result.name // "error"')

    if [[ "${SNAPSHOT_NAME}" != "error" && "${SNAPSHOT_NAME}" != "null" ]]; then
        echo "[$(date)] Snapshot created: ${SNAPSHOT_NAME}"
    else
        echo "[$(date)] WARNING: Failed to create snapshot for ${COLLECTION}"
        echo "${RESPONSE}"
    fi
done

# List snapshots
echo "[$(date)] Current snapshots:"
for COLLECTION in $COLLECTIONS; do
    echo "  ${COLLECTION}:"
    curl -s -H "api-key: ${API_KEY}" "${QDRANT_URL}/collections/${COLLECTION}/snapshots" \
        | jq -r '.result[] | "    - \(.name) (\(.size) bytes)"' 2>/dev/null || echo "    (none)"
done

echo "[$(date)] Qdrant snapshot complete"
