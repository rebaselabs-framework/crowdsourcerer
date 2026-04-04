#!/bin/bash
# CrowdSorcerer API entrypoint — run migrations then start server

echo "==> Running database migrations..."
if alembic upgrade head; then
    echo "==> Migrations complete."
else
    echo "!!! Migration failed (exit code $?). Starting server anyway for diagnostics."
    echo "!!! Check DATABASE_URL and migration files."
fi

echo "==> Starting CrowdSorcerer API..."
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 8100 \
    --workers 1 \
    --log-level info
