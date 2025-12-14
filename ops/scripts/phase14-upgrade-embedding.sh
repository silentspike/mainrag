#!/bin/bash
# Phase 14: Embedding Model Upgrade
# Upgrades TEI embedding model with minimal downtime
# Usage: bash ops/scripts/phase14-upgrade-embedding.sh [model] [dimension]
# Examples:
#   bash ops/scripts/phase14-upgrade-embedding.sh nomic-ai/nomic-embed-text-v1.5 768
#   bash ops/scripts/phase14-upgrade-embedding.sh BAAI/bge-m3 1024

set -e

MODEL="${1:-nomic-ai/nomic-embed-text-v1.5}"
DIMENSION="${2:-768}"
DOCKER_COMPOSE_PATH="/opt/mainrag/docker-compose.yml"
BACKUP_DIR="/data/mainrag/backups/phase14"

echo "=== Phase 14: Embedding Model Upgrade ==="
echo "Target Model: $MODEL"
echo "Target Dimension: $DIMENSION"
echo ""

# Step 1: Pre-flight checks
echo "[1/7] Running pre-flight checks..."

if [ ! -f "$DOCKER_COMPOSE_PATH" ]; then
    echo "ERROR: docker-compose.yml not found at $DOCKER_COMPOSE_PATH"
    exit 1
fi

if ! docker ps > /dev/null 2>&1; then
    echo "ERROR: Docker is not accessible"
    exit 1
fi

echo "✓ Pre-flight checks passed"

# Step 2: Create backups
echo ""
echo "[2/7] Creating backups..."

mkdir -p "$BACKUP_DIR"

# Backup current docker-compose
cp "$DOCKER_COMPOSE_PATH" "$BACKUP_DIR/docker-compose-$(date +%Y%m%d_%H%M%S).yml.bak"

# Backup PostgreSQL
if PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag -c "SELECT 1" > /dev/null 2>&1; then
    PGPASSWORD='<REDACTED_DB_PW>' pg_dump -h localhost -U mainrag -d mainrag \
        --format=custom --compress=9 \
        -f "$BACKUP_DIR/mainrag_pre_phase14_$(date +%Y%m%d_%H%M%S).dump"
    echo "✓ PostgreSQL backup created"
else
    echo "⚠ Could not backup PostgreSQL (not required for model upgrade)"
fi

# Step 3: Stop API and TEI
echo ""
echo "[3/7] Stopping services..."

if systemctl is-active --quiet mainrag-api; then
    sudo systemctl stop mainrag-api
    echo "✓ API stopped"
fi

if [ -f "$DOCKER_COMPOSE_PATH" ]; then
    cd "$(dirname "$DOCKER_COMPOSE_PATH")"
    docker compose stop tei || true
    echo "✓ TEI stopped"
fi

sleep 2

# Step 4: Update docker-compose.yml
echo ""
echo "[4/7] Updating TEI configuration..."

# Backup original
cp "$DOCKER_COMPOSE_PATH" "$DOCKER_COMPOSE_PATH.pre-phase14"

# Update model ID in docker-compose
sed -i "s|--model-id=.*|--model-id=$MODEL|" "$DOCKER_COMPOSE_PATH"

# Also update max-batch-tokens if dimension changes
if [ "$DIMENSION" != "768" ]; then
    # For larger models, reduce batch tokens slightly
    sed -i "s|--max-batch-tokens=.*|--max-batch-tokens=8192|" "$DOCKER_COMPOSE_PATH"
fi

echo "✓ docker-compose.yml updated"

# Step 5: Pull new model and start TEI
echo ""
echo "[5/7] Starting TEI with new model..."
echo "Note: First startup may take several minutes while model is downloaded"

cd "$(dirname "$DOCKER_COMPOSE_PATH")"
docker compose up -d tei

