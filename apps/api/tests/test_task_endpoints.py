"""Smoke tests for task-related API endpoints.

Verifies that auth-required endpoints reject unauthenticated requests with
401, and that publicly accessible endpoints return 200.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


def _make_empty_db() -> MagicMock:
    """Minimal mock DB that returns empty results for any query."""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()

    # Return empty scalars/count for any execute() call
    empty_result = MagicMock()
    empty_result.scalar_one_or_none = MagicMock(return_value=None)
    empty_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    # For count queries that return a scalar int
    db.execute = AsyncMock(return_value=empty_result)
    db.scalar = AsyncMock(return_value=0)
    return db


def _db_override(mock_db):
    async def _override():
        yield mock_db
    return _override


@pytest.fixture
async def client():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def app():
    from main import app as _app
    return _app


@pytest.mark.asyncio
async def test_tasks_list_requires_auth(client):
    """/v1/tasks should return 401 without credentials."""
    r = await client.get("/v1/tasks")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tasks_templates_is_public(client):
    """/v1/tasks/templates is a public endpoint — returns 200 without auth."""
    r = await client.get("/v1/tasks/templates")
    assert r.status_code == 200
    data = r.json()
    assert "templates" in data
    assert isinstance(data["templates"], list)


@pytest.mark.asyncio
async def test_tasks_public_returns_200(app):
    """/v1/tasks/public is open to everyone and should return 200 (empty feed)."""
    db = _make_empty_db()
    # The public feed does two execute calls: count + list
    # Both return "0 items" which is a valid empty feed response
    count_result = MagicMock()
    count_result.scalar_one_or_none = MagicMock(return_value=0)
    count_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    list_result = MagicMock()
    list_result.scalar_one_or_none = MagicMock(return_value=None)
    list_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    call_count = 0

    async def _side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return count_result
        return list_result

    db.execute.side_effect = _side_effect

    from core.database import get_db
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/tasks/public")
        assert r.status_code == 200
        data = r.json()
        assert "tasks" in data or "items" in data or isinstance(data, list) or "total" in data
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_tasks_post_requires_auth(client):
    """Creating a task without auth should return 401."""
    r = await client.post("/v1/tasks", json={"type": "llm_generate", "input": {"prompt": "hi"}})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tasks_single_requires_auth(client):
    """Fetching a specific task without auth should return 401."""
    r = await client.get("/v1/tasks/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 401
