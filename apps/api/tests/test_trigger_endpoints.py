"""Tests for the triggers router (pipeline schedule & webhook triggers).

Covers:
  1.  POST   /v1/pipelines/{pid}/triggers         — unauthenticated → 401
  2.  POST   /v1/pipelines/{pid}/triggers         — create schedule trigger → 201
  3.  POST   /v1/pipelines/{pid}/triggers         — create webhook trigger → 201
  4.  POST   /v1/pipelines/{pid}/triggers         — pipeline not found → 404
  5.  POST   /v1/pipelines/{pid}/triggers         — schedule without cron → 422
  6.  POST   /v1/pipelines/{pid}/triggers         — invalid cron expression → 422
  7.  POST   /v1/pipelines/{pid}/triggers         — invalid trigger_type → 422
  8.  POST   /v1/pipelines/{pid}/triggers         — schedule trigger sets next_fire_at
  9.  POST   /v1/pipelines/{pid}/triggers         — webhook trigger generates webhook_url
  10. GET    /v1/pipelines/{pid}/triggers         — unauthenticated → 401
  11. GET    /v1/pipelines/{pid}/triggers         — happy path returns list
  12. GET    /v1/pipelines/{pid}/triggers         — empty list
  13. GET    /v1/pipelines/{pid}/triggers         — pipeline not found → 404
  14. GET    /v1/pipelines/triggers/{tid}          — unauthenticated → 401
  15. GET    /v1/pipelines/triggers/{tid}          — happy path
  16. GET    /v1/pipelines/triggers/{tid}          — not found → 404
  17. GET    /v1/pipelines/triggers/{tid}          — wrong owner → 403
  18. PATCH  /v1/pipelines/triggers/{tid}          — unauthenticated → 401
  19. PATCH  /v1/pipelines/triggers/{tid}          — update name
  20. PATCH  /v1/pipelines/triggers/{tid}          — update is_active
  21. PATCH  /v1/pipelines/triggers/{tid}          — update default_input
  22. PATCH  /v1/pipelines/triggers/{tid}          — update cron_expression on schedule
  23. PATCH  /v1/pipelines/triggers/{tid}          — cron on webhook trigger → 422
  24. PATCH  /v1/pipelines/triggers/{tid}          — invalid cron → 422
  25. PATCH  /v1/pipelines/triggers/{tid}          — not found → 404
  26. PATCH  /v1/pipelines/triggers/{tid}          — wrong owner → 403
  27. DELETE /v1/pipelines/triggers/{tid}          — unauthenticated → 401
  28. DELETE /v1/pipelines/triggers/{tid}          — happy path → 204
  29. DELETE /v1/pipelines/triggers/{tid}          — not found → 404
  30. DELETE /v1/pipelines/triggers/{tid}          — wrong owner → 403
  31. POST   /v1/pipelines/webhooks/{token}        — happy path (fires trigger)
  32. POST   /v1/pipelines/webhooks/{token}        — not found → 404
  33. POST   /v1/pipelines/webhooks/{token}        — inactive trigger → 404
  34. POST   /v1/pipelines/webhooks/{token}        — with JSON body
  35. POST   /v1/pipelines/webhooks/{token}        — pipeline execution error → 500
  36. POST   /v1/pipelines/webhooks/{token}        — non-JSON body ignored
  37. POST   /v1/pipelines/triggers/{tid}/fire     — unauthenticated → 401
  38. POST   /v1/pipelines/triggers/{tid}/fire     — happy path
  39. POST   /v1/pipelines/triggers/{tid}/fire     — not found → 404
  40. POST   /v1/pipelines/triggers/{tid}/fire     — wrong owner → 403
  41. POST   /v1/pipelines/triggers/{tid}/fire     — pipeline execution error → 500
"""
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport

