#!/bin/bash
# MainRAG Sync with Enterprise Telemetry
#
# Usage: sync-with-telemetry.sh <source_name>
#
# This script:
# 1. Resolves source name to ID
# 2. Starts telemetry monitor in background
# 3. Runs sync and captures output
# 4. Updates dashboard files in real-time
# 5. Shows final summary

set -euo pipefail

SOURCE_NAME="${1:-}"
TOOLS_DIR="$(dirname "$(readlink -f "$0")")"
TELEMETRY_LOG="/tmp/mainrag-telemetry.jsonl"
WEB_DIR="/work"  # nginx serves from /work at port 8000

if [[ -z "$SOURCE_NAME" ]]; then
    echo "Usage: $0 <source_name>"
    echo ""
    echo "Example: $0 kubernetes"
    exit 1
fi

echo "=============================================="
echo " MainRAG Enterprise Sync with Telemetry"
echo "=============================================="
echo ""

# Resolve source name to ID
echo "Resolving source '$SOURCE_NAME'..."
SOURCE_INFO=$(mainrag source list --json 2>/dev/null | jq -r --arg name "$SOURCE_NAME" '.sources[] | select(.name == $name) | "\(.id) \(.source_type) \(.path // "N/A")"' 2>/dev/null || echo "")

if [[ -z "$SOURCE_INFO" ]]; then
    echo "ERROR: Source '$SOURCE_NAME' not found."
    echo ""
    echo "Available sources:"
    mainrag source list 2>/dev/null | head -30
    exit 1
fi

SOURCE_ID=$(echo "$SOURCE_INFO" | awk '{print $1}')
SOURCE_TYPE=$(echo "$SOURCE_INFO" | awk '{print $2}')

echo "  Source ID: $SOURCE_ID"
echo "  Type: $SOURCE_TYPE"
echo ""

# Setup telemetry files
echo "Setting up telemetry..."
rm -f "$TELEMETRY_LOG"

# Copy dashboard to web directory
cp "$TOOLS_DIR/telemetry-dashboard.html" "$WEB_DIR/mainrag-telemetry.html"
ln -sf "$TELEMETRY_LOG" "$WEB_DIR/mainrag-telemetry.jsonl"

echo "  Dashboard: http://localhost:8000/mainrag-telemetry.html"
echo "  Log file: $TELEMETRY_LOG"
echo ""

# Start telemetry monitor in background
echo "Starting telemetry monitor..."
"$TOOLS_DIR/telemetry-monitor.sh" "$SOURCE_ID" "$TELEMETRY_LOG" 2 &
MONITOR_PID=$!
echo "  Monitor PID: $MONITOR_PID"
echo ""

# Cleanup function
cleanup() {
    echo ""
    echo "Stopping telemetry monitor..."
    kill $MONITOR_PID 2>/dev/null || true
    wait $MONITOR_PID 2>/dev/null || true

    # Final stats
    if [[ -f "$TELEMETRY_LOG" ]]; then
        echo ""
        echo "=============================================="
        echo " Final Telemetry Summary"
        echo "=============================================="
        LAST_LINE=$(tail -n 1 "$TELEMETRY_LOG" 2>/dev/null || echo "{}")
        if echo "$LAST_LINE" | jq -e '.pipeline' >/dev/null 2>&1; then
            echo "$LAST_LINE" | jq -r '"
  Elapsed: \(.elapsed_sec)s
  Files: \(.pipeline.files.count)
  Chunks: \(.pipeline.chunks.count)
  Symbols: \(.pipeline.symbols.count)
  Embeddings: \(.pipeline.embeddings.count)
  Peak CPU: \(.system.cpu_pct)%
  Peak MEM: \(.system.mem_pct)%
"'
        fi
    fi
}
trap cleanup EXIT

# Wait for monitor to start
sleep 2

echo "=============================================="
echo " Starting Sync: $SOURCE_NAME"
echo "=============================================="
echo ""

# Run sync with verbose output
START_TIME=$(date +%s)

if mainrag source sync "$SOURCE_NAME" --verbose 2>&1; then
    SYNC_STATUS="SUCCESS"
else
    SYNC_STATUS="FAILED"
fi

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo ""
echo "=============================================="
echo " Sync Complete: $SYNC_STATUS"
echo " Duration: ${DURATION}s"
echo "=============================================="

# Keep monitor running for a few more seconds to capture final state
echo ""
echo "Capturing final metrics..."
sleep 5
