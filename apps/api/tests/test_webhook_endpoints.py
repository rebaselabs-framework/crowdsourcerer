"""Comprehensive tests for the webhooks router (/v1/webhooks/*).

Covers all 15 endpoints:
  - GET  /v1/webhooks/events           — event type catalogue (no auth)
  - GET  /v1/webhooks/endpoints        — list user endpoints
  - POST /v1/webhooks/endpoints        — create endpoint
  - PATCH /v1/webhooks/endpoints/{id}  — update endpoint
  - DELETE /v1/webhooks/endpoints/{id} — delete endpoint
  - POST /v1/webhooks/endpoints/{id}/rotate-secret  — rotate signing secret
  - POST /v1/webhooks/endpoints/{id}/test            — test ping delivery
  - GET  /v1/webhooks/logs             — list delivery logs
  - POST /v1/webhooks/logs/{id}/retry  — retry failed delivery
  - POST /v1/webhooks/logs/{id}/replay — replay to all endpoints
  - GET  /v1/webhooks/stats            — delivery statistics
  - GET  /v1/webhooks/preferences      — notification preferences
  - PUT  /v1/webhooks/preferences      — update preferences
  - GET  /v1/webhooks/retry-queue      — retry queue status
  - GET  /v1/webhooks/retry-queue/items — retry queue items

Each class tests: 401 without token, happy path, 404 for not-found,
validation errors, and edge cases.
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

# ── Constants ────────────────────────────────────────────────────────────────

USER_ID = str(uuid.uuid4())
OTHER_USER_ID = str(uuid.uuid4())
ENDPOINT_ID = str(uuid.uuid4())
LOG_ID = str(uuid.uuid4())


# ── Helpers ──────────────────────────────────────────────────────────────────

def _token(user_id: str = USER_ID) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _auth(user_id: str = USER_ID) -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


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
        """Simulate DB refresh by populating server-side defaults."""
        from models.db import WebhookEndpointDB, NotificationPreferencesDB
        if isinstance(obj, WebhookEndpointDB):
            if obj.id is None:
                obj.id = uuid.uuid4()
            if obj.is_active is None:
                obj.is_active = True
            if obj.delivery_count is None:
                obj.delivery_count = 0
            if obj.failure_count is None:
                obj.failure_count = 0
            if obj.created_at is None:
                obj.created_at = datetime.now(timezone.utc)
            if obj.updated_at is None:
                obj.updated_at = datetime.now(timezone.utc)
    db.refresh = _refresh

    async def _delete(obj):
        pass
    db.delete = _delete
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _result_scalar(val):
    """Mock for (await db.execute(select(func.count())...)).scalar()"""
    r = MagicMock()
    r.scalar = MagicMock(return_value=val)
    r.scalar_one_or_none = MagicMock(return_value=val)
    r.scalar_one = MagicMock(return_value=val)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all = MagicMock(return_value=[])
    return r


def _scalars_result(items):
    """Mock for (await db.execute(select(Model)...)).scalars().all()"""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalar_one = MagicMock(return_value=items[0] if items else None)
    r.scalar = MagicMock(return_value=items[0] if items else None)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    r.all = MagicMock(return_value=items)
    return r


def _make_endpoint(endpoint_id=None, user_id=None):
    ep = MagicMock()
    ep.id = uuid.UUID(endpoint_id or ENDPOINT_ID)
    ep.user_id = user_id or USER_ID
    ep.url = "https://example.com/webhook"
    ep.description = "Test endpoint"
    ep.events = ["task.completed", "task.failed"]
    ep.is_active = True
    ep.delivery_count = 10
    ep.failure_count = 1
    ep.last_triggered_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ep.last_failure_at = None
    ep.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ep.updated_at = None
    ep.secret = "encrypted_secret"
    ep.previous_secret = None
    ep.previous_secret_expires_at = None
    return ep


def _make_log(log_id=None, user_id=None, success=True):
    log = MagicMock()
    log.id = uuid.UUID(log_id or LOG_ID)
    log.user_id = user_id or USER_ID
    log.task_id = uuid.uuid4()
    log.url = "https://example.com/webhook"
    log.event_type = "task.completed"
    log.attempt = 1
    log.status_code = 200 if success else 500
    log.success = success
    log.error = None if success else "HTTP 500"
    log.duration_ms = 42
    log.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return log


def _make_queue_item(status="pending"):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.endpoint_id = uuid.uuid4()
    item.task_id = uuid.uuid4()
    item.event_type = "task.completed"
    item.url = "https://example.com/webhook"
    item.attempt = 1
    item.max_attempts = 5
    item.status = status
    item.next_retry_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    item.last_error = None
    item.last_status_code = None
    item.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    item.updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return item


def _make_prefs_row(prefs_dict=None):
    row = MagicMock()
    row.webhook_event_prefs = prefs_dict or {}
    row.user_id = USER_ID
    return row


# ── App + DB wiring ──────────────────────────────────────────────────────────

def _get_app_and_override(mock_db):
    from main import app
    from core.database import get_db
    app.dependency_overrides[get_db] = _db_override(mock_db)
    return app, get_db


def _cleanup(app, get_db_func):
    app.dependency_overrides.pop(get_db_func, None)


# ═════════════════════════════════════════════════════════════════════════════
# 1. GET /v1/webhooks/events — event catalogue (no auth)
# ═════════════════════════════════════════════════════════════════════════════

class TestListEventTypes:

    @pytest.mark.asyncio
    async def test_returns_events_without_auth(self):
        """No auth required — returns full event catalogue."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/webhooks/events")
        assert r.status_code == 200
        data = r.json()
        assert "events" in data
        assert "default_events" in data
        assert isinstance(data["events"], list)
        assert len(data["events"]) > 0

    @pytest.mark.asyncio
    async def test_event_structure(self):
        """Each event has type, description, and is_default."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/webhooks/events")
        events = r.json()["events"]
        for ev in events:
            assert "type" in ev
            assert "description" in ev
            assert "is_default" in ev
            assert isinstance(ev["is_default"], bool)

    @pytest.mark.asyncio
    async def test_default_events_subset(self):
        """default_events should be a subset of all event types."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/webhooks/events")
        data = r.json()
        all_types = {ev["type"] for ev in data["events"]}
        for de in data["default_events"]:
            assert de in all_types


