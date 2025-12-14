#!/bin/bash
# Phase 13: Rollback to Hybrid Architecture
# Restores previous API binary and restarts Qdrant if migration fails
# Usage: bash ops/scripts/phase13-rollback.sh

set -e

echo "=== Phase 13: ROLLBACK to Hybrid Architecture ==="
echo ""
echo "⚠️  WARNING: This will revert to the hybrid (pgvector + Qdrant) architecture"
echo ""

# Configuration
API_BACKUP_PATH="/opt/mainrag/api/mainrag-api.backup.pre-phase13"
API_INSTALL_PATH="/opt/mainrag/api/mainrag-api"
BACKUP_DIR="/data/mainrag/backups/phase13"

# Step 1: Verify backup exists
echo "[1/4] Verifying backups..."

if [ ! -f "$API_BACKUP_PATH" ]; then
    echo "ERROR: Backup not found at $API_BACKUP_PATH"
    echo "Cannot proceed with rollback"
    exit 1
fi

echo "✓ API backup found"

if [ ! -d "$BACKUP_DIR" ]; then
    echo "WARNING: Pre-migration backup directory not found"
    echo "Database state may need manual restoration"
fi

# Step 2: Stop API
echo ""
echo "[2/4] Stopping API server..."

if systemctl is-active --quiet mainrag-api; then
    sudo systemctl stop mainrag-api
    sleep 2
    echo "✓ API server stopped"
else
    echo "⊘ API server not running"
fi

# Step 3: Restore previous binary
echo ""
echo "[3/4] Restoring previous API binary..."

cp "$API_BACKUP_PATH" "$API_INSTALL_PATH"
echo "✓ Previous binary restored from: $API_BACKUP_PATH"

# Step 4: Start services
echo ""
echo "[4/4] Starting services..."

# Start Qdrant first
if systemctl is-active --quiet qdrant; then
    echo "✓ Qdrant already running"
else
    echo "Starting Qdrant..."
    sudo systemctl start qdrant
    sleep 3

    if ! curl -sf http://localhost:6333/health > /dev/null; then
        echo "ERROR: Qdrant failed to start"
        exit 1
    fi
    echo "✓ Qdrant started"
fi

# Start API
echo "Starting API..."
sudo systemctl start mainrag-api

# Wait for API to be ready
echo "Waiting for API to be healthy..."
RETRY_COUNT=0
MAX_RETRIES=30

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -sf http://localhost:3001/health > /dev/null 2>&1; then
        echo "✓ API is healthy"
        break
    fi

    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
        echo "  Waiting... ($RETRY_COUNT/$MAX_RETRIES)"
        sleep 1
    fi
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "ERROR: API failed to become healthy"
    exit 1
fi

# Verify Qdrant health
if ! curl -sf http://localhost:6333/health > /dev/null; then
    echo "ERROR: Qdrant health check failed"
    exit 1
fi

echo "✓ Qdrant healthy"

# Final status
echo ""
echo "=== Rollback Complete ==="
echo ""
echo "System Status:"
echo "  - API: running (hybrid mode with pgvector + Qdrant)"
echo "  - PostgreSQL: Available"
echo "  - Qdrant: Running"
echo "  - TEI: Available"
echo ""
echo "Important Notes:"
echo "1. System is back in HYBRID mode (pgvector + Qdrant)"
echo "2. Data consistency between pgvector and Qdrant should be verified"
echo "3. Review logs for any errors: journalctl -u mainrag-api -f"
echo "4. If you had issues, check pre-migration backups in: $BACKUP_DIR"
echo ""
echo "To restore database from backup:"
echo "  PGPASSWORD='<REDACTED_DB_PW>' pg_restore -h localhost -U mainrag -d mainrag -v <backup_file>"
echo ""
echo "For issues or questions, contact the MAINRAG team"
