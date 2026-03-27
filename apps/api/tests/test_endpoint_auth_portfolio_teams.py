"""Auth guard + input validation tests for portfolio, worker_teams,
notifications, and triggers endpoints.

Patterns:
- Auth-required endpoints return 401 without credentials.
- Public endpoints do NOT return 401.
- Input validation returns 422 (Pydantic) or 400 (manual checks).
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
    """Minimal mock DB that returns empty results for any query."""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    db.delete = AsyncMock()
    db.get = AsyncMock(return_value=None)  # db.get() is awaitable
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
    """Client with a mocked empty DB — lets requests reach endpoint logic."""
    from main import app
    from core.database import get_db
    mock_db = _make_empty_db()
    app.dependency_overrides[get_db] = _db_override(mock_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_db, None)


# ── Token helper ──────────────────────────────────────────────────────────────

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


def _task_id() -> str:
    return str(uuid.uuid4())


# ═══════════════════════════════════════════════════════════════════════════════
# portfolio.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_pin_task_requires_auth(client):
    r = await client.post("/v1/worker/portfolio", json={"task_id": _task_id()})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_my_portfolio_requires_auth(client):
    r = await client.get("/v1/worker/portfolio")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_update_pin_requires_auth(client):
    r = await client.patch(f"/v1/worker/portfolio/{_uid()}", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_pin_requires_auth(client):
    r = await client.delete(f"/v1/worker/portfolio/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_public_worker_portfolio_no_auth_needed(client_with_db):
    """GET /v1/workers/:id/portfolio is public — should NOT return 401."""
    r = await client_with_db.get(f"/v1/workers/{_uid()}/portfolio")
    assert r.status_code != 401


@pytest.mark.asyncio
async def test_pin_task_missing_task_id_is_422(client):
    """task_id is required in PinTaskRequest."""
    token = _make_token()
    r = await client.post(
        "/v1/worker/portfolio",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_pin_task_caption_too_long_is_422(client):
    """Caption has max_length=500."""
    token = _make_token()
    r = await client.post(
        "/v1/worker/portfolio",
        json={"task_id": _task_id(), "caption": "x" * 501},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_update_pin_caption_too_long_is_422(client):
    """Caption has max_length=500 on update too."""
    token = _make_token()
    r = await client.patch(
        f"/v1/worker/portfolio/{_uid()}",
        json={"caption": "y" * 501},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_pin_task_negative_display_order_is_422(client):
    """display_order must be >= 0."""
    token = _make_token()
    r = await client.post(
        "/v1/worker/portfolio",
        json={"task_id": _task_id(), "display_order": -1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_pin_task_valid_input_passes_pydantic(client_with_db):
    """Valid input: task_id + optional caption → passes Pydantic, hits DB (404)."""
    token = _make_token()
    r = await client_with_db.post(
        "/v1/worker/portfolio",
        json={"task_id": _task_id(), "caption": "My best work", "display_order": 0},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Should NOT be 422 — may be 404 (task not found)
    assert r.status_code != 422


# ═══════════════════════════════════════════════════════════════════════════════
# worker_teams.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_team_requires_auth(client):
    r = await client.post("/v1/worker-teams", json={"name": "My Team"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_teams_requires_auth(client):
    r = await client.get("/v1/worker-teams")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_pending_invites_requires_auth(client):
    r = await client.get("/v1/worker-teams/invites/pending")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_team_requires_auth(client):
    r = await client.get(f"/v1/worker-teams/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_team_requires_auth(client):
    r = await client.delete(f"/v1/worker-teams/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_invite_member_requires_auth(client):
    r = await client.post(
        f"/v1/worker-teams/{_uid()}/invite",
        json={"username": "someuser"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_accept_invite_requires_auth(client):
    r = await client.post(f"/v1/worker-teams/invites/{_uid()}/accept")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_decline_invite_requires_auth(client):
    r = await client.post(f"/v1/worker-teams/invites/{_uid()}/decline")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_remove_member_requires_auth(client):
    r = await client.delete(f"/v1/worker-teams/{_uid()}/members/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_assign_team_requires_auth(client):
    r = await client.post(
        f"/v1/tasks/{_task_id()}/assign-team",
        json={"team_id": _uid()},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unassign_team_requires_auth(client):
    r = await client.delete(f"/v1/tasks/{_task_id()}/assign-team")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_team_tasks_requires_auth(client):
    r = await client.get(f"/v1/worker-teams/{_uid()}/tasks")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_team_missing_name_is_422(client):
    """Team name is required."""
    token = _make_token()
    r = await client.post(
        "/v1/worker-teams",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_invite_member_missing_username_is_422(client):
    """username is required in invite request."""
    token = _make_token()
    r = await client.post(
        f"/v1/worker-teams/{_uid()}/invite",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_assign_team_missing_team_id_is_422(client):
    """team_id is required for assign-team."""
    token = _make_token()
    r = await client.post(
        f"/v1/tasks/{_task_id()}/assign-team",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# notifications.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_notifications_requires_auth(client):
    r = await client.get("/v1/notifications")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unread_count_requires_auth(client):
    r = await client.get("/v1/notifications/unread-count")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_mark_notification_read_requires_auth(client):
    r = await client.post(f"/v1/notifications/{_uid()}/read")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_mark_all_read_requires_auth(client):
    r = await client.post("/v1/notifications/read-all")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_grouped_notifications_requires_auth(client):
    r = await client.get("/v1/notifications/grouped")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_all_notifications_requires_auth(client):
    r = await client.delete("/v1/notifications/all")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_notification_requires_auth(client):
    r = await client.delete(f"/v1/notifications/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_notification_prefs_requires_auth(client):
    r = await client.get("/v1/notifications/preferences")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_put_notification_prefs_requires_auth(client):
    r = await client.put("/v1/notifications/preferences", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_notifications_bad_limit_is_422(client):
    """limit must be <= 100."""
    token = _make_token()
    r = await client.get(
        "/v1/notifications?limit=200",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_list_notifications_zero_limit_is_422(client):
    """limit must be >= 1."""
    token = _make_token()
    r = await client.get(
        "/v1/notifications?limit=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_grouped_notifications_bad_limit_is_422(client):
    """limit must be <= 200 for grouped endpoint."""
    token = _make_token()
    r = await client.get(
        "/v1/notifications/grouped?limit=300",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# triggers.py
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_trigger_requires_auth(client):
    r = await client.post(
        f"/v1/pipelines/{_uid()}/triggers",
        json={"trigger_type": "schedule"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_triggers_requires_auth(client):
    r = await client.get(f"/v1/pipelines/{_uid()}/triggers")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_trigger_requires_auth(client):
    r = await client.get(f"/v1/pipelines/triggers/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_update_trigger_requires_auth(client):
    r = await client.patch(f"/v1/pipelines/triggers/{_uid()}", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_trigger_requires_auth(client):
    r = await client.delete(f"/v1/pipelines/triggers/{_uid()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_fire_trigger_requires_auth(client):
    r = await client.post(f"/v1/pipelines/triggers/{_uid()}/fire")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_trigger_no_auth_needed(client_with_db):
    """POST /v1/pipelines/webhooks/:token is a public inbound webhook."""
    # Should not be 401 — may be 404 (token not found)
    r = await client_with_db.post(
        "/v1/pipelines/webhooks/some-webhook-token",
        json={"event": "test"},
    )
    assert r.status_code != 401


@pytest.mark.asyncio
async def test_create_trigger_missing_type_is_422(client):
    """trigger_type is required."""
    token = _make_token()
    r = await client.post(
        f"/v1/pipelines/{_uid()}/triggers",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_trigger_invalid_type_is_422(client):
    """trigger_type must match pattern ^(schedule|webhook)$."""
    token = _make_token()
    r = await client.post(
        f"/v1/pipelines/{_uid()}/triggers",
        json={"trigger_type": "manual"},  # not in allowed values
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_trigger_name_too_long_is_422(client):
    """name max_length=255."""
    token = _make_token()
    r = await client.post(
        f"/v1/pipelines/{_uid()}/triggers",
        json={"trigger_type": "schedule", "name": "x" * 256},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_trigger_valid_schedule_passes_pydantic(client_with_db):
    """Valid schedule trigger passes Pydantic, hits DB logic (may get 404/403)."""
    token = _make_token()
    r = await client_with_db.post(
        f"/v1/pipelines/{_uid()}/triggers",
        json={"trigger_type": "schedule", "cron_expression": "0 9 * * 1-5"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code != 422