# ═════════════════════════════════════════════════════════════════════════════
# 2. GET /v1/webhooks/endpoints — list user endpoints
# ═════════════════════════════════════════════════════════════════════════════

class TestListEndpoints:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/webhooks/endpoints")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_happy_path_empty(self):
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/endpoints", headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert data["items"] == []
            assert data["total"] == 0
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_happy_path_with_endpoints(self):
        ep = _make_endpoint()
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([ep])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/endpoints", headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert data["total"] == 1
            item = data["items"][0]
            assert item["id"] == str(ep.id)
            assert item["url"] == ep.url
            assert item["events"] == ep.events
            assert item["is_active"] is True
            # Secret should NOT be in the list response
            assert "secret" not in item
        finally:
            _cleanup(app, get_db)


# ═════════════════════════════════════════════════════════════════════════════
# 3. POST /v1/webhooks/endpoints — create endpoint
# ═════════════════════════════════════════════════════════════════════════════

class TestCreateEndpoint:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/webhooks/endpoints", json={
                "url": "https://example.com/webhook"
            })
        assert r.status_code == 401

    @pytest.mark.asyncio
    @patch("routers.webhooks.encrypt_secret", return_value="encrypted_secret")
    @patch("routers.webhooks.validate_webhook_url")
    @patch("routers.webhooks.safe_create_task")
    async def test_happy_path(self, mock_task, mock_validate, mock_encrypt):
        db = _make_mock_db()
        ep = _make_endpoint()
        # First execute: count check → scalar() returns 0
        # The endpoint also calls db.commit and db.refresh
        db.execute.return_value = _result_scalar(0)
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/webhooks/endpoints", json={
                    "url": "https://example.com/webhook",
                    "description": "My webhook",
                    "events": ["task.completed"],
                }, headers=_auth())
            assert r.status_code == 201
            data = r.json()
            assert "id" in data
            assert "secret" in data  # Secret returned only on creation
            assert data["url"] == "https://example.com/webhook"
            assert data["events"] == ["task.completed"]
            mock_validate.assert_called_once()
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    @patch("routers.webhooks.encrypt_secret", return_value="encrypted_secret")
    @patch("routers.webhooks.validate_webhook_url")
    @patch("routers.webhooks.safe_create_task")
    async def test_create_with_no_events(self, mock_task, mock_validate, mock_encrypt):
        """When events is None, endpoint subscribes to all events (default)."""
        db = _make_mock_db()
        db.execute.return_value = _result_scalar(0)
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/webhooks/endpoints", json={
                    "url": "https://example.com/webhook",
                }, headers=_auth())
            assert r.status_code == 201
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    @patch("routers.webhooks.validate_webhook_url")
    async def test_invalid_event_types(self, mock_validate):
        db = _make_mock_db()
        db.execute.return_value = _result_scalar(0)
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/webhooks/endpoints", json={
                    "url": "https://example.com/webhook",
                    "events": ["task.completed", "bogus.event"],
                }, headers=_auth())
            assert r.status_code == 400
            assert "Unknown event types" in r.json()["detail"]
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    @patch("routers.webhooks.validate_webhook_url", side_effect=__import__("core.url_validation", fromlist=["UnsafeURLError"]).UnsafeURLError("blocked"))
    async def test_unsafe_url_rejected(self, mock_validate):
        db = _make_mock_db()
        db.execute.return_value = _result_scalar(0)
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/webhooks/endpoints", json={
                    "url": "http://169.254.169.254/latest/meta-data",
                }, headers=_auth())
            assert r.status_code == 400
            assert "Invalid webhook URL" in r.json()["detail"]
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    @patch("routers.webhooks.validate_webhook_url")
    async def test_max_endpoints_cap(self, mock_validate):
        """User already has 20 endpoints — creation rejected."""
        db = _make_mock_db()
        db.execute.return_value = _result_scalar(20)
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/webhooks/endpoints", json={
                    "url": "https://example.com/webhook",
                }, headers=_auth())
            assert r.status_code == 400
            assert "Maximum of 20" in r.json()["detail"]
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_missing_url_validation_error(self):
        """URL is required in the body."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/webhooks/endpoints", json={}, headers=_auth())
        assert r.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
# 4. PATCH /v1/webhooks/endpoints/{id} — update endpoint
# ═════════════════════════════════════════════════════════════════════════════

class TestUpdateEndpoint:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(f"/v1/webhooks/endpoints/{ENDPOINT_ID}", json={})
        assert r.status_code == 401

    @pytest.mark.asyncio
    @patch("routers.webhooks.validate_webhook_url")
    async def test_happy_path(self, mock_validate):
        ep = _make_endpoint()
        db = _make_mock_db()
        # _get_owned_endpoint: first execute returns the endpoint
        db.execute.return_value = _scalars_result([ep])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(f"/v1/webhooks/endpoints/{ENDPOINT_ID}", json={
                    "url": "https://new.example.com/hook",
                    "description": "Updated",
                    "is_active": False,
                }, headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert data["id"] == str(ep.id)
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_not_found(self):
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(f"/v1/webhooks/endpoints/{uuid.uuid4()}", json={
                    "description": "Does not exist",
                }, headers=_auth())
            assert r.status_code == 404
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_invalid_events_on_update(self):
        ep = _make_endpoint()
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([ep])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(f"/v1/webhooks/endpoints/{ENDPOINT_ID}", json={
                    "events": ["not.a.real.event"],
                }, headers=_auth())
            assert r.status_code == 400
            assert "Unknown event types" in r.json()["detail"]
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    @patch("routers.webhooks.validate_webhook_url", side_effect=__import__("core.url_validation", fromlist=["UnsafeURLError"]).UnsafeURLError("blocked"))
    async def test_unsafe_url_on_update(self, mock_validate):
        ep = _make_endpoint()
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([ep])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(f"/v1/webhooks/endpoints/{ENDPOINT_ID}", json={
                    "url": "http://127.0.0.1/evil",
                }, headers=_auth())
            assert r.status_code == 400
            assert "Invalid webhook URL" in r.json()["detail"]
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_invalid_uuid_returns_422(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/webhooks/endpoints/not-a-uuid", json={}, headers=_auth())
        assert r.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
# 5. DELETE /v1/webhooks/endpoints/{id} — delete endpoint
# ═════════════════════════════════════════════════════════════════════════════

class TestDeleteEndpoint:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(f"/v1/webhooks/endpoints/{ENDPOINT_ID}")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_happy_path(self):
        ep = _make_endpoint()
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([ep])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(f"/v1/webhooks/endpoints/{ENDPOINT_ID}", headers=_auth())
            assert r.status_code == 204
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_not_found(self):
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(f"/v1/webhooks/endpoints/{uuid.uuid4()}", headers=_auth())
            assert r.status_code == 404
        finally:
            _cleanup(app, get_db)


# ═════════════════════════════════════════════════════════════════════════════
# 6. POST /v1/webhooks/endpoints/{id}/rotate-secret — rotate signing secret
# ═════════════════════════════════════════════════════════════════════════════

class TestRotateSecret:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(f"/v1/webhooks/endpoints/{ENDPOINT_ID}/rotate-secret")
        assert r.status_code == 401

    @pytest.mark.asyncio
    @patch("routers.webhooks.encrypt_secret", return_value="new_encrypted_secret")
    async def test_happy_path(self, mock_encrypt):
        ep = _make_endpoint()
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([ep])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/webhooks/endpoints/{ENDPOINT_ID}/rotate-secret",
                    headers=_auth(),
                )
            assert r.status_code == 200
            data = r.json()
            assert "secret" in data
            assert "previous_secret_expires_at" in data
            # The old secret should be stashed as previous_secret
            assert ep.previous_secret == "encrypted_secret"
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_not_found(self):
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/webhooks/endpoints/{uuid.uuid4()}/rotate-secret",
                    headers=_auth(),
                )
            assert r.status_code == 404
        finally:
            _cleanup(app, get_db)


# ═════════════════════════════════════════════════════════════════════════════
# 7. POST /v1/webhooks/endpoints/{id}/test — test ping
# ═════════════════════════════════════════════════════════════════════════════

class TestTestEndpoint:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(f"/v1/webhooks/endpoints/{ENDPOINT_ID}/test")
        assert r.status_code == 401

    @pytest.mark.asyncio
    @patch("routers.webhooks.decrypt_secret", return_value="raw_secret")
    @patch("routers.webhooks._get_webhook_client")
    async def test_happy_path_success(self, mock_client_fn, mock_decrypt):
        ep = _make_endpoint()
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([ep])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_fn.return_value = mock_client

        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/webhooks/endpoints/{ENDPOINT_ID}/test",
                    headers=_auth(),
                )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert data["status_code"] == 200
            assert data["error"] is None
            assert "duration_ms" in data
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    @patch("routers.webhooks.decrypt_secret", return_value="raw_secret")
    @patch("routers.webhooks._get_webhook_client")
    async def test_test_endpoint_returns_failure_on_5xx(self, mock_client_fn, mock_decrypt):
        ep = _make_endpoint()
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([ep])

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_fn.return_value = mock_client

        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/webhooks/endpoints/{ENDPOINT_ID}/test",
                    headers=_auth(),
                )
            assert r.status_code == 200  # The endpoint itself succeeds
            data = r.json()
            assert data["success"] is False
            assert data["status_code"] == 500
            assert data["error"] == "HTTP 500"
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    @patch("routers.webhooks.decrypt_secret", return_value="raw_secret")
    @patch("routers.webhooks._get_webhook_client")
    async def test_test_endpoint_network_error(self, mock_client_fn, mock_decrypt):
        ep = _make_endpoint()
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([ep])

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=ConnectionError("refused"))
        mock_client_fn.return_value = mock_client

        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/webhooks/endpoints/{ENDPOINT_ID}/test",
                    headers=_auth(),
                )
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is False
            assert data["status_code"] is None
            assert "refused" in data["error"]
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_not_found(self):
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/webhooks/endpoints/{uuid.uuid4()}/test",
                    headers=_auth(),
                )
            assert r.status_code == 404
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    @patch("routers.webhooks.decrypt_secret", return_value="raw_secret")
    @patch("routers.webhooks._get_webhook_client")
    async def test_test_with_previous_secret_grace_period(self, mock_client_fn, mock_decrypt):
        """When previous_secret exists and is within grace period, both v1 and v0 sigs sent."""
        ep = _make_endpoint()
        ep.previous_secret = "old_encrypted_secret"
        ep.previous_secret_expires_at = datetime(2099, 1, 1, tzinfo=timezone.utc)  # far future
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([ep])

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_fn.return_value = mock_client

        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/webhooks/endpoints/{ENDPOINT_ID}/test",
                    headers=_auth(),
                )
            assert r.status_code == 200
            assert r.json()["success"] is True
            # Verify the client was called with a signature header containing v0
            call_kwargs = mock_client.post.call_args
            sig_header = call_kwargs.kwargs["headers"]["X-Crowdsourcerer-Signature"]
            assert "v1=" in sig_header
            assert "v0=" in sig_header
        finally:
            _cleanup(app, get_db)


# ═════════════════════════════════════════════════════════════════════════════
# 8. GET /v1/webhooks/logs — list delivery logs
# ═════════════════════════════════════════════════════════════════════════════

class TestListWebhookLogs:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/webhooks/logs")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_happy_path_empty(self):
        db = _make_mock_db()
        # First execute: count query → scalar() returns 0
        # Second execute: the actual log query → scalars().all() returns []
        db.execute.side_effect = [
            _result_scalar(0),
            _scalars_result([]),
        ]
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/logs", headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert data["items"] == []
            assert data["total"] == 0
            assert data["page"] == 1
            assert data["has_next"] is False
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_happy_path_with_logs(self):
        log = _make_log()
        db = _make_mock_db()
        db.execute.side_effect = [
            _result_scalar(1),
            _scalars_result([log]),
        ]
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/logs", headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert data["total"] == 1
            assert len(data["items"]) == 1
            item = data["items"][0]
            assert item["id"] == str(log.id)
            assert item["event_type"] == "task.completed"
            assert item["success"] is True
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_pagination_has_next(self):
        """When total > page * page_size, has_next should be True."""
        log = _make_log()
        db = _make_mock_db()
        db.execute.side_effect = [
            _result_scalar(50),   # total = 50
            _scalars_result([log]),
        ]
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/logs?page=1&page_size=25", headers=_auth())
            assert r.status_code == 200
            assert r.json()["has_next"] is True
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_filter_params_accepted(self):
        """Filters (task_id, event_type, success) should not cause errors."""
        db = _make_mock_db()
        db.execute.side_effect = [
            _result_scalar(0),
            _scalars_result([]),
        ]
        app, get_db = _get_app_and_override(db)
        try:
            tid = str(uuid.uuid4())
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/webhooks/logs?task_id={tid}&event_type=task.completed&success=true",
                    headers=_auth(),
                )
            assert r.status_code == 200
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_page_size_validation(self):
        """page_size > 100 should be rejected."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/webhooks/logs?page_size=200", headers=_auth())
        assert r.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
