#!/bin/bash
# Phase 13: Pre-Migration Checks
# Validates system health and creates backups before pgvector-only migration
# Usage: bash ops/scripts/phase13-pre-migration.sh

set -e

echo "=== Phase 13: Pre-Migration Checks ==="
echo ""

# 1. System Health Checks
echo "[1/4] Checking system health..."
if ! curl -sf http://localhost:3001/health > /dev/null; then
    echo "ERROR: API not responding on :3001"
    exit 1
fi
echo "✓ API health check passed"

if ! curl -sf http://localhost:6333/health > /dev/null; then
    echo "WARNING: Qdrant not responding on :6333 (expected if already stopped)"
else
    echo "✓ Qdrant health check passed"
fi

# PostgreSQL check
if ! PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag -c "SELECT 1" > /dev/null 2>&1; then
    echo "ERROR: PostgreSQL connection failed"
    exit 1
fi
echo "✓ PostgreSQL connection check passed"

# 2. Create Backups
echo ""
echo "[2/4] Creating backups..."

BACKUP_DIR="/data/mainrag/backups/phase13"
mkdir -p "$BACKUP_DIR"

# PostgreSQL backup
echo "Creating PostgreSQL backup..."
PGPASSWORD='<REDACTED_DB_PW>' pg_dump -h localhost -U mainrag -d mainrag \
    --format=custom --compress=9 \
    -f "$BACKUP_DIR/mainrag_pre_migration_$(date +%Y%m%d_%H%M%S).dump"
echo "✓ PostgreSQL backup created"

# 3. Data Validation
echo ""
echo "[3/4] Validating data..."

CHUNK_COUNT=$(PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag \
    -t -c "SELECT COUNT(*) FROM chunks" 2>/dev/null || echo "0")
EMBEDDING_COUNT=$(PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag \
    -t -c "SELECT COUNT(*) FROM chunk_embeddings" 2>/dev/null || echo "0")

echo "Current state:"
echo "  - Total chunks: $CHUNK_COUNT"
echo "  - Chunks with embeddings: $EMBEDDING_COUNT"

if [ "$CHUNK_COUNT" -eq 0 ]; then
    echo "WARNING: No chunks found in database"
elif [ "$EMBEDDING_COUNT" -lt $((CHUNK_COUNT / 2)) ]; then
    echo "WARNING: Only $EMBEDDING_COUNT of $CHUNK_COUNT chunks have embeddings"
fi

# 4. Qdrant Snapshot (optional, for reference)
echo ""
echo "[4/4] Optional: Creating Qdrant snapshot..."

if curl -sf http://localhost:6333/health > /dev/null 2>&1; then
    SNAPSHOT_FILE="$BACKUP_DIR/qdrant_snapshot_$(date +%Y%m%d_%H%M%S).tar.gz"
    if curl -X POST "http://localhost:6333/collections/mainrag_chunks/snapshots" \
        -H "api-key: <REDACTED_QDRANT_API_KEY>" \
        -o "$SNAPSHOT_FILE" 2>/dev/null; then
        echo "✓ Qdrant snapshot created at $SNAPSHOT_FILE"
    else
        echo "⚠ Qdrant snapshot failed (non-critical)"
    fi
else
    echo "⊘ Qdrant not available, skipping snapshot"
fi

echo ""
echo "=== Pre-Migration Checks Complete ==="
echo ""
echo "Backups stored in: $BACKUP_DIR"
echo "Review the data above before proceeding with migration."
echo ""
echo "Next steps:"
echo "1. Review the statistics above for any anomalies"
echo "2. If satisfied, run: bash ops/scripts/phase13-migrate.sh"
echo "3. If issues found, contact MAINRAG team"
