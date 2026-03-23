#!/bin/bash
# CrowdSorcerer API entrypoint — run migrations then start server
set -e

echo "==> Running database migrations..."
alembic upgrade head

echo "==> Starting CrowdSorcerer API..."
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 8100 \
    --workers 1 \
    --log-level info
