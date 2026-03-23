.PHONY: dev test deploy build lint api-dev web-dev install

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

# Build
build:
	pnpm build
	docker compose build

# Test
test:
	cd apps/api && pytest tests/ -v
	pnpm test

# Lint
lint:
	cd apps/api && ruff check . && mypy .
	pnpm lint

# Deploy (push triggers Coolify webhook)
deploy:
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