# ── Fixed IDs ────────────────────────────────────────────────────────────────

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PIPELINE_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TRIGGER_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
NOW = datetime.now(timezone.utc)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _make_mock_db():
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    db.execute = AsyncMock()
    db.scalar = AsyncMock(return_value=0)
    db.get = AsyncMock(return_value=None)

    async def _refresh(obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.UUID(TRIGGER_ID)
        if getattr(obj, "is_active", None) is None:
            obj.is_active = True
        if getattr(obj, "run_count", None) is None:
            obj.run_count = 0
        if getattr(obj, "last_fired_at", None) is None:
            obj.last_fired_at = None
        if getattr(obj, "created_at", None) is None:
            obj.created_at = NOW

    db.refresh = _refresh

    async def _delete(obj):
        pass
    db.delete = _delete
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _scalar(value):
    """Mock an execute() result where .scalar_one_or_none() returns value."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value)
    r.scalar = MagicMock(return_value=value if not isinstance(value, MagicMock) else 0)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all = MagicMock(return_value=[])
    return r


def _scalars_result(items):
    """Mock an execute() result where .scalars().all() returns items."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalar_one = MagicMock(return_value=len(items))
    r.scalar = MagicMock(return_value=len(items))
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    r.all = MagicMock(return_value=items)
    return r


def _make_pipeline(user_id=None):
    p = MagicMock()
    p.id = uuid.UUID(PIPELINE_ID)
    p.user_id = user_id or USER_ID
    p.name = "Test Pipeline"
    return p


def _make_trigger(trigger_type="schedule", user_id=None):
    t = MagicMock()
    t.id = uuid.UUID(TRIGGER_ID)
    t.pipeline_id = uuid.UUID(PIPELINE_ID)
    t.user_id = user_id or USER_ID
    t.trigger_type = trigger_type
    t.name = "Test Trigger"
    t.is_active = True
    t.cron_expression = "0 9 * * 1" if trigger_type == "schedule" else None
    t.webhook_token = "test-webhook-token" if trigger_type == "webhook" else None
    t.default_input = {"key": "value"}
    t.last_fired_at = None
    t.next_fire_at = None
    t.run_count = 0
    t.created_at = NOW
    return t


def _auth_header(user_id=None):
    return {"Authorization": f"Bearer {_token(user_id or USER_ID)}"}


# ── Tests: Create Trigger ────────────────────────────────────────────────────


class TestCreateTrigger:
    """POST /v1/pipelines/{pipeline_id}/triggers"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    json={"trigger_type": "schedule", "cron_expression": "0 9 * * 1"},
                )
            assert r.status_code in (401, 403)
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_create_schedule_trigger(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        pipeline = _make_pipeline()
        db.execute = AsyncMock(return_value=_scalar(pipeline))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    json={
                        "trigger_type": "schedule",
                        "name": "My Schedule",
                        "cron_expression": "0 9 * * 1",
                        "default_input": {"foo": "bar"},
                    },
                    headers=_auth_header(),
                )
            assert r.status_code == 201
            body = r.json()
            assert body["trigger_type"] == "schedule"
            assert body["pipeline_id"] == PIPELINE_ID
            assert body["is_active"] is True
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_create_webhook_trigger(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        pipeline = _make_pipeline()
        db.execute = AsyncMock(return_value=_scalar(pipeline))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    json={"trigger_type": "webhook", "name": "My Webhook"},
                    headers=_auth_header(),
                )
            assert r.status_code == 201
            body = r.json()
            assert body["trigger_type"] == "webhook"
            assert body["webhook_token"] is not None
            assert body["webhook_url"] is not None
            assert "/v1/pipelines/webhooks/" in body["webhook_url"]
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_pipeline_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    json={"trigger_type": "schedule", "cron_expression": "0 9 * * 1"},
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_schedule_without_cron(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        pipeline = _make_pipeline()
        db.execute = AsyncMock(return_value=_scalar(pipeline))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    json={"trigger_type": "schedule"},
                    headers=_auth_header(),
                )
            assert r.status_code == 422
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_cron_expression(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        pipeline = _make_pipeline()
        db.execute = AsyncMock(return_value=_scalar(pipeline))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    json={
                        "trigger_type": "schedule",
                        "cron_expression": "not a cron",
                    },
                    headers=_auth_header(),
                )
            assert r.status_code == 422
            assert "cron" in r.json()["detail"].lower()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_trigger_type(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    json={"trigger_type": "invalid_type"},
                    headers=_auth_header(),
                )
            assert r.status_code == 422
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_schedule_trigger_sets_next_fire_at(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        pipeline = _make_pipeline()
        db.execute = AsyncMock(return_value=_scalar(pipeline))

        # Track the object that gets added so we can inspect it
        added_objects = []
        original_add = db.add

        def tracking_add(obj):
            added_objects.append(obj)
            return original_add(obj)

        db.add = tracking_add

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    json={
                        "trigger_type": "schedule",
                        "cron_expression": "0 9 * * 1",
                    },
                    headers=_auth_header(),
                )
            assert r.status_code == 201
            # The added trigger object should have next_fire_at set
            assert len(added_objects) > 0
            trigger_obj = added_objects[0]
            assert trigger_obj.next_fire_at is not None
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_webhook_generates_webhook_url(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        pipeline = _make_pipeline()
        db.execute = AsyncMock(return_value=_scalar(pipeline))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    json={"trigger_type": "webhook"},
                    headers=_auth_header(),
                )
            assert r.status_code == 201
            body = r.json()
            assert body["webhook_url"].startswith("http://test/v1/pipelines/webhooks/")
            assert body["cron_expression"] is None
        finally:
            _app.dependency_overrides.pop(get_db, None)


# ── Tests: List Triggers ─────────────────────────────────────────────────────


class TestListTriggers:
    """GET /v1/pipelines/{pipeline_id}/triggers"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/pipelines/{PIPELINE_ID}/triggers")
            assert r.status_code in (401, 403)
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        pipeline = _make_pipeline()
        trigger = _make_trigger()

        # First execute: pipeline lookup, second execute: triggers query
        db.execute = AsyncMock(
            side_effect=[_scalar(pipeline), _scalars_result([trigger])]
        )

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert isinstance(body, list)
            assert len(body) == 1
            assert body[0]["trigger_type"] == "schedule"
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_empty_list(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        pipeline = _make_pipeline()
        db.execute = AsyncMock(
            side_effect=[_scalar(pipeline), _scalars_result([])]
        )

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            assert r.json() == []
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_pipeline_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/pipelines/{PIPELINE_ID}/triggers",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


# ── Tests: Get Trigger ───────────────────────────────────────────────────────


class TestGetTrigger:
    """GET /v1/pipelines/triggers/{trigger_id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/pipelines/triggers/{TRIGGER_ID}")
            assert r.status_code in (401, 403)
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger()
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["id"] == TRIGGER_ID
            assert body["trigger_type"] == "schedule"
            assert body["name"] == "Test Trigger"
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_wrong_owner(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger(user_id=OTHER_USER_ID)
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)


# ── Tests: Update Trigger ────────────────────────────────────────────────────


class TestUpdateTrigger:
    """PATCH /v1/pipelines/triggers/{trigger_id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    json={"name": "Updated"},
                )
            assert r.status_code in (401, 403)
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_update_name(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger()
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    json={"name": "Renamed Trigger"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            assert trigger.name == "Renamed Trigger"
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_update_is_active(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger()
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    json={"is_active": False},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            assert trigger.is_active is False
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_update_default_input(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger()
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    json={"default_input": {"new_key": "new_value"}},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            assert trigger.default_input == {"new_key": "new_value"}
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_update_cron_on_schedule(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger(trigger_type="schedule")
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    json={"cron_expression": "30 14 * * 5"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            assert trigger.cron_expression == "30 14 * * 5"
            # next_fire_at should be recomputed
            assert trigger.next_fire_at is not None
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_cron_on_webhook_trigger_rejected(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger(trigger_type="webhook")
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    json={"cron_expression": "0 9 * * 1"},
                    headers=_auth_header(),
                )
            assert r.status_code == 422
            assert "schedule" in r.json()["detail"].lower()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_cron_update(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger(trigger_type="schedule")
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    json={"cron_expression": "garbage cron"},
                    headers=_auth_header(),
                )
            assert r.status_code == 422
            assert "cron" in r.json()["detail"].lower()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    json={"name": "Updated"},
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_wrong_owner(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger(user_id=OTHER_USER_ID)
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    json={"name": "Updated"},
                    headers=_auth_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)


# ── Tests: Delete Trigger ────────────────────────────────────────────────────


class TestDeleteTrigger:
    """DELETE /v1/pipelines/triggers/{trigger_id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(f"/v1/pipelines/triggers/{TRIGGER_ID}")
            assert r.status_code in (401, 403)
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger()
        db.execute = AsyncMock(return_value=_scalar(trigger))

        deleted_objects = []

        async def _tracking_delete(obj):
            deleted_objects.append(obj)
        db.delete = _tracking_delete

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 204
            assert len(deleted_objects) == 1
            db.commit.assert_awaited_once()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_wrong_owner(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger(user_id=OTHER_USER_ID)
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)


# ── Tests: Fire Webhook Trigger ──────────────────────────────────────────────


class TestFireWebhookTrigger:
    """POST /v1/pipelines/webhooks/{token}"""

    @pytest.mark.asyncio
    @patch(
        "routers.triggers._fire_trigger",
        new_callable=AsyncMock,
        return_value={"run_id": "xxx", "status": "running"},
    )
    async def test_happy_path(self, mock_fire):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger(trigger_type="webhook")
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post("/v1/pipelines/webhooks/test-webhook-token")
            assert r.status_code == 200
            body = r.json()
            assert body["triggered"] is True
            assert body["run_id"] == "xxx"
            mock_fire.assert_awaited_once()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post("/v1/pipelines/webhooks/nonexistent-token")
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_inactive_trigger(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        # The query filters by is_active == True, so an inactive trigger
        # will not be found => returns None
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post("/v1/pipelines/webhooks/inactive-token")
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch(
        "routers.triggers._fire_trigger",
        new_callable=AsyncMock,
        return_value={"run_id": "yyy", "status": "running"},
    )
    async def test_with_json_body(self, mock_fire):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger(trigger_type="webhook")
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/pipelines/webhooks/test-webhook-token",
                    json={"extra_param": "extra_value"},
                )
            assert r.status_code == 200
            # Verify _fire_trigger was called with the override_input
            call_args = mock_fire.call_args
            assert call_args[1].get("override_input") == {"extra_param": "extra_value"} or \
                   (len(call_args[0]) >= 3 and call_args[0][2] == {"extra_param": "extra_value"})
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch(
        "routers.triggers._fire_trigger",
        new_callable=AsyncMock,
        side_effect=Exception("Pipeline execution boom"),
    )
    async def test_pipeline_execution_error(self, mock_fire):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger(trigger_type="webhook")
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post("/v1/pipelines/webhooks/test-webhook-token")
            assert r.status_code == 500
            assert "Pipeline execution failed" in r.json()["detail"]
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch(
        "routers.triggers._fire_trigger",
        new_callable=AsyncMock,
        return_value={"run_id": "zzz", "status": "running"},
    )
    async def test_non_json_body_ignored(self, mock_fire):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger(trigger_type="webhook")
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/pipelines/webhooks/test-webhook-token",
                    content=b"this is not json",
                    headers={"Content-Type": "text/plain"},
                )
            assert r.status_code == 200
            body = r.json()
            assert body["triggered"] is True
            # _fire_trigger should be called with empty dict as override
            call_args = mock_fire.call_args
            override = call_args[1].get("override_input") if "override_input" in (call_args[1] or {}) else (
                call_args[0][2] if len(call_args[0]) >= 3 else {}
            )
            assert override == {} or override is None
        finally:
            _app.dependency_overrides.pop(get_db, None)


# ── Tests: Manually Fire Trigger ─────────────────────────────────────────────


class TestManuallyFireTrigger:
    """POST /v1/pipelines/triggers/{trigger_id}/fire"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(f"/v1/pipelines/triggers/{TRIGGER_ID}/fire")
            assert r.status_code in (401, 403)
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch(
        "routers.triggers._fire_trigger",
        new_callable=AsyncMock,
        return_value={"run_id": "manual-run-id", "status": "running"},
    )
    async def test_happy_path(self, mock_fire):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger()
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}/fire",
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["triggered"] is True
            assert body["run_id"] == "manual-run-id"
            mock_fire.assert_awaited_once()
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}/fire",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_wrong_owner(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger(user_id=OTHER_USER_ID)
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}/fire",
                    headers=_auth_header(),
                )
            assert r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch(
        "routers.triggers._fire_trigger",
        new_callable=AsyncMock,
        side_effect=Exception("Pipeline kaboom"),
    )
    async def test_pipeline_execution_error(self, mock_fire):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        trigger = _make_trigger()
        db.execute = AsyncMock(return_value=_scalar(trigger))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/pipelines/triggers/{TRIGGER_ID}/fire",
                    headers=_auth_header(),
                )
            assert r.status_code == 500
            assert "Pipeline execution failed" in r.json()["detail"]
        finally:
            _app.dependency_overrides.pop(get_db, None)
