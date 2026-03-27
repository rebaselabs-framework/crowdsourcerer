"""Auth guard tests for the remaining smaller routers:
applications, two_factor, global_search, platform_stats, skills,
api_key_usage, analytics, orgs.

Pattern: 401 without credentials for protected endpoints.
         Public endpoints do NOT return 401.
"""
from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_dummy")

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, MagicMock


# ── Mock DB helpers ───────────────────────────────────────────────────────────

def _make_empty_db() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    db.delete = AsyncMock()
    db.get = AsyncMock(return_value=None)
    empty_result = MagicMock()
    empty_result.scalar_one_or_none = MagicMock(return_value=None)
    empty_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db.execute = AsyncMock(return_value=empty_result)
    db.scalar = AsyncMock(return_value=0)
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def client():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def client_with_db():
    from main import app
    from core.database import get_db
    mock_db = _make_empty_db()
    app.dependency_overrides[get_db] = _db_override(mock_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_db, None)


def _uid() -> str:
    return str(uuid.uuid4())


def _task_id() -> str:
    return str(uuid.uuid4())


# ═══════════════════════════════════════════════════════════════════════════════
# applications.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_apply_to_task_requires_auth(client):
    r = await client.post(f"/v1/tasks/{_task_id()}/apply", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_task_applications_requires_auth(client):
    r = await client.get(f"/v1/tasks/{_task_id()}/applications")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_accept_application_requires_auth(client):
    r = await client.post(
        f"/v1/tasks/{_task_id()}/applications/{_uid()}/accept"
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_reject_application_requires_auth(client):
    r = await client.post(
        f"/v1/tasks/{_task_id()}/applications/{_uid()}/reject"
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_withdraw_application_requires_auth(client):
    r = await client.delete(f"/v1/tasks/{_task_id()}/applications")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_worker_applications_requires_auth(client):
    r = await client.get("/v1/worker/applications")
    assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# two_factor.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_2fa_status_requires_auth(client):
    r = await client.get("/v1/auth/2fa/status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_2fa_setup_requires_auth(client):
    r = await client.post("/v1/auth/2fa/setup")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_2fa_enable_requires_auth(client):
    r = await client.post("/v1/auth/2fa/enable", json={"code": "123456"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_2fa_disable_requires_auth(client):
    r = await client.post("/v1/auth/2fa/disable", json={"code": "123456"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_2fa_verify_no_bearer_auth_needed(client_with_db):
    """POST /v1/auth/2fa/verify uses pending_token in body — no Bearer auth header.
    When called with an invalid pending_token the endpoint returns 401 with a
    message about the token, NOT the standard 'Not authenticated' 401 from
    missing Bearer credentials."""
    r = await client_with_db.post(
        "/v1/auth/2fa/verify",
        json={"pending_token": "invalid-token", "code": "123456"},
    )
    # Endpoint is reachable without Bearer (may get 401 for bad token, but not
    # the HTTPBearer "Not authenticated" 403/401 that other endpoints return)
    # Verify the error is about the pending_token, not missing auth header
    if r.status_code == 401:
        body = r.json()
        detail = str(body.get("detail", "")).lower()
        # Standard Bearer auth failure returns exactly "Not authenticated"
        # This endpoint fails with "Invalid or expired 2FA session" or similar
        assert detail != "not authenticated"


# ═══════════════════════════════════════════════════════════════════════════════
# global_search.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_global_search_requires_auth(client):
    r = await client.get("/v1/search/global?q=test")
    assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# platform_stats.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_platform_stats_no_auth_needed(client_with_db):
    """GET /v1/platform/stats is public — should NOT return 401."""
    r = await client_with_db.get("/v1/platform/stats")
    assert r.status_code != 401


# ═══════════════════════════════════════════════════════════════════════════════
# skills.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_my_skills_requires_auth(client):
    r = await client.get("/v1/workers/me/skills")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_recommended_skills_requires_auth(client):
    r = await client.get("/v1/workers/me/recommended")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_worker_verified_skills_no_auth_needed(client_with_db):
    """GET /v1/workers/:id/verified-skills is public."""
    r = await client_with_db.get(f"/v1/workers/{_uid()}/verified-skills")
    assert r.status_code != 401


# ═══════════════════════════════════════════════════════════════════════════════
# api_key_usage.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_api_key_usage_overview_requires_auth(client):
    r = await client.get("/v1/api-keys/usage/overview")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_api_key_usage_by_key_requires_auth(client):
    r = await client.get(f"/v1/api-keys/{_uid()}/usage")
    assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# analytics.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_analytics_overview_requires_auth(client):
    r = await client.get("/v1/analytics/overview")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analytics_org_requires_auth(client):
    r = await client.get(f"/v1/analytics/org/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analytics_costs_requires_auth(client):
    r = await client.get("/v1/analytics/costs")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analytics_export_requires_auth(client):
    r = await client.get("/v1/analytics/export")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analytics_completion_times_requires_auth(client):
    r = await client.get("/v1/analytics/completion-times")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analytics_revenue_requires_auth(client):
    r = await client.get("/v1/analytics/revenue")
    assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# orgs.py  (organisation management)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_org_requires_auth(client):
    r = await client.post("/v1/orgs", json={"name": "My Org"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_orgs_requires_auth(client):
    r = await client.get("/v1/orgs")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_org_requires_auth(client):
    r = await client.get(f"/v1/orgs/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_update_org_requires_auth(client):
    r = await client.patch(f"/v1/orgs/{_uid()}", json={"name": "Updated"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_org_requires_auth(client):
    r = await client.delete(f"/v1/orgs/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_org_members_requires_auth(client):
    r = await client.get(f"/v1/orgs/{_uid()}/members")
    assert r.status_code == 401
