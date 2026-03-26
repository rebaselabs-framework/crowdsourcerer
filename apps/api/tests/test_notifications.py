"""Tests for the notifications router.

Covers:
  1.  GET /v1/notifications — unauthenticated → 401/403
  2.  GET /v1/notifications — happy path: returns list with total/unread_count
  3.  GET /v1/notifications?unread_only=true — filters to unread notifications
  4.  GET /v1/notifications/unread-count — returns unread count
  5.  POST /v1/notifications/{id}/read — marks notification as read
  6.  POST /v1/notifications/{id}/read — not found → 404
  7.  POST /v1/notifications/read-all — marks all read, returns {"ok": True}
  8.  DELETE /v1/notifications/{id} — deletes notification
  9.  DELETE /v1/notifications/{id} — not found → 404
  10. DELETE /v1/notifications/all — deletes all notifications
  11. GET /v1/notifications/preferences — no row exists → default prefs dict
  12. GET /v1/notifications/preferences — existing row → prefs dict from DB
  13. PUT /v1/notifications/preferences — updates fields on existing row
  14. PUT /v1/notifications/preferences — no existing row → creates new one
  15. GET /v1/notifications/grouped — groups notifications by type category
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── IDs ───────────────────────────────────────────────────────────────────────

USER_ID   = str(uuid.uuid4())
NOTIF_ID  = str(uuid.uuid4())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _make_notification(
    notif_id: str | None = None,
    notif_type: str = "SYSTEM",
    title: str = "Test notification",
    body: str = "Hello",
    link: str | None = "/worker",
    is_read: bool = False,
) -> MagicMock:
    n = MagicMock(spec=[
        "id", "type", "title", "body", "link", "is_read", "created_at",
    ])
    n.id         = uuid.UUID(notif_id) if notif_id else uuid.uuid4()
    n.type       = notif_type       # plain str — Pydantic reads this directly
    n.title      = title
    n.body       = body
    n.link       = link
    n.is_read    = is_read
    n.created_at = _now()
    return n


def _make_prefs(
    user_id: str | None = None,
    digest_frequency: str = "weekly",
) -> MagicMock:
    p = MagicMock()
    p.user_id                  = uuid.UUID(user_id) if user_id else uuid.uuid4()
    p.email_task_completed     = True
    p.email_task_failed        = True
    p.email_submission_received = True
    p.email_worker_approved    = True
    p.email_payout_update      = True
    p.email_daily_challenge    = False
    p.email_task_available     = False
    p.email_referral_bonus     = True
    p.email_sla_breach         = True
    p.notif_task_events        = True
    p.notif_submissions        = True
    p.notif_payouts            = True
    p.notif_gamification       = True
    p.notif_system             = True
    p.digest_frequency         = digest_frequency
    p.updated_at               = _now()
    return p


def _make_db() -> MagicMock:
    db          = MagicMock()
    db.add      = MagicMock()
    db.execute  = AsyncMock()
    db.commit   = AsyncMock()
    db.delete   = AsyncMock()

    async def _refresh(obj):
        pass
    db.refresh = _refresh
    return db


def _scalar_result(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one         = MagicMock(return_value=value)
    r.scalars            = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=[value] if value is not None else []))
    )
    return r


def _scalars_result(items: list) -> MagicMock:
    r = MagicMock()
    r.scalar_one         = MagicMock(return_value=len(items))
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalars            = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=items))
    )
    return r


def _db_override(mock_db):
    async def _override():
        yield mock_db
    return _override


def _real_token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


# ── Auth guard ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_notifications_unauthenticated():
    """No token → 401 or 403."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/notifications")
    assert r.status_code in (401, 403)


# ── List notifications ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_notifications_happy_path():
    """GET /v1/notifications — returns items, total, unread_count."""
    from main import app
    from core.database import get_db

    notif = _make_notification(notif_id=NOTIF_ID, is_read=False)
    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            # total count
            return _scalar_result(1)
        if call_num[0] == 2:
            # unread count
            return _scalar_result(1)
        # items
        return _scalars_result([notif])

    db.execute.side_effect = _side_effect
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/notifications",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "total" in body
        assert "unread_count" in body
        assert body["total"] == 1
        assert body["unread_count"] == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_notifications_unread_only():
    """GET /v1/notifications?unread_only=true — endpoint returns 200."""
    from main import app
    from core.database import get_db

    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] <= 2:
            return _scalar_result(0)
        return _scalars_result([])

    db.execute.side_effect = _side_effect
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/notifications?unread_only=true",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
    finally:
        app.dependency_overrides.clear()