# 9. POST /v1/webhooks/logs/{id}/retry — retry delivery
# ═════════════════════════════════════════════════════════════════════════════

class TestRetryWebhook:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(f"/v1/webhooks/logs/{LOG_ID}/retry")
        assert r.status_code == 401

    @pytest.mark.asyncio
    @patch("routers.webhooks.retry_webhook_log", new_callable=AsyncMock)
    async def test_happy_path(self, mock_retry):
        mock_retry.return_value = {"success": True, "new_log_id": str(uuid.uuid4())}
        db = _make_mock_db()
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/v1/webhooks/logs/{LOG_ID}/retry", headers=_auth())
            assert r.status_code == 200
            assert r.json()["success"] is True
            mock_retry.assert_awaited_once_with(log_id=LOG_ID, user_id=USER_ID)
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    @patch("routers.webhooks.retry_webhook_log", new_callable=AsyncMock, side_effect=ValueError("Log not found"))
    async def test_not_found(self, mock_retry):
        db = _make_mock_db()
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/v1/webhooks/logs/{LOG_ID}/retry", headers=_auth())
            assert r.status_code == 404
            assert "Log not found" in r.json()["detail"]
        finally:
            _cleanup(app, get_db)


# ═════════════════════════════════════════════════════════════════════════════
# 10. POST /v1/webhooks/logs/{id}/replay — replay to all endpoints
# ═════════════════════════════════════════════════════════════════════════════

