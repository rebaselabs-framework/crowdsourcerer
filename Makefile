.PHONY: dev test deploy build lint check api-dev web-dev install setup-hooks e2e e2e-smoke e2e-local

# Dev
dev:
	docker compose up --build

api-dev:
	cd apps/api && uvicorn main:app --reload --port 8100

web-dev:
	cd apps/web && pnpm dev

# Install
install:
	pnpm install
	pip install -r apps/api/requirements.txt

# Install git hooks (run once after cloning)
# Installs BOTH pre-commit (catches errors before commit) and pre-push (last-line-of-defense before remote push)
setup-hooks:
	cp scripts/pre-commit .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit
	cp scripts/pre-push .git/hooks/pre-push
	chmod +x .git/hooks/pre-push
	@echo "✅ Git hooks installed (pre-commit + pre-push)"

# Build
build:
	pnpm build
	docker compose build

# Test
# Uses uv run to avoid needing pytest in system PATH
test:
	cd apps/api && uv run pytest tests/ -v
	pnpm test

# Quick API test (no frontend, faster)
test-api:
	cd apps/api && uv run pytest tests/ -q

# Lint
lint:
	cd apps/api && ruff check . && mypy .
	pnpm lint

# STRICT CHECK — run this before every commit/deploy
# Catches Astro syntax errors, TypeScript errors, missing imports, and build failures
# This is mandatory: if check fails, do NOT commit or deploy
check:
	@echo "=== Astro type check ==="
	cd apps/web && pnpm exec astro check
	@echo "=== Astro build ==="
	cd apps/web && pnpm build
	@echo "=== All checks passed ✅ ==="

# Deploy (push triggers Coolify webhook)
# ALWAYS runs 'make check' first — build errors must never reach prod
deploy:
	$(MAKE) check
	git push origin main

# DB migrations
migrate:
	cd apps/api && alembic upgrade head

migrate-create:
	cd apps/api && alembic revision --autogenerate -m "$(msg)"

# Logs
logs:
	docker compose logs -f api

# Reset local dev DB
db-reset:
	docker compose down -v
	docker compose up -d db
	sleep 2
	$(MAKE) migrate

# E2E tests (Playwright) — runs against live deployment by default
# Override target with: E2E_BASE_URL=http://localhost:4321 make e2e
e2e:
	npx playwright test --reporter=list

# Quick smoke test only
e2e-smoke:
	npx playwright test e2e/smoke.spec.ts --reporter=list

# E2E against local docker-compose (must run `make dev` first)
e2e-local:
	E2E_BASE_URL=http://localhost:4321 npx playwright test --reporter=list