# ── Unread count ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_unread_count():
    """GET /v1/notifications/unread-count — returns unread_count field."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(7)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/notifications/unread-count",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        assert r.json()["unread_count"] == 7
    finally:
        app.dependency_overrides.clear()


# ── Mark read ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_read_happy_path():
    """POST /v1/notifications/{id}/read — marks notification as read, returns 200."""
    from main import app
    from core.database import get_db

    notif = _make_notification(notif_id=NOTIF_ID, is_read=False)
    db = _make_db()
    db.execute.return_value = _scalar_result(notif)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/notifications/{NOTIF_ID}/read",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        # Notification is_read should be set to True by the endpoint
        assert notif.is_read is True
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_mark_read_not_found():
    """POST /v1/notifications/{id}/read — notification not found → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(None)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/notifications/{uuid.uuid4()}/read",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ── Mark all read ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_all_read():
    """POST /v1/notifications/read-all — returns {"ok": True}."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = MagicMock()   # UPDATE result
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/notifications/read-all",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        db.commit.assert_called_once()
    finally:
        app.dependency_overrides.clear()


# ── Delete notification ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_notification_happy_path():
    """DELETE /v1/notifications/{id} — deletes and returns {"ok": True}."""
    from main import app
    from core.database import get_db

    notif = _make_notification(notif_id=NOTIF_ID)
    db = _make_db()
    db.execute.return_value = _scalar_result(notif)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(
                f"/v1/notifications/{NOTIF_ID}",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        db.delete.assert_called_once_with(notif)
        db.commit.assert_called_once()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_notification_not_found():
    """DELETE /v1/notifications/{id} — not found → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(None)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(
                f"/v1/notifications/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ── Delete all ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_all_notifications():
    """DELETE /v1/notifications/all — returns {"ok": True}, commits."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = MagicMock()
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(
                "/v1/notifications/all",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        db.commit.assert_called_once()
    finally:
        app.dependency_overrides.clear()


# ── Grouped notifications ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_grouped_notifications_empty():
    """GET /v1/notifications/grouped — no notifications → groups list is empty."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalars_result([])
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/notifications/grouped",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert "groups" in body
        assert "total_unread" in body
        assert body["groups"] == []
        assert body["total_unread"] == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_grouped_notifications_groups_by_type():
    """Notifications are grouped into the correct category buckets."""
    from main import app
    from core.database import get_db

    n1 = _make_notification(notif_type="BADGE_EARNED", title="Badge!", is_read=False)
    n2 = _make_notification(notif_type="PAYOUT_COMPLETED", title="Paid!", is_read=True)
    n3 = _make_notification(notif_type="SYSTEM", title="System msg", is_read=False)

    db = _make_db()
    db.execute.return_value = _scalars_result([n1, n2, n3])
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/notifications/grouped",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        group_types = {g["type"] for g in body["groups"]}
        assert "gamification" in group_types   # BADGE_EARNED
        assert "payout" in group_types         # PAYOUT_COMPLETED
        assert "system" in group_types         # SYSTEM catch-all
        # total_unread: n1 (unread) + n3 (unread) = 2
        assert body["total_unread"] == 2
    finally:
        app.dependency_overrides.clear()


# ── Notification preferences ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_preferences_default_when_no_row():
    """GET /v1/notifications/preferences — no DB row → returns factory defaults."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(None)   # no prefs row
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/notifications/preferences",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        # Defaults: email_task_completed=True, digest_frequency="weekly"
        assert body["email_task_completed"] is True
        assert body["digest_frequency"] == "weekly"
        assert "notif_gamification" in body
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_preferences_existing_row():
    """GET /v1/notifications/preferences — existing row → returns its values."""
    from main import app
    from core.database import get_db

    prefs = _make_prefs(digest_frequency="daily")
    db = _make_db()
    db.execute.return_value = _scalar_result(prefs)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/notifications/preferences",
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["digest_frequency"] == "daily"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_preferences_creates_row_when_missing():
    """PUT /v1/notifications/preferences — no existing row → db.add() called."""
    from main import app
    from core.database import get_db

    prefs = _make_prefs()
    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(None)   # no existing prefs
        return _scalar_result(prefs)

    db.execute.side_effect = _side_effect
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.put(
                "/v1/notifications/preferences",
                json={"digest_frequency": "daily"},
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        db.add.assert_called_once()   # created a new prefs row
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_preferences_updates_existing():
    """PUT /v1/notifications/preferences — existing row → fields updated, no db.add."""
    from main import app
    from core.database import get_db

    prefs = _make_prefs(digest_frequency="weekly")
    db = _make_db()
    db.execute.return_value = _scalar_result(prefs)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.put(
                "/v1/notifications/preferences",
                json={"digest_frequency": "none", "email_daily_challenge": True},
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        db.add.assert_not_called()           # no new row
        db.commit.assert_called_once()
        # Fields were set on the prefs object
        assert prefs.digest_frequency == "none"
        assert prefs.email_daily_challenge is True
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_preferences_ignores_invalid_digest_frequency():
    """PUT /v1/notifications/preferences — invalid digest_frequency is silently ignored."""
    from main import app
    from core.database import get_db

    prefs = _make_prefs(digest_frequency="weekly")
    db = _make_db()
    db.execute.return_value = _scalar_result(prefs)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.put(
                "/v1/notifications/preferences",
                json={"digest_frequency": "hourly"},   # invalid
                headers={"Authorization": f"Bearer {_real_token(USER_ID)}"},
            )
        assert r.status_code == 200
        # digest_frequency was NOT updated (invalid value ignored)
        assert prefs.digest_frequency == "weekly"
    finally:
        app.dependency_overrides.clear()
