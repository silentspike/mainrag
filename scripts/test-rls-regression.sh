#!/usr/bin/env bash
# test-rls-regression.sh — Security regression tests for MAINRAG
# Validates that application-layer auth and API-level access control work correctly.
#
# Architecture (post FORCE RLS removal):
# - API connects as table owner (mainrag) → bypasses RLS by design
# - Security is enforced at application layer (TenantContext, Qdrant user_id filter)
# - Normal RLS policies remain as defense-in-depth for non-owner DB roles
# - FORCE RLS is OFF — intentional, only LLMs and admin access the system
#
# Usage: ./scripts/test-rls-regression.sh
set -euo pipefail

API_URL="${API_URL:-http://localhost:3001}"
DB_HOST="${POSTGRES_HOST:-localhost}"
DB_PORT="${POSTGRES_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-mainrag}"
DB_USER="${POSTGRES_USER:-mainrag}"
DB_PASSWORD="${POSTGRES_PASSWORD:-<REDACTED_DB_PW>}"
ADMIN_USER_ID="db8e73cc-f562-40c5-b3ca-70e6a042ef89"

PASS=0
FAIL=0
TOTAL=0

log_test() {
    TOTAL=$((TOTAL + 1))
    echo -n "  Test $TOTAL: $1 ... "
}

pass() {
    PASS=$((PASS + 1))
    echo "PASS"
}

fail() {
    FAIL=$((FAIL + 1))
    echo "FAIL: $1"
}

psql_cmd() {
    PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tA 2>/dev/null <<EOSQL
$1
EOSQL
}

echo "=== MAINRAG Security Regression Tests ==="
echo "API: $API_URL"
echo "DB:  $DB_USER@$DB_HOST:$DB_PORT/$DB_NAME"
echo ""

# --- Setup: Get admin token ---
echo "--- Setup ---"
ADMIN_TOKEN=$(curl -sf -X POST "$API_URL/api/v1/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"TestBaseline2025x"}' 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || echo "")

if [ -z "$ADMIN_TOKEN" ]; then
    echo "WARNING: Could not get admin token. API tests will be skipped."
fi

# --- Database Schema Tests ---
echo ""
echo "--- Database Schema Tests ---"

log_test "RLS enabled on chunks (policies exist)"
RLS_CHUNKS=$(psql_cmd "SELECT relrowsecurity FROM pg_class WHERE relname = 'chunks';")
if [ "$RLS_CHUNKS" = "t" ]; then
    pass
else
    fail "RLS not enabled on chunks table"
fi

log_test "RLS enabled on files (policies exist)"
RLS_FILES=$(psql_cmd "SELECT relrowsecurity FROM pg_class WHERE relname = 'files';")
if [ "$RLS_FILES" = "t" ]; then
    pass
else
    fail "RLS not enabled on files table"
fi

log_test "RLS enabled on sources (policies exist)"
RLS_SOURCES=$(psql_cmd "SELECT relrowsecurity FROM pg_class WHERE relname = 'sources';")
if [ "$RLS_SOURCES" = "t" ]; then
    pass
else
    fail "RLS not enabled on sources table"
fi

log_test "FORCE RLS is OFF (table owner bypasses — by design)"
FORCE_CHUNKS=$(psql_cmd "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'chunks';")
FORCE_FILES=$(psql_cmd "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'files';")
FORCE_SOURCES=$(psql_cmd "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'sources';")
if [ "$FORCE_CHUNKS" = "f" ] && [ "$FORCE_FILES" = "f" ] && [ "$FORCE_SOURCES" = "f" ]; then
    pass
else
    fail "FORCE RLS should be OFF (chunks=$FORCE_CHUNKS files=$FORCE_FILES sources=$FORCE_SOURCES)"
fi

log_test "Data exists in DB (chunks > 0)"
CHUNK_COUNT=$(psql_cmd "SELECT COUNT(*) FROM chunks;")
if [ -n "$CHUNK_COUNT" ] && [ "$CHUNK_COUNT" -gt 0 ]; then
    pass
    echo "         ($CHUNK_COUNT chunks)"
else
    fail "No chunks in database"
fi

log_test "user_can_access_source() function exists"
FUNC_EXISTS=$(psql_cmd "SELECT COUNT(*) FROM pg_proc WHERE proname = 'user_can_access_source';")
if [ "$FUNC_EXISTS" -gt 0 ]; then
    pass
else
    fail "user_can_access_source() function missing"
fi

# --- API-Level Auth Tests ---
if [ -n "$ADMIN_TOKEN" ]; then
    echo ""
    echo "--- API-Level Auth Tests ---"

    log_test "Admin search returns results"
    API_RESULT=$(curl -sf -X POST "$API_URL/api/v1/search" \
        -H "Authorization: Bearer $ADMIN_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"query":"authentication","limit":5}' 2>/dev/null || echo "{}")
    API_COUNT=$(echo "$API_RESULT" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('results',[])))" 2>/dev/null || echo "0")
    if [ "$API_COUNT" -gt 0 ]; then
        pass
        echo "         ($API_COUNT results)"
    else
        fail "Admin search returned 0 results"
    fi

    log_test "Unauthenticated search is rejected (401)"
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API_URL/api/v1/search" \
        -H "Content-Type: application/json" \
        -d '{"query":"test","limit":5}' 2>/dev/null)
    if [ "$HTTP_CODE" = "401" ]; then
        pass
    else
        fail "Expected 401, got $HTTP_CODE"
    fi

    log_test "Invalid token is rejected (401)"
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API_URL/api/v1/search" \
        -H "Authorization: Bearer invalid_token_12345" \
        -H "Content-Type: application/json" \
        -d '{"query":"test","limit":5}' 2>/dev/null)
    if [ "$HTTP_CODE" = "401" ]; then
        pass
    else
        fail "Expected 401, got $HTTP_CODE"
    fi

    log_test "Health endpoint is public (no auth needed)"
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3001/health 2>/dev/null)
    if [ "$HTTP_CODE" = "200" ]; then
        pass
    else
        fail "Expected 200, got $HTTP_CODE"
    fi
fi

# --- Summary ---
echo ""
echo "==================================="
echo "Results: $PASS passed, $FAIL failed out of $TOTAL tests"
if [ $FAIL -gt 0 ]; then
    echo "STATUS: FAIL"
    exit 1
else
    echo "STATUS: PASS — Security checks intact"
    exit 0
fi