class TestReplayWebhook:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(f"/v1/webhooks/logs/{LOG_ID}/replay")
        assert r.status_code == 401

    @pytest.mark.asyncio
    @patch("routers.webhooks.replay_webhook_log", new_callable=AsyncMock)
    async def test_happy_path(self, mock_replay):
        mock_replay.return_value = {"replayed_to": 3, "results": []}
        db = _make_mock_db()
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/v1/webhooks/logs/{LOG_ID}/replay", headers=_auth())
            assert r.status_code == 200
            assert r.json()["replayed_to"] == 3
            mock_replay.assert_awaited_once_with(log_id=LOG_ID, user_id=USER_ID)
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    @patch("routers.webhooks.replay_webhook_log", new_callable=AsyncMock, side_effect=ValueError("Log not found or not owned"))
    async def test_not_found(self, mock_replay):
        db = _make_mock_db()
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/v1/webhooks/logs/{LOG_ID}/replay", headers=_auth())
            assert r.status_code == 404
        finally:
            _cleanup(app, get_db)


# ═════════════════════════════════════════════════════════════════════════════
# 11. GET /v1/webhooks/stats — webhook delivery statistics
# ═════════════════════════════════════════════════════════════════════════════

class TestWebhookStats:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/webhooks/stats")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_happy_path_no_deliveries(self):
        db = _make_mock_db()
        # 4 db.execute calls: total, succeeded, avg_duration, event_rows
        db.execute.side_effect = [
            _result_scalar(0),   # total
            _result_scalar(0),   # succeeded
            _result_scalar(None),  # avg_duration
            _scalars_result([]),   # by_event (returns .all() rows)
        ]
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/stats", headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert data["total_deliveries"] == 0
            assert data["succeeded"] == 0
            assert data["failed"] == 0
            assert data["success_rate"] == 100.0
            assert data["avg_duration_ms"] is None
            assert data["by_event_type"] == {}
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_happy_path_with_deliveries(self):
        db = _make_mock_db()
        # Mock event_rows result — .all() returns list of Row-like tuples
        event_row = MagicMock()
        event_row.event_type = "task.completed"
        event_row.cnt = 7
        event_result = MagicMock()
        event_result.all = MagicMock(return_value=[event_row])

        db.execute.side_effect = [
            _result_scalar(10),    # total
            _result_scalar(8),     # succeeded
            _result_scalar(45.2),  # avg_duration
            event_result,          # by_event
        ]
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/stats", headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert data["total_deliveries"] == 10
            assert data["succeeded"] == 8
            assert data["failed"] == 2
            assert data["success_rate"] == 80.0
            assert data["avg_duration_ms"] == 45
            assert data["by_event_type"]["task.completed"] == 7
        finally:
            _cleanup(app, get_db)


