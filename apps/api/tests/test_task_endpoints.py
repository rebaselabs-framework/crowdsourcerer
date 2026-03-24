"""Smoke tests for task-related API endpoints.

Verifies that auth-required endpoints reject unauthenticated requests with
401, and that publicly accessible endpoints return 200.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


@pytest.fixture
async def client():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_tasks_list_requires_auth(client):
    """/v1/tasks should return 401 without credentials."""
    r = await client.get("/v1/tasks")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tasks_templates_requires_auth(client):
    """/v1/tasks/templates should return 401 without credentials."""
    r = await client.get("/v1/tasks/templates")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tasks_public_returns_200(client):
    """/v1/tasks/public is open to everyone and should return 200."""
    r = await client.get("/v1/tasks/public")
    assert r.status_code == 200


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
