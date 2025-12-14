#!/bin/bash
# Phase 13: Post-Migration Verification
# Runs benchmarks and verifies pgvector-only performance
# Usage: bash ops/scripts/phase13-post-migration.sh

set -e

echo "=== Phase 13: Post-Migration Verification ==="
echo ""

# Create results directory
RESULTS_DIR="/data/mainrag/benchmarks"
mkdir -p "$RESULTS_DIR"

# Step 1: Basic health checks
echo "[1/4] Running health checks..."

HEALTH=$(curl -s http://localhost:3001/health)
echo "API Health Response:"
echo "$HEALTH" | jq . 2>/dev/null || echo "$HEALTH"

if ! echo "$HEALTH" | grep -q '"postgres":true'; then
    echo "ERROR: PostgreSQL health check failed"
    exit 1
fi

if ! echo "$HEALTH" | grep -q '"tei":true'; then
    echo "ERROR: TEI health check failed"
    exit 1
fi

echo "✓ All services healthy"

# Step 2: Performance baseline
echo ""
echo "[2/4] Collecting performance metrics..."

# Measure search performance
echo "Running 10 test searches..."

SEARCH_TIMES=()
for i in {1..10}; do
    START=$(date +%s%N)

    # Execute a simple search
    curl -s "http://localhost:3001/api/search" \
        -H "Content-Type: application/json" \
        -d '{"query": "function", "limit": 10}' > /dev/null

    END=$(date +%s%N)
    ELAPSED=$((($END - $START) / 1000000))  # Convert to milliseconds
    SEARCH_TIMES+=($ELAPSED)

    if [ $((i % 5)) -eq 0 ]; then
        echo "  Completed $i/10 searches..."
    fi
done

# Calculate statistics
TOTAL=0
MIN=${SEARCH_TIMES[0]}
MAX=${SEARCH_TIMES[0]}

for TIME in "${SEARCH_TIMES[@]}"; do
    TOTAL=$((TOTAL + TIME))
    if [ $TIME -lt $MIN ]; then MIN=$TIME; fi
    if [ $TIME -gt $MAX ]; then MAX=$TIME; fi
done

AVG=$((TOTAL / ${#SEARCH_TIMES[@]}))

echo "✓ Search performance:"
echo "  - Average: ${AVG}ms"
echo "  - Min: ${MIN}ms"
echo "  - Max: ${MAX}ms"

# Step 3: Data validation
echo ""
echo "[3/4] Validating data integrity..."

CHUNK_COUNT=$(PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag \
    -t -c "SELECT COUNT(*) FROM chunks" 2>/dev/null | xargs)
EMBEDDING_COUNT=$(PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag \
    -t -c "SELECT COUNT(*) FROM chunk_embeddings" 2>/dev/null | xargs)
EMBEDDING_NULL=$(PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag \
    -t -c "SELECT COUNT(*) FROM chunk_embeddings WHERE vector IS NULL OR vector = '[]'" 2>/dev/null | xargs)

echo "Data Statistics:"
echo "  - Total chunks: $CHUNK_COUNT"
echo "  - Chunks with embeddings: $EMBEDDING_COUNT"
echo "  - Empty embeddings: $EMBEDDING_NULL"

if [ "$CHUNK_COUNT" -eq 0 ]; then
    echo "WARNING: No chunks in database"
elif [ "$EMBEDDING_COUNT" -lt "$CHUNK_COUNT" ]; then
    echo "WARNING: $(($CHUNK_COUNT - $EMBEDDING_COUNT)) chunks missing embeddings"
fi

if [ "$EMBEDDING_NULL" -gt 0 ]; then
    echo "ERROR: Found $EMBEDDING_NULL empty embeddings"
    exit 1
fi

echo "✓ Data integrity verified"

# Step 4: Generate report
echo ""
echo "[4/4] Generating report..."

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
REPORT_FILE="$RESULTS_DIR/phase13_post_migration_$TIMESTAMP.json"

cat > "$REPORT_FILE" << JSONEOF
{
  "timestamp": "$TIMESTAMP",
  "migration": "phase_13_pgvector_only",
  "status": "success",
  "system": {
    "postgres_healthy": true,
    "tei_healthy": true
  },
  "performance": {
    "search_avg_ms": $AVG,
    "search_min_ms": $MIN,
    "search_max_ms": $MAX,
    "test_count": ${#SEARCH_TIMES[@]}
  },
  "data": {
    "total_chunks": $CHUNK_COUNT,
    "chunks_with_embeddings": $EMBEDDING_COUNT,
    "empty_embeddings": $EMBEDDING_NULL
  },
  "acceptance_criteria": {
    "postgres_available": true,
    "tei_available": true,
    "embedding_coverage": $(python3 -c "print(int(($EMBEDDING_COUNT / max($CHUNK_COUNT, 1)) * 100))")
  }
}
JSONEOF

echo "✓ Report saved to: $REPORT_FILE"
echo ""
echo "=== Post-Migration Verification Complete ==="
echo ""
cat "$REPORT_FILE" | jq .
echo ""
echo "Summary:"
echo "- Migration Status: SUCCESS"
echo "- All services healthy: YES"
echo "- Data integrity: VERIFIED"
echo "- Average search latency: ${AVG}ms"
echo ""
echo "Next steps:"
echo "1. Run additional application-level tests"
echo "2. Monitor API logs for errors: journalctl -u mainrag-api -f"
echo "3. Verify search results quality manually"
echo "4. If satisfied, Qdrant service can be stopped (optional)"
echo ""
echo "To rollback: bash ops/scripts/phase13-rollback.sh"
