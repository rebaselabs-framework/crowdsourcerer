"""Alembic migration environment — async SQLAlchemy + asyncpg."""
from __future__ import annotations
import asyncio
import os
import sys
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Ensure the api root is on sys.path so absolute imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# NOTE: We intentionally do NOT import models.db here.
# Importing it causes SQLAlchemy's Enum types (with create_type=True by default)
# to register event listeners that auto-emit CREATE TYPE on the first connection,
# conflicting with the explicit CREATE TYPE in migration scripts.

# Alembic config object (populated from alembic.ini)
config = context.config

# Pull DATABASE_URL from environment; override whatever is in alembic.ini
_db_url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@db:5432/crowdsourcerer")
config.set_main_option("sqlalchemy.url", _db_url)

# Configure Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use None for target_metadata — all migrations are manually authored.
# Passing Base.metadata causes SQLAlchemy to auto-create enum types before
# the migration scripts run, breaking migrations that CREATE TYPE explicitly.
target_metadata = None


# ─── Offline mode ────────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ─── Online mode (async) ──────────────────────────────────────────────────────

def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = config.get_main_option("sqlalchemy.url")

    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        echo=True,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