# Wait for TEI to be healthy
echo "Waiting for TEI to load model..."
MAX_RETRIES=120
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -sf http://localhost:8080/health > /dev/null 2>&1; then
        echo "✓ TEI is ready"
        break
    fi

    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $((RETRY_COUNT % 10)) -eq 0 ]; then
        echo "  Still loading... (${RETRY_COUNT}s elapsed)"
    fi
    sleep 1
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "ERROR: TEI failed to start after $MAX_RETRIES seconds"
    echo "Reverting changes..."
    cd "$(dirname "$DOCKER_COMPOSE_PATH")"
    docker compose stop tei || true
    cp "$DOCKER_COMPOSE_PATH.pre-phase14" "$DOCKER_COMPOSE_PATH"
    docker compose up -d tei
    exit 1
fi

# Step 6: Verify model and dimension
echo ""
echo "[6/7] Verifying model and dimension..."

# Check model ID
ACTUAL_MODEL=$(curl -s http://localhost:8080/info 2>/dev/null | jq -r '.model_id' 2>/dev/null || echo "UNKNOWN")
echo "Loaded Model: $ACTUAL_MODEL"

if [ "$ACTUAL_MODEL" != "$MODEL" ]; then
    echo "ERROR: Model mismatch! Expected $MODEL, got $ACTUAL_MODEL"
    exit 1
fi

# Check embedding dimension
ACTUAL_DIM=$(curl -s -X POST http://localhost:8080/embed \
    -H "Content-Type: application/json" \
    -d '{"inputs": "test"}' 2>/dev/null | jq '.[0] | length' 2>/dev/null || echo "UNKNOWN")
echo "Embedding Dimension: $ACTUAL_DIM"

if [ "$ACTUAL_DIM" != "$DIMENSION" ]; then
    echo "ERROR: Dimension mismatch! Expected $DIMENSION, got $ACTUAL_DIM"
    exit 1
fi

echo "✓ Model and dimension verified"

# Step 7: Start API
echo ""
echo "[7/7] Starting API server..."

if [ "$DIMENSION" != "768" ]; then
    echo ""
    echo "⚠️  WARNING: Embedding dimension has changed!"
    echo "   Current: 768-dim"
    echo "   New:     ${DIMENSION}-dim"
    echo ""
    echo "   You must now:"
    echo "   1. Run schema migration: psql -f ops/migrations/14_embedding_dimension_update.sql"
    echo "   2. Re-index all sources: mainrag source list --json | jq -r '.[].id' | xargs -I{} mainrag source sync {}"
    echo ""
    echo "   Starting API in degraded mode (reindexing required)"
fi

sudo systemctl start mainrag-api

# Wait for API to be ready
RETRY_COUNT=0
MAX_RETRIES=30

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -sf http://localhost:3001/health > /dev/null 2>&1; then
        echo "✓ API is healthy"
        break
    fi

    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
        sleep 1
    fi
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "ERROR: API failed to start"
    exit 1
fi

# Summary
echo ""
echo "=== Phase 14: Model Upgrade Complete ==="
echo ""
echo "Model:    $ACTUAL_MODEL"
echo "Dimension: $ACTUAL_DIM"
echo ""

if [ "$DIMENSION" == "768" ]; then
    echo "✅ Drop-in upgrade complete! No re-indexing required."
    echo ""
    echo "Next steps:"
    echo "1. Test search functionality: curl http://localhost:3001/api/search -d '{\"query\": \"test\"}'"
    echo "2. Monitor API logs: journalctl -u mainrag-api -f"
    echo "3. Verify search quality vs previous model"
else
    echo "⚠️  Full upgrade in progress - Re-indexing required!"
    echo ""
    echo "Schema migration: psql -f ops/migrations/14_embedding_dimension_update.sql"
    echo "Re-index sources: mainrag source list --json | jq -r '.[].id' | xargs -I{} mainrag source sync {}"
fi

echo ""
echo "Rollback (if needed): cp \"$DOCKER_COMPOSE_PATH.pre-phase14\" \"$DOCKER_COMPOSE_PATH\" && docker compose up -d tei"