# ═════════════════════════════════════════════════════════════════════════════
# 12. GET /v1/webhooks/preferences — get notification prefs
# ═════════════════════════════════════════════════════════════════════════════

class TestGetPreferences:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/webhooks/preferences")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_happy_path_no_prefs_saved(self):
        """When no prefs row exists, all events default to True."""
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/preferences", headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert "prefs" in data
            assert "events" in data
            # All events should default to True
            for val in data["prefs"].values():
                assert val is True
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_happy_path_with_saved_prefs(self):
        """Saved prefs override defaults."""
        prefs_row = _make_prefs_row({"task.completed": False, "task.failed": True})
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([prefs_row])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/preferences", headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert data["prefs"]["task.completed"] is False
            assert data["prefs"]["task.failed"] is True
            # Events not in saved prefs default to True
            assert data["prefs"]["task.created"] is True
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_events_list_structure(self):
        """The events list should include type, description, enabled, is_default."""
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/preferences", headers=_auth())
            events = r.json()["events"]
            for ev in events:
                assert "type" in ev
                assert "description" in ev
                assert "enabled" in ev
                assert "is_default" in ev
        finally:
            _cleanup(app, get_db)


# ═════════════════════════════════════════════════════════════════════════════
# 13. PUT /v1/webhooks/preferences — update notification prefs
# ═════════════════════════════════════════════════════════════════════════════

