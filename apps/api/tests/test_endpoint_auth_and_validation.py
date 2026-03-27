"""Auth guard + input validation tests for previously untested endpoints.

Tests use the ASGI app via httpx (no real DB required).
Each endpoint's auth guard is verified to return 401 without credentials.
Input validation errors return 422 (Pydantic) or 400 (manual checks).

Covered routers:
  payouts.py       — 6 endpoints
  comments.py      — 4 endpoints
  task_messages.py — 4 endpoints
  task_dependencies.py — 4 endpoints
  ratings.py       — 4 endpoints
  profiles.py      — 5 endpoints
  availability.py  — 5 endpoints
  ratings validation — payout_method, credits_requested, score range
  payout validation  — method+details combos
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


def _make_empty_db() -> MagicMock:
    """Minimal mock DB that returns empty results for any query."""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    db.delete = AsyncMock()
    db.get = AsyncMock(return_value=None)  # db.get() is awaitable
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


@pytest.fixture
async def client():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def client_with_db():
    """Client with a mocked empty DB — lets requests reach endpoint logic."""
    from main import app
    from core.database import get_db
    mock_db = _make_empty_db()
    app.dependency_overrides[get_db] = _db_override(mock_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_db, None)


# ── Helper ────────────────────────────────────────────────────────────────────

def _task_id() -> str:
    return str(uuid.uuid4())


def _uid() -> str:
    return str(uuid.uuid4())


def _make_token(user_id: str | None = None) -> str:
    """Create a JWT using whatever JWT_SECRET is actually active in the process."""
    from jose import jwt as _jwt
    import time
    secret = os.environ.get("JWT_SECRET", "app-test-secret")
    return _jwt.encode(
        {"sub": user_id or str(uuid.uuid4()), "exp": int(time.time()) + 300},
        secret,
        algorithm="HS256",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# payouts.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_payout_create_requires_auth(client):
    r = await client.post("/v1/payouts", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_payout_list_requires_auth(client):
    r = await client.get("/v1/payouts")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_payout_summary_requires_auth(client):
    r = await client.get("/v1/payouts/summary")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_payout_cancel_requires_auth(client):
    r = await client.delete(f"/v1/payouts/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_payout_admin_list_requires_auth(client):
    r = await client.get("/v1/payouts/admin/all")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_payout_admin_review_requires_auth(client):
    r = await client.post(f"/v1/payouts/{_uid()}/review", json={})
    assert r.status_code == 401


# Payout create input validation (Pydantic schema)
@pytest.mark.asyncio
async def test_payout_create_missing_body_is_422(client):
    """POST /v1/payouts without body → 422 (Pydantic validation)."""
    # We need a token to get past auth and test validation
    token = _make_token()
    r = await client.post(
        "/v1/payouts",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_payout_create_invalid_method_is_422(client):
    """Payout with unknown payout_method → 422 (Pydantic Literal validation)."""
    token = _make_token()
    r = await client.post(
        "/v1/payouts",
        json={
            "credits_requested": 2000,
            "payout_method": "bitcoin",   # not in Literal["paypal", "bank_transfer", "crypto"]
            "payout_details": {"address": "1abc"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_payout_create_below_minimum_is_400(client):
    """credits_requested < MIN_PAYOUT_CREDITS → 400."""
    token = _make_token()
    r = await client.post(
        "/v1/payouts",
        json={
            "credits_requested": 50,   # below 1000 minimum
            "payout_method": "paypal",
            "payout_details": {"email": "user@example.com"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_payout_create_paypal_missing_email_is_400(client):
    """PayPal payout without 'email' in details → 400."""
    token = _make_token()
    r = await client.post(
        "/v1/payouts",
        json={
            "credits_requested": 2000,
            "payout_method": "paypal",
            "payout_details": {"name": "John"},   # missing 'email'
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_payout_create_bank_transfer_missing_iban_is_400(client):
    """bank_transfer without iban → 400."""
    token = _make_token()
    r = await client.post(
        "/v1/payouts",
        json={
            "credits_requested": 2000,
            "payout_method": "bank_transfer",
            "payout_details": {"account_name": "John"},   # missing 'iban'
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_payout_create_crypto_missing_address_is_400(client):
    """crypto payout without address → 400."""
    token = _make_token()
    r = await client.post(
        "/v1/payouts",
        json={
            "credits_requested": 2000,
            "payout_method": "crypto",
            "payout_details": {"network": "ethereum"},   # missing 'address'
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# comments.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_comments_requires_auth(client):
    r = await client.get(f"/v1/tasks/{_task_id()}/comments")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_comment_requires_auth(client):
    r = await client.post(f"/v1/tasks/{_task_id()}/comments", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_edit_comment_requires_auth(client):
    r = await client.patch(f"/v1/tasks/{_task_id()}/comments/{_uid()}", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_comment_requires_auth(client):
    r = await client.delete(f"/v1/tasks/{_task_id()}/comments/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_comment_empty_body_is_422(client):
    """Comment body must be at least 1 character."""
    token = _make_token()
    r = await client.post(
        f"/v1/tasks/{_task_id()}/comments",
        json={"body": ""},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_post_comment_too_long_body_is_422(client):
    """Comment body max is 500 characters."""
    token = _make_token()
    r = await client.post(
        f"/v1/tasks/{_task_id()}/comments",
        json={"body": "x" * 501},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# task_messages.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_send_message_requires_auth(client):
    r = await client.post(f"/v1/tasks/{_task_id()}/messages", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_task_messages_requires_auth(client):
    r = await client.get(f"/v1/tasks/{_task_id()}/messages")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unread_message_count_requires_auth(client):
    r = await client.get("/v1/tasks/messages/unread-count")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_message_inbox_requires_auth(client):
    r = await client.get("/v1/tasks/messages/inbox")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_send_message_empty_body_is_422(client):
    """Message body must be at least 1 character."""
    token = _make_token()
    r = await client.post(
        f"/v1/tasks/{_task_id()}/messages",
        json={"body": "", "recipient_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_send_message_missing_recipient_is_422(client):
    """recipient_id is required."""
    token = _make_token()
    r = await client.post(
        f"/v1/tasks/{_task_id()}/messages",
        json={"body": "Hello!"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# task_dependencies.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_add_dependency_requires_auth(client):
    r = await client.post(f"/v1/tasks/{_task_id()}/dependencies", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_dependencies_requires_auth(client):
    r = await client.get(f"/v1/tasks/{_task_id()}/dependencies")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_dependents_requires_auth(client):
    r = await client.get(f"/v1/tasks/{_task_id()}/dependents")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_remove_dependency_requires_auth(client):
    r = await client.delete(f"/v1/tasks/{_task_id()}/dependencies/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_add_dependency_missing_upstream_is_422(client):
    """upstream_task_id is required."""
    token = _make_token()
    r = await client.post(
        f"/v1/tasks/{_task_id()}/dependencies",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# ratings.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rate_task_requires_auth(client):
    r = await client.post(f"/v1/tasks/{_task_id()}/rate", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_task_rating_requires_auth(client):
    r = await client.get(f"/v1/tasks/{_task_id()}/rating")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_my_ratings_requires_auth(client):
    r = await client.get("/v1/workers/me/ratings")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_public_worker_ratings_no_auth_needed(client_with_db):
    """Public endpoint: no auth required, just 404 (worker not found)."""
    r = await client_with_db.get(f"/v1/workers/{_uid()}/ratings")
    # Should be 404 (not found) or 200 with empty, NOT 401
    assert r.status_code != 401


@pytest.mark.asyncio
async def test_rate_task_missing_score_is_422(client):
    """Score is required for rating."""
    token = _make_token()
    r = await client.post(
        f"/v1/tasks/{_task_id()}/rate",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_rate_task_score_below_1_is_422(client):
    """Score must be >= 1."""
    token = _make_token()
    r = await client.post(
        f"/v1/tasks/{_task_id()}/rate",
        json={"score": 0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_rate_task_score_above_5_is_422(client):
    """Score must be <= 5."""
    token = _make_token()
    r = await client.post(
        f"/v1/tasks/{_task_id()}/rate",
        json={"score": 6},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# profiles.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_public_worker_profile_no_auth_needed(client_with_db):
    """GET /workers/:id/profile is public — should NOT return 401."""
    r = await client_with_db.get(f"/v1/workers/{_uid()}/profile")
    assert r.status_code != 401


@pytest.mark.asyncio
async def test_public_worker_task_stats_no_auth_needed(client_with_db):
    r = await client_with_db.get(f"/v1/workers/{_uid()}/task-stats")
    assert r.status_code != 401


@pytest.mark.asyncio
async def test_public_worker_recent_activity_no_auth_needed(client_with_db):
    r = await client_with_db.get(f"/v1/workers/{_uid()}/recent-activity")
    assert r.status_code != 401


@pytest.mark.asyncio
async def test_patch_my_profile_requires_auth(client):
    r = await client.patch("/v1/users/me/profile", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_profile_status_requires_auth(client):
    r = await client.get("/v1/users/me/profile-status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_patch_my_profile_bio_too_long_is_422(client):
    """Bio max length validation."""
    token = _make_token()
    r = await client.patch(
        "/v1/users/me/profile",
        json={"bio": "x" * 1001},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Either 422 (Pydantic) or 400 (manual) — not 401
    assert r.status_code in (400, 422)


# ═══════════════════════════════════════════════════════════════════════════════
# availability.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_availability_requires_auth(client):
    r = await client.get("/v1/worker/availability")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_put_availability_requires_auth(client):
    r = await client.put("/v1/worker/availability", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_worker_availability_requires_auth(client):
    """GET /worker/availability/worker/:id requires auth."""
    r = await client.get(f"/v1/worker/availability/worker/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_blackout_requires_auth(client):
    r = await client.post("/v1/worker/availability/blackouts", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_blackout_requires_auth(client):
    r = await client.delete(f"/v1/worker/availability/blackouts/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_blackout_missing_fields_is_422(client):
    """Blackout requires start_date and end_date."""
    token = _make_token()
    r = await client.post(
        "/v1/worker/availability/blackouts",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# Payout validation edge cases
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_payout_create_negative_credits_is_422(client):
    """credits_requested must be positive (Pydantic)."""
    token = _make_token()
    r = await client.post(
        "/v1/payouts",
        json={
            "credits_requested": -100,
            "payout_method": "paypal",
            "payout_details": {"email": "user@example.com"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    # Pydantic validation should reject negative credits
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
async def test_payout_create_valid_paypal_details_passes_validation(client_with_db):
    """Valid paypal payout with proper email passes method/detail validation."""
    token = _make_token()
    r = await client_with_db.post(
        "/v1/payouts",
        json={
            "credits_requested": 2000,
            "payout_method": "paypal",
            "payout_details": {"email": "worker@example.com"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    # Should NOT be 400 or 422 from our guards — may be 404 (user not found)
    assert r.status_code not in (400, 422)


@pytest.mark.asyncio
async def test_payout_create_valid_crypto_details_passes_validation(client_with_db):
    """Valid crypto payout with network+address passes method/detail validation."""
    token = _make_token()
    r = await client_with_db.post(
        "/v1/payouts",
        json={
            "credits_requested": 2000,
            "payout_method": "crypto",
            "payout_details": {"network": "ethereum", "address": "0xabc123"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code not in (400, 422)


# ═══════════════════════════════════════════════════════════════════════════════
# Rating score boundary tests (Pydantic)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rate_task_score_1_passes_pydantic(client_with_db):
    """Score=1 is the minimum valid value."""
    token = _make_token()
    r = await client_with_db.post(
        f"/v1/tasks/{_task_id()}/rate",
        json={"score": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Should NOT be 422 — may be 404 (task not found) or 403 (not the owner)
    assert r.status_code != 422


@pytest.mark.asyncio
async def test_rate_task_score_5_passes_pydantic(client_with_db):
    """Score=5 is the maximum valid value."""
    token = _make_token()
    r = await client_with_db.post(
        f"/v1/tasks/{_task_id()}/rate",
        json={"score": 5},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code != 422
