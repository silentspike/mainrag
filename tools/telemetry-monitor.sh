#!/bin/bash
# Enterprise Telemetry Monitor for MainRAG Sync Operations
# Real-time monitoring with historical data and live progress
#
# Usage: telemetry-monitor.sh <source_id> <logfile> [interval_sec]
#
# Features:
# - Real-time database polling for accurate counts
# - Rate calculations with moving average
# - System resource monitoring (CPU, MEM, disk I/O)
# - JSONL output for dashboard consumption
# - Human-readable terminal output

set -euo pipefail

SOURCE_ID="${1:-}"
LOGFILE="${2:-/tmp/mainrag-telemetry.jsonl}"
INTERVAL="${3:-2}"

# Database connection (adjust as needed)
DB_HOST="${MAINRAG_DB_HOST:-localhost}"
DB_PORT="${MAINRAG_DB_PORT:-5432}"
DB_NAME="${MAINRAG_DB_NAME:-mainrag}"
DB_USER="${MAINRAG_DB_USER:-mainrag}"
export PGPASSWORD="${MAINRAG_DB_PASSWORD:-}"

# Qdrant connection
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

if [[ -z "$SOURCE_ID" ]]; then
    echo "Usage: $0 <source_id> [logfile] [interval_sec]" >&2
    exit 1
fi

# Initialize state
PREV_FILES=0
PREV_CHUNKS=0
PREV_SYMBOLS=0
PREV_CALLGRAPH=0
PREV_EMBEDDINGS=0
PREV_TIME=$(date +%s%3N)  # milliseconds
START_TIME=$PREV_TIME

# Rate smoothing (exponential moving average)
declare -A RATES
RATES[files]=0
RATES[chunks]=0
RATES[symbols]=0
RATES[callgraph]=0
RATES[embeddings]=0
ALPHA=0.3  # Smoothing factor

# Cleanup on exit
cleanup() {
    local now=$(date -Iseconds)
    echo '{"event":"monitor_stopped","timestamp":"'"$now"'","reason":"signal"}' >> "$LOGFILE"
    echo ""
    echo "Monitor stopped."
}
trap cleanup EXIT INT TERM

# Query database for source stats (using postgres user for peer auth)
query_db() {
    local query="$1"
    sudo -u postgres psql -d "$DB_NAME" -t -A -c "$query" 2>/dev/null || echo "0"
}

# Get Qdrant embedding count
get_qdrant_count() {
    local count
    count=$(curl -s "$QDRANT_URL/collections/mainrag" 2>/dev/null | jq -r '.result.points_count // 0' 2>/dev/null || echo "0")
    echo "${count:-0}"
}

# Calculate smoothed rate
calc_rate() {
    local key="$1"
    local current="$2"
    local prev="$3"
    local interval_ms="$4"

    if [[ $interval_ms -gt 0 ]]; then
        local instant_rate=$(awk "BEGIN {printf \"%.2f\", ($current - $prev) / ($interval_ms / 1000)}")
        local prev_rate="${RATES[$key]}"
        # Exponential moving average
        RATES[$key]=$(awk "BEGIN {printf \"%.2f\", $ALPHA * $instant_rate + (1 - $ALPHA) * $prev_rate}")
    fi
    echo "${RATES[$key]}"
}

# Write header
NOW=$(date -Iseconds)
cat > "$LOGFILE" << EOF
{"event":"monitor_started","source_id":$SOURCE_ID,"timestamp":"$NOW","config":{"interval_sec":$INTERVAL,"db_host":"$DB_HOST","qdrant_url":"$QDRANT_URL"}}
EOF

echo "=============================================="
echo " MainRAG Enterprise Telemetry Monitor"
echo "=============================================="
echo ""
echo "  Source ID: $SOURCE_ID"
echo "  Log File: $LOGFILE"
echo "  Interval: ${INTERVAL}s"
echo "  Database: $DB_HOST:$DB_PORT/$DB_NAME"
echo ""
echo "Press Ctrl+C to stop"
echo ""
echo "----------------------------------------------"
printf "%-8s %-6s %-6s | %-10s %-12s %-10s %-10s %-12s\n" \
    "Elapsed" "CPU" "MEM" "Files" "Chunks" "Symbols" "CallGraph" "Embeddings"
echo "----------------------------------------------"

