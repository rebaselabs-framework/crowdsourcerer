#!/usr/bin/env bash
# Project metrics — run to see current test counts, file counts, etc.
# Useful for verifying CLAUDE.md/MEMORY.md stats are accurate.
# Usage: bash scripts/metrics.sh
set -uo pipefail

cd "$(dirname "$0")/.."

echo "=== CrowdSorcerer Project Metrics ==="
echo ""

# Backend tests (grep for test function definitions)
BACKEND_TESTS=$(grep -r "def test_\|async def test_" apps/api/tests/ --include="*.py" 2>/dev/null | wc -l | tr -d ' ')
echo "Backend tests:    $BACKEND_TESTS"

# Backend test files
TEST_FILES=$(find apps/api/tests -name "test_*.py" 2>/dev/null | wc -l | tr -d ' ')
echo "Backend test files: $TEST_FILES"

# E2E tests
E2E_FILES=$(find e2e -name "*.spec.ts" -o -name "*.e2e.ts" 2>/dev/null | wc -l | tr -d ' ')
E2E_TESTS=$(grep -rh "test(" e2e/ --include="*.spec.ts" --include="*.e2e.ts" 2>/dev/null | wc -l | tr -d ' ')
echo "E2E test files:   $E2E_FILES"
echo "E2E test cases:   ~$E2E_TESTS"

# API routers
ROUTERS=$(find apps/api/routers -name "*.py" ! -name "__init__*" 2>/dev/null | wc -l | tr -d ' ')
echo "API routers:      $ROUTERS"

# Frontend pages
PAGES=$(find apps/web/src/pages -name "*.astro" 2>/dev/null | wc -l | tr -d ' ')
echo "Frontend pages:   $PAGES"

# Migrations
MIGRATIONS=$(find apps/api/alembic/versions -name "*.py" ! -name "__init__*" 2>/dev/null | wc -l | tr -d ' ')
echo "Migrations:       $MIGRATIONS"

# Lines of code (rough)
PY_LOC=$(find apps/api -name "*.py" ! -path "*/tests/*" ! -path "*/__pycache__/*" ! -path "*/alembic/*" ! -path "*/.venv/*" ! -path "*/site-packages/*" -exec cat {} + 2>/dev/null | wc -l | tr -d ' ')
ASTRO_LOC=$(find apps/web/src -name "*.astro" -exec cat {} + 2>/dev/null | wc -l | tr -d ' ')
TS_LOC=$(find apps/web/src packages -name "*.ts" ! -path "*/node_modules/*" ! -path "*/e2e/*" -exec cat {} + 2>/dev/null | wc -l | tr -d ' ')
echo ""
echo "Source LoC:"
echo "  Python (api):   $PY_LOC"
echo "  Astro:          $ASTRO_LOC"
echo "  TypeScript:     $TS_LOC"

echo ""
echo "Last commit: $(git log --oneline -1)"
echo "Branch:      $(git branch --show-current)"
echo "Uncommitted: $(git status --porcelain | wc -l | tr -d ' ') files"
