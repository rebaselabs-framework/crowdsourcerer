"""Basic smoke tests for the CrowdSorcerer API."""
import pytest
from httpx import AsyncClient, ASGITransport


@pytest.fixture
async def client():
    # Import here so DATABASE_URL env can be set first
    import os
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test"
    )
    os.environ.setdefault("JWT_SECRET", "test-secret")
    os.environ.setdefault("API_KEY_SALT", "test-salt")
    os.environ.setdefault("DEBUG", "true")

    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.asyncio
async def test_root(client):
    r = await client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert "name" in data
    assert "docs" in data


@pytest.mark.asyncio
async def test_docs_available(client):
    r = await client.get("/docs")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_unauthorized_tasks(client):
    r = await client.get("/v1/tasks")
    assert r.status_code == 403  # No auth header


@pytest.mark.asyncio
async def test_unauthorized_credits(client):
    r = await client.get("/v1/credits")
    assert r.status_code == 403
