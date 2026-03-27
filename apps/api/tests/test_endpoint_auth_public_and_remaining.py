"""Auth guard + input validation tests for the remaining routers:
auth (register/login), challenges, leaderboard, credits, and admin.

Highlights:
- Register/login are public (no 401), but validate input (422).
- Leaderboard is public.
- Challenges are auth-required.
- Credits balance/transactions/checkout are auth-required.
- Admin routes require auth + admin flag.
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
    db.refresh = AsyncMock()  # db.refresh() is awaitable
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


def _make_token(user_id: str | None = None) -> str:
    from jose import jwt as _jwt
    import time
    secret = os.environ.get("JWT_SECRET", "app-test-secret")
    return _jwt.encode(
        {"sub": user_id or str(uuid.uuid4()), "exp": int(time.time()) + 300},
        secret,
        algorithm="HS256",
    )


def _uid() -> str:
    return str(uuid.uuid4())


# ═══════════════════════════════════════════════════════════════════════════════
# auth.py — public endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_register_missing_body_is_422(client):
    """POST /v1/auth/register with empty body → 422 (Pydantic).
    The endpoint is reachable without Bearer auth; Pydantic validates before rate-limiting.
    """
    r = await client.post("/v1/auth/register", json={})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_login_missing_body_is_422(client):
    """POST /v1/auth/login with empty body → 422 (Pydantic).
    The endpoint is reachable without Bearer auth.
    """
    r = await client.post("/v1/auth/login", json={})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_missing_email_is_422(client):
    """email is required for registration."""
    r = await client.post(
        "/v1/auth/register",
        json={"password": "securepassword123"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_invalid_email_is_422(client):
    """email must be valid format."""
    r = await client.post(
        "/v1/auth/register",
        json={"email": "not-an-email", "password": "securepassword123"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_short_password_is_422(client):
    """password must be at least 8 characters."""
    r = await client.post(
        "/v1/auth/register",
        json={"email": "test@example.com", "password": "short"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_invalid_role_is_422(client):
    """role must be 'requester' or 'worker'."""
    r = await client.post(
        "/v1/auth/register",
        json={
            "email": "test@example.com",
            "password": "securepassword123",
            "role": "admin",  # not in Literal["requester", "worker"]
        },
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_login_missing_credentials_is_422(client):
    """Both email and password are required for login."""
    r = await client.post("/v1/auth/login", json={})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_forgot_password_missing_email_is_422(client):
    """Forgot-password with no email → 422 (Pydantic, no Bearer auth needed)."""
    r = await client.post("/v1/auth/forgot-password", json={})
    # The endpoint is reachable without a Bearer token
    # With missing email it returns 422 (Pydantic) before any rate-limit
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# challenges.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_today_challenge_requires_auth(client):
    r = await client.get("/v1/challenges/today")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_claim_challenge_requires_auth(client):
    r = await client.post("/v1/challenges/today/claim")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_challenge_history_requires_auth(client):
    r = await client.get("/v1/challenges/history")
    assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# leaderboard.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_leaderboard_no_auth_needed(client_with_db):
    """GET /v1/leaderboard is public."""
    r = await client_with_db.get("/v1/leaderboard")
    assert r.status_code != 401


@pytest.mark.asyncio
async def test_leaderboard_invalid_category_is_422(client):
    """category must be xp|tasks|earnings."""
    r = await client.get("/v1/leaderboard?category=reputation")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_leaderboard_invalid_period_is_422(client):
    """period must be all_time|weekly."""
    r = await client.get("/v1/leaderboard?period=monthly")
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# credits.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_credit_balance_requires_auth(client):
    r = await client.get("/v1/credits")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_credit_transactions_requires_auth(client):
    r = await client.get("/v1/credits/transactions")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_credit_checkout_requires_auth(client):
    r = await client.post(
        "/v1/credits/checkout",
        json={
            "credits": 1000,
            "success_url": "https://example.com/success",
            "cancel_url": "https://example.com/cancel",
        },
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_credit_checkout_below_minimum_is_422(client):
    """credits must be >= 100."""
    token = _make_token()
    r = await client.post(
        "/v1/credits/checkout",
        json={
            "credits": 50,  # below minimum
            "success_url": "https://example.com/success",
            "cancel_url": "https://example.com/cancel",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_credit_checkout_above_maximum_is_422(client):
    """credits must be <= 100000."""
    token = _make_token()
    r = await client.post(
        "/v1/credits/checkout",
        json={
            "credits": 200000,  # above maximum
            "success_url": "https://example.com/success",
            "cancel_url": "https://example.com/cancel",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_credit_checkout_invalid_url_is_422(client):
    """success_url and cancel_url must be valid HTTP URLs."""
    token = _make_token()
    r = await client.post(
        "/v1/credits/checkout",
        json={
            "credits": 1000,
            "success_url": "not-a-url",
            "cancel_url": "https://example.com/cancel",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_credit_transactions_page_size_too_large_is_422(client):
    """page_size max is 100."""
    token = _make_token()
    r = await client.get(
        "/v1/credits/transactions?page_size=200",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# admin.py — a few key endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_admin_stats_requires_auth(client):
    r = await client.get("/v1/admin/stats")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_list_users_requires_auth(client):
    r = await client.get("/v1/admin/users")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_get_user_requires_auth(client):
    r = await client.get(f"/v1/admin/users/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_patch_user_requires_auth(client):
    """PATCH /v1/admin/users/:id requires admin auth."""
    r = await client.patch(f"/v1/admin/users/{_uid()}", json={"is_banned": True})
    assert r.status_code == 401
