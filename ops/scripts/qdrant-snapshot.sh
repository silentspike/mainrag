#!/bin/bash
# MAINRAG Qdrant Snapshot Script
# Creates snapshots of all collections with 7-day retention

set -euo pipefail

QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
if [[ -z "${QDRANT_API_KEY:-}" ]]; then
    echo "[$(date)] ERROR: QDRANT_API_KEY is not set" >&2
    exit 1
fi
API_KEY="${QDRANT_API_KEY}"
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

# Cleanup old snapshots (retention policy)
echo "[$(date)] Cleaning up snapshots older than ${RETENTION_DAYS} days..."
CUTOFF_DATE=$(date -d "${RETENTION_DAYS} days ago" +%Y-%m-%dT%H:%M:%S)

for COLLECTION in $COLLECTIONS; do
    # Get list of snapshots with their creation times
    SNAPSHOTS=$(curl -s -H "api-key: ${API_KEY}" "${QDRANT_URL}/collections/${COLLECTION}/snapshots" \
        | jq -r '.result[] | "\(.name) \(.creation_time // "unknown")"' 2>/dev/null || true)

    if [[ -n "$SNAPSHOTS" ]]; then
        while IFS=' ' read -r SNAP_NAME SNAP_TIME; do
            # Skip if creation_time is unknown or empty
            if [[ "$SNAP_TIME" == "unknown" || -z "$SNAP_TIME" ]]; then
                continue
            fi

            # Compare dates (Qdrant uses ISO 8601 format)
            SNAP_DATE="${SNAP_TIME:0:19}"
            if [[ "$SNAP_DATE" < "$CUTOFF_DATE" ]]; then
                echo "[$(date)] Deleting old snapshot: ${SNAP_NAME} (created: ${SNAP_DATE})"
                DELETE_RESP=$(curl -s -X DELETE \
                    -H "api-key: ${API_KEY}" \
                    "${QDRANT_URL}/collections/${COLLECTION}/snapshots/${SNAP_NAME}")

                if echo "$DELETE_RESP" | jq -e '.result == true' > /dev/null 2>&1; then
                    echo "[$(date)] Successfully deleted: ${SNAP_NAME}"
                else
                    echo "[$(date)] WARNING: Failed to delete ${SNAP_NAME}: ${DELETE_RESP}"
                fi
            fi
        done <<< "$SNAPSHOTS"
    fi
done

# List remaining snapshots
echo "[$(date)] Current snapshots after cleanup:"
for COLLECTION in $COLLECTIONS; do
    echo "  ${COLLECTION}:"
    curl -s -H "api-key: ${API_KEY}" "${QDRANT_URL}/collections/${COLLECTION}/snapshots" \
        | jq -r '.result[] | "    - \(.name) (\(.size) bytes)"' 2>/dev/null || echo "    (none)"
done

echo "[$(date)] Qdrant snapshot complete"
