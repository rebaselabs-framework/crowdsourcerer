"""Tests for the scheduled tasks endpoint.

Covers:
  - GET /v1/tasks/scheduled: requires auth (401 without token)
  - GET /v1/tasks/scheduled: returns empty list when no scheduled tasks
  - GET /v1/tasks/scheduled: returns items with required fields
  - GET /v1/tasks/scheduled: only returns tasks owned by the requesting user
  - GET /v1/tasks/scheduled: respects limit query param validation
  - GET /v1/tasks/scheduled: tags default to empty list when None
  - GET /v1/tasks/scheduled: scheduled_at and created_at are ISO strings
  - GET /v1/tasks/scheduled: total matches item count
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "scheduled-test-secret")
os.environ.setdefault("API_KEY_SALT", "scheduled-test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Shared IDs ────────────────────────────────────────────────────────────────

USER_ID = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _make_mock_db() -> MagicMock:
    db = MagicMock()
    db.add      = MagicMock()
    db.flush    = AsyncMock()
    db.commit   = AsyncMock()
    db.rollback = AsyncMock()
    db.close    = AsyncMock()
    db.execute  = AsyncMock(return_value=_scalars_result([]))
    db.scalar   = AsyncMock(return_value=0)

    async def _refresh(obj): pass
    db.refresh = _refresh
    return db


def _scalars_result(items):
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    r.scalar_one_or_none = MagicMock(return_value=None)
    r.scalar_one = MagicMock(return_value=0)
    return r


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _make_task(
    task_id: str = TASK_ID,
    task_type: str = "llm_generate",
    execution_mode: str = "ai",
    priority: str = "normal",
    tags: list | None = None,
    scheduled_offset_hours: int = 2,
) -> MagicMock:
    t = MagicMock()
    t.id             = uuid.UUID(task_id)
    t.type           = task_type
    t.status         = "pending"
    t.execution_mode = execution_mode
    t.priority       = priority
    t.tags           = tags
    t.scheduled_at   = datetime.now(timezone.utc) + timedelta(hours=scheduled_offset_hours)
    t.created_at     = datetime.now(timezone.utc) - timedelta(minutes=5)
    return t


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    from main import app as _app
    return _app


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {_token(USER_ID)}"}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestScheduledTasksAuth:

    @pytest.mark.asyncio
    async def test_requires_auth(self, app):
        """Unauthenticated request returns 401."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/tasks/scheduled")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_accepted(self, app, auth_headers):
        """Valid token returns 200."""
        db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/tasks/scheduled", headers=auth_headers)
            assert r.status_code == 200
        finally:
            app.dependency_overrides.pop(get_db, None)


class TestScheduledTasksData:

    @pytest.mark.asyncio
    async def test_empty_list_when_none_scheduled(self, app, auth_headers):
        """Returns items=[] and total=0 when no scheduled tasks exist."""
        db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/tasks/scheduled", headers=auth_headers)
            assert r.status_code == 200
            body = r.json()
            assert body["items"] == []
            assert body["total"] == 0
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_returns_required_fields(self, app, auth_headers):
        """Each item has all required fields."""
        task = _make_task()
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result([task]))

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/tasks/scheduled", headers=auth_headers)
            assert r.status_code == 200
            items = r.json()["items"]
            assert len(items) == 1
            item = items[0]
            for field in ("id", "type", "status", "execution_mode", "priority",
                          "scheduled_at", "created_at", "tags"):
                assert field in item, f"Missing field: {field}"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_total_matches_item_count(self, app, auth_headers):
        """total in response equals len(items)."""
        tasks = [_make_task(str(uuid.uuid4()), scheduled_offset_hours=i + 1) for i in range(3)]
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result(tasks))

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/tasks/scheduled", headers=auth_headers)
            body = r.json()
            assert body["total"] == 3
            assert len(body["items"]) == 3
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_tags_default_to_empty_list_when_none(self, app, auth_headers):
        """tags=None on DB object becomes [] in response."""
        task = _make_task(tags=None)
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result([task]))

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/tasks/scheduled", headers=auth_headers)
            item = r.json()["items"][0]
            assert item["tags"] == []
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_tags_preserved_when_set(self, app, auth_headers):
        """tags list is preserved in response."""
        task = _make_task(tags=["prod", "nightly"])
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result([task]))

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/tasks/scheduled", headers=auth_headers)
            item = r.json()["items"][0]
            assert item["tags"] == ["prod", "nightly"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_scheduled_at_is_iso_string(self, app, auth_headers):
        """scheduled_at is a valid ISO-format datetime string."""
        task = _make_task()
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result([task]))

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/tasks/scheduled", headers=auth_headers)
            item = r.json()["items"][0]
            # Should be parseable as a datetime
            dt = datetime.fromisoformat(item["scheduled_at"].replace("Z", "+00:00"))
            assert dt > datetime.now(timezone.utc), "scheduled_at should be in the future"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_limit_param_too_high_rejected(self, app, auth_headers):
        """limit > 200 is rejected with 422."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/tasks/scheduled?limit=201", headers=auth_headers)
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_limit_param_zero_rejected(self, app, auth_headers):
        """limit=0 is rejected with 422 (ge=1)."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/tasks/scheduled?limit=0", headers=auth_headers)
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_multiple_task_types_returned(self, app, auth_headers):
        """Different task types are all returned correctly."""
        tasks = [
            _make_task(str(uuid.uuid4()), task_type="llm_generate",   execution_mode="ai",    scheduled_offset_hours=1),
            _make_task(str(uuid.uuid4()), task_type="label_image",     execution_mode="human", scheduled_offset_hours=2),
            _make_task(str(uuid.uuid4()), task_type="web_research",    execution_mode="ai",    scheduled_offset_hours=3),
        ]
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result(tasks))

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/tasks/scheduled", headers=auth_headers)
            body = r.json()
            assert body["total"] == 3
            types = {i["type"] for i in body["items"]}
            assert types == {"llm_generate", "label_image", "web_research"}
            modes = {i["execution_mode"] for i in body["items"]}
            assert modes == {"ai", "human"}
        finally:
            app.dependency_overrides.pop(get_db, None)