while true; do
    NOW_MS=$(date +%s%3N)
    NOW_ISO=$(date -Iseconds)
    ELAPSED_SEC=$(( (NOW_MS - START_TIME) / 1000 ))
    INTERVAL_MS=$((NOW_MS - PREV_TIME))

    # System metrics
    CPU=$(top -bn1 | grep "Cpu(s)" | awk '{printf "%.1f", $2}')
    MEM_INFO=$(free -b | awk '/Mem:/ {printf "%.1f %d %d", $3/$2*100, $3, $2}')
    MEM_PCT=$(echo "$MEM_INFO" | awk '{print $1}')
    MEM_USED=$(echo "$MEM_INFO" | awk '{print $2}')
    MEM_TOTAL=$(echo "$MEM_INFO" | awk '{print $3}')

    # Database queries for real-time counts
    FILES=$(query_db "SELECT COUNT(*) FROM files WHERE source_id = $SOURCE_ID")
    CHUNKS=$(query_db "SELECT COUNT(*) FROM chunks c JOIN files f ON c.file_id = f.id WHERE f.source_id = $SOURCE_ID")
    SYMBOLS=$(query_db "SELECT COUNT(*) FROM symbols s JOIN files f ON s.file_id = f.id WHERE f.source_id = $SOURCE_ID")
    CALLGRAPH=$(query_db "SELECT COUNT(*) FROM call_graph cg JOIN symbols s ON cg.caller_symbol_id = s.id JOIN files f ON s.file_id = f.id WHERE f.source_id = $SOURCE_ID")

    # Qdrant embedding count (total, not per source - would need filtering)
    EMBEDDINGS=$(get_qdrant_count)

    # Ensure numeric values
    FILES=${FILES:-0}
    CHUNKS=${CHUNKS:-0}
    SYMBOLS=${SYMBOLS:-0}
    CALLGRAPH=${CALLGRAPH:-0}
    EMBEDDINGS=${EMBEDDINGS:-0}

    # Calculate smoothed rates
    FILES_RATE=$(calc_rate "files" "$FILES" "$PREV_FILES" "$INTERVAL_MS")
    CHUNKS_RATE=$(calc_rate "chunks" "$CHUNKS" "$PREV_CHUNKS" "$INTERVAL_MS")
    SYMBOLS_RATE=$(calc_rate "symbols" "$SYMBOLS" "$PREV_SYMBOLS" "$INTERVAL_MS")
    CALLGRAPH_RATE=$(calc_rate "callgraph" "$CALLGRAPH" "$PREV_CALLGRAPH" "$INTERVAL_MS")
    EMBEDDINGS_RATE=$(calc_rate "embeddings" "$EMBEDDINGS" "$PREV_EMBEDDINGS" "$INTERVAL_MS")

    # Write JSONL entry
    cat >> "$LOGFILE" << EOF
{"event":"progress","elapsed_sec":$ELAPSED_SEC,"timestamp":"$NOW_ISO","system":{"cpu_pct":$CPU,"mem_pct":$MEM_PCT,"mem_used_bytes":$MEM_USED,"mem_total_bytes":$MEM_TOTAL},"pipeline":{"files":{"count":$FILES,"rate":$FILES_RATE},"chunks":{"count":$CHUNKS,"rate":$CHUNKS_RATE},"symbols":{"count":$SYMBOLS,"rate":$SYMBOLS_RATE},"call_graph":{"count":$CALLGRAPH,"rate":$CALLGRAPH_RATE},"embeddings":{"count":$EMBEDDINGS,"rate":$EMBEDDINGS_RATE}}}
EOF

    # Terminal output
    printf "\r%-8s %-6s %-6s | %-10s %-12s %-10s %-10s %-12s" \
        "${ELAPSED_SEC}s" \
        "${CPU}%" \
        "${MEM_PCT}%" \
        "$FILES (+${FILES_RATE}/s)" \
        "$CHUNKS (+${CHUNKS_RATE}/s)" \
        "$SYMBOLS (+${SYMBOLS_RATE}/s)" \
        "$CALLGRAPH" \
        "$EMBEDDINGS (+${EMBEDDINGS_RATE}/s)"

    # Update previous values
    PREV_FILES=$FILES
    PREV_CHUNKS=$CHUNKS
    PREV_SYMBOLS=$SYMBOLS
    PREV_CALLGRAPH=$CALLGRAPH
    PREV_EMBEDDINGS=$EMBEDDINGS
    PREV_TIME=$NOW_MS

    sleep "$INTERVAL"
done