class TestUpdatePreferences:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.put("/v1/webhooks/preferences", json={"prefs": {}})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_happy_path_create_new(self):
        """When no prefs row exists, one is created."""
        db = _make_mock_db()
        # First execute: lookup existing prefs → None
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.put("/v1/webhooks/preferences", json={
                    "prefs": {"task.completed": False},
                }, headers=_auth())
            assert r.status_code == 200
            assert r.json()["ok"] is True
            # db.add should have been called for the new row
            db.add.assert_called_once()
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_happy_path_update_existing(self):
        """When prefs row exists, it is updated (merged)."""
        prefs_row = _make_prefs_row({"task.completed": True})
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([prefs_row])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.put("/v1/webhooks/preferences", json={
                    "prefs": {"task.completed": False, "task.failed": True},
                }, headers=_auth())
            assert r.status_code == 200
            assert r.json()["ok"] is True
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_unknown_events_silently_ignored(self):
        """Unrecognised event types in body.prefs are silently dropped."""
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.put("/v1/webhooks/preferences", json={
                    "prefs": {"totally.fake.event": False, "task.completed": True},
                }, headers=_auth())
            assert r.status_code == 200
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_missing_prefs_field(self):
        """Body without 'prefs' key should fail validation."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.put("/v1/webhooks/preferences", json={}, headers=_auth())
        assert r.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
# 14. GET /v1/webhooks/retry-queue — retry queue status
# ═════════════════════════════════════════════════════════════════════════════

class TestRetryQueueStatus:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/webhooks/retry-queue")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_happy_path_empty_queue(self):
        db = _make_mock_db()
        empty_result = MagicMock()
        empty_result.all = MagicMock(return_value=[])
        db.execute.return_value = empty_result
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/retry-queue", headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert data["total"] == 0
            assert data["pending"] == 0
            assert data["processing"] == 0
            assert data["completed"] == 0
            assert data["dead_letter"] == 0
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_happy_path_with_items(self):
        db = _make_mock_db()
        # result.all() returns list of (status, count) tuples
        row_pending = MagicMock()
        row_pending.__iter__ = MagicMock(return_value=iter(("pending", 5)))
        row_dead = MagicMock()
        row_dead.__iter__ = MagicMock(return_value=iter(("dead_letter", 2)))
        result = MagicMock()
        result.all = MagicMock(return_value=[("pending", 5), ("dead_letter", 2)])
        db.execute.return_value = result
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/retry-queue", headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert data["total"] == 7
            assert data["pending"] == 5
            assert data["dead_letter"] == 2
            assert data["processing"] == 0
            assert data["completed"] == 0
        finally:
            _cleanup(app, get_db)


# ═════════════════════════════════════════════════════════════════════════════
# 15. GET /v1/webhooks/retry-queue/items — retry queue items
# ═════════════════════════════════════════════════════════════════════════════

class TestRetryQueueItems:

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/webhooks/retry-queue/items")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_happy_path_empty(self):
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/retry-queue/items", headers=_auth())
            assert r.status_code == 200
            assert r.json() == []
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_happy_path_with_items(self):
        item = _make_queue_item()
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([item])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/webhooks/retry-queue/items", headers=_auth())
            assert r.status_code == 200
            data = r.json()
            assert len(data) == 1
            assert data[0]["id"] == str(item.id)
            assert data[0]["status"] == "pending"
            assert data[0]["event_type"] == "task.completed"
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_filter_by_status(self):
        """Status query param should be accepted without error."""
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/webhooks/retry-queue/items?status=dead_letter",
                    headers=_auth(),
                )
            assert r.status_code == 200
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_pagination_params(self):
        """limit and offset params should work."""
        db = _make_mock_db()
        db.execute.return_value = _scalars_result([])
        app, get_db = _get_app_and_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/webhooks/retry-queue/items?limit=5&offset=10",
                    headers=_auth(),
                )
            assert r.status_code == 200
        finally:
            _cleanup(app, get_db)

    @pytest.mark.asyncio
    async def test_limit_validation(self):
        """limit > 100 should be rejected."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/webhooks/retry-queue/items?limit=200",
                headers=_auth(),
            )
        assert r.status_code == 422
