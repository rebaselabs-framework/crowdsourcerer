#!/usr/bin/env bash
# Quick production health check — run at start of every session
# Usage: bash scripts/healthcheck.sh
set -euo pipefail

BASE="https://crowdsourcerer.rebaselabs.online"
PASS=0
FAIL=0
WARN=0

check() {
    local label="$1" url="$2" expect="${3:-200}"
    code=$(curl -s -o /dev/null -w "%{http_code}" "$url" --max-time 10 2>/dev/null || echo "000")
    if [ "$code" = "$expect" ]; then
        PASS=$((PASS+1))
    else
        echo "  ❌ $label: HTTP $code (expected $expect)"
        FAIL=$((FAIL+1))
    fi
}

check_json() {
    local label="$1" url="$2" field="$3"
    val=$(curl -s "$url" --max-time 10 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$field','MISSING'))" 2>/dev/null || echo "ERROR")
    if [ "$val" = "MISSING" ] || [ "$val" = "ERROR" ]; then
        echo "  ❌ $label: field '$field' missing or error"
        FAIL=$((FAIL+1))
    else
        PASS=$((PASS+1))
    fi
}

echo "=== CrowdSorcerer Production Health Check ==="
echo ""

# Core health
echo "── Core ──"
check "Landing page" "$BASE/"
check "Health endpoint" "$BASE/v1/health"
check_json "Database" "$BASE/v1/health" "database"
check "API root" "$BASE/v1/tasks/public"
check "OpenAPI spec" "$BASE/v1/openapi-spec"

# Auth flow
echo "── Auth ──"
REG_RESP=$(curl -s -X POST "$BASE/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"hc-$(date +%s)@example.com\",\"password\":\"TestPass123!\",\"role\":\"worker\"}" 2>/dev/null)
AT=$(echo "$REG_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
RT=$(echo "$REG_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('refresh_token',''))" 2>/dev/null)
if [ -n "$AT" ] && [ "$AT" != "None" ] && [ "$AT" != "" ]; then
    PASS=$((PASS+1))
else
    echo "  ❌ Registration: no access token returned"
    FAIL=$((FAIL+1))
fi
if [ -n "$RT" ] && [ "$RT" != "None" ] && [ "$RT" != "" ]; then
    # Test refresh
    REFRESH=$(curl -s -X POST "$BASE/v1/auth/refresh" \
      -H "Content-Type: application/json" \
      -d "{\"refresh_token\":\"$RT\"}" 2>/dev/null)
    NEW_AT=$(echo "$REFRESH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
    if [ -n "$NEW_AT" ] && [ "$NEW_AT" != "" ]; then
        PASS=$((PASS+1))
    else
        echo "  ❌ Token refresh: failed"
        FAIL=$((FAIL+1))
    fi
else
    echo "  ❌ Registration: no refresh token returned"
    FAIL=$((FAIL+1))
fi

# Key pages (spot check)
echo "── Pages ──"
check "Marketplace" "$BASE/marketplace"
check "Leaderboard" "$BASE/leaderboard"
check "Docs" "$BASE/docs"
check "API Reference" "$BASE/docs/api-reference"
check "Pricing" "$BASE/pricing"

# RebaseKit services
echo "── RebaseKit ──"
RK_UP=0
RK_DOWN=0
for svc in pii llm webtask enrich web storage validate watermark; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "https://api.rebaselabs.online/$svc/api/health" --max-time 5 2>/dev/null || echo "000")
    if [ "$code" = "200" ] || [ "$code" = "401" ]; then
        RK_UP=$((RK_UP+1))
    else
        RK_DOWN=$((RK_DOWN+1))
    fi
done
echo "  RebaseKit: $RK_UP up, $RK_DOWN down"
if [ "$RK_DOWN" -gt 0 ]; then
    WARN=$((WARN+1))
fi

# Summary
echo ""
echo "=== Results: $PASS passed, $FAIL failed, $WARN warnings ==="
if [ "$FAIL" -gt 0 ]; then
    echo "⚠️  FAILURES DETECTED — investigate before doing other work"
    exit 1
fi
echo "✅ All checks passed"
