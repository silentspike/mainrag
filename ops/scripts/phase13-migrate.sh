#!/bin/bash
# Phase 13: Migration to pgvector-only Architecture
# Stops API, optimizes pgvector index, deploys new API version, performs health checks
# Usage: bash ops/scripts/phase13-migrate.sh

set -e

echo "=== Phase 13: Migration to pgvector-only Architecture ==="
echo ""

# Configuration
API_BINARY="/work/mainrag/api/target/release/mainrag-api"
API_INSTALL_PATH="/opt/mainrag/api/mainrag-api"
API_BACKUP_PATH="/opt/mainrag/api/mainrag-api.backup.pre-phase13"
MIGRATION_TIMEOUT_SECONDS=300

# Step 1: Backup current API
echo "[1/6] Backing up current API binary..."
if [ -f "$API_INSTALL_PATH" ]; then
    cp "$API_INSTALL_PATH" "$API_BACKUP_PATH"
    echo "✓ Backed up to: $API_BACKUP_PATH"
else
    echo "⚠ Current API binary not found at $API_INSTALL_PATH"
fi

# Step 2: Stop API server
echo ""
echo "[2/6] Stopping API server..."
if systemctl is-active --quiet mainrag-api; then
    sudo systemctl stop mainrag-api
    sleep 2
    echo "✓ API server stopped"
else
    echo "⊘ API server not running"
fi

# Step 3: Optimize pgvector Index
echo ""
echo "[3/6] Optimizing pgvector HNSW index..."
echo "This may take several minutes for large datasets..."

PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag << 'PGEOF'
BEGIN;

-- Drop existing index if it exists
DROP INDEX IF EXISTS idx_chunk_embeddings_vector;
DROP INDEX IF EXISTS idx_chunk_embeddings_vector_hnsw;

-- Create optimized HNSW index with higher parameters
CREATE INDEX idx_chunk_embeddings_vector_hnsw
ON chunk_embeddings
USING hnsw (vector vector_cosine_ops)
WITH (m = 24, ef_construction = 256);

-- Runtime parameters
SET hnsw.ef_search = 100;

-- Analyze statistics for query planner
ANALYZE chunk_embeddings;

COMMIT;
PGEOF

echo "✓ pgvector index optimized"

# Step 4: Deploy new API binary
echo ""
echo "[4/6] Deploying pgvector-only API binary..."

if [ ! -f "$API_BINARY" ]; then
    echo "ERROR: API binary not found at $API_BINARY"
    echo "Please run: cargo build --release in /work/mainrag/api"
    exit 1
fi

cp "$API_BINARY" "$API_INSTALL_PATH"
echo "✓ New API binary deployed"

# Step 5: Start API server
echo ""
echo "[5/6] Starting API server..."
sudo systemctl start mainrag-api

# Wait for API to be ready
echo "Waiting for API to be healthy..."
RETRY_COUNT=0
MAX_RETRIES=30
WAIT_SECONDS=1

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -sf http://localhost:3001/health > /dev/null 2>&1; then
        echo "✓ API is healthy"
        break
    fi

    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
        echo "  Waiting... ($RETRY_COUNT/$MAX_RETRIES)"
        sleep $WAIT_SECONDS
    fi
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "ERROR: API failed to become healthy after $((MAX_RETRIES * WAIT_SECONDS)) seconds"
    echo "Rolling back..."
    sudo systemctl stop mainrag-api
    if [ -f "$API_BACKUP_PATH" ]; then
        cp "$API_BACKUP_PATH" "$API_INSTALL_PATH"
        sudo systemctl start mainrag-api
    fi
    exit 1
fi

# Step 6: Health check
echo ""
echo "[6/6] Running comprehensive health checks..."

# Check API health
HEALTH_RESPONSE=$(curl -s http://localhost:3001/health)
POSTGRES_OK=$(echo "$HEALTH_RESPONSE" | grep -q '"postgres":true' && echo "true" || echo "false")
TEI_OK=$(echo "$HEALTH_RESPONSE" | grep -q '"tei":true' && echo "true" || echo "false")

if [ "$POSTGRES_OK" != "true" ] || [ "$TEI_OK" != "true" ]; then
    echo "ERROR: Health check failed"
    echo "Response: $HEALTH_RESPONSE"
    exit 1
fi

echo "✓ All services healthy"

# Final verification
echo ""
echo "=== Migration Complete ==="
echo ""
echo "System Status:"
echo "  - API: running (pgvector-only mode)"
echo "  - PostgreSQL: $(PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag -t -c "SELECT COUNT(*) FROM chunks" 2>/dev/null) chunks indexed"
echo "  - Embeddings: $(PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag -t -c "SELECT COUNT(*) FROM chunk_embeddings" 2>/dev/null) embeddings in pgvector"
echo ""
echo "Next steps:"
echo "1. Monitor logs: journalctl -u mainrag-api -f --no-pager"
echo "2. Run post-migration tests: bash ops/scripts/phase13-post-migration.sh"
echo "3. Verify application functionality in staging"
echo ""
echo "To rollback (if needed): bash ops/scripts/phase13-rollback.sh"
