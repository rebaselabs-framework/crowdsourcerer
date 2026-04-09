"""Tests for the requester templates router — personal CRUD + marketplace.

Covers:
  Personal templates (prefix /v1/task-templates):
    1.  GET  /v1/task-templates              — 401 without token
    2.  POST /v1/task-templates              — 401 without token
    3.  GET  /v1/task-templates/{id}         — 401 without token
    4.  PATCH /v1/task-templates/{id}        — 401 without token
    5.  DELETE /v1/task-templates/{id}       — 401 without token
    6.  POST /v1/task-templates/{id}/use     — 401 without token
    7.  POST /v1/task-templates/{id}/publish — 401 without token
    8.  POST /v1/task-templates              — create happy path (201)
    9.  GET  /v1/task-templates              — list returns templates + total
   10.  GET  /v1/task-templates/{id}         — get owned template (200)
   11.  GET  /v1/task-templates/{id}         — other user's template → 404 (IDOR)
   12.  PATCH /v1/task-templates/{id}        — partial update happy path
   13.  DELETE /v1/task-templates/{id}       — delete happy path (204)
   14.  DELETE /v1/task-templates/{id}       — delete other user's → 404
   15.  POST /v1/task-templates/{id}/use     — increment use_count
   16.  POST /v1/task-templates              — max 50 templates → 400
   17.  POST /v1/task-templates              — missing name → 422
   18.  POST /v1/task-templates/{id}/publish — publish happy path
   19.  DELETE /v1/task-templates/{id}/publish — unpublish (204)

  Marketplace (prefix /v1/template-marketplace):
   20.  GET  /v1/template-marketplace        — 401 without token
   21.  GET  /v1/template-marketplace        — browse returns results
   22.  GET  /v1/template-marketplace/{id}   — get public template
   23.  GET  /v1/template-marketplace/{id}   — non-public template → 404
   24.  POST /v1/template-marketplace/{id}/import — 401 without token
   25.  POST /v1/template-marketplace/{id}/import — happy path (201)
   26.  POST /v1/template-marketplace/{id}/import — not found → 404
   27.  POST /v1/template-marketplace/{id}/import — own template → 400
   28.  POST /v1/template-marketplace/{id}/import — max limit → 400
   29.  PATCH /v1/task-templates/{id}        — update other user's → 404
   30.  POST /v1/task-templates/{id}/use     — other user's template → 404
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Stable IDs ──────────────────────────────────────────────────────────────

USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())
TPL_ID = str(uuid.uuid4())


# ── Helpers ─────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _real_token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _make_template(
    template_id: str | None = None,
    user_id: str = USER_A,
    name: str = "My Template",
    description: str | None = "A test template",
    task_type: str = "web_research",
    task_input: dict | None = None,
    task_config: dict | None = None,
    icon: str | None = None,
    use_count: int = 0,
    import_count: int = 0,
    is_public: bool = False,
    marketplace_title: str | None = None,
    marketplace_description: str | None = None,
    marketplace_tags: list | None = None,
    published_at: datetime | None = None,
) -> MagicMock:
    tpl = MagicMock()
    tpl.id = uuid.UUID(template_id or str(uuid.uuid4()))
    tpl.user_id = uuid.UUID(user_id)
    tpl.name = name
    tpl.description = description
    tpl.task_type = task_type
    tpl.task_input = task_input or {"url": "https://example.com"}
    tpl.task_config = task_config or {"priority": "medium"}
    tpl.icon = icon
    tpl.use_count = use_count
    tpl.import_count = import_count
    tpl.is_public = is_public
    tpl.marketplace_title = marketplace_title
    tpl.marketplace_description = marketplace_description
    tpl.marketplace_tags = marketplace_tags or []
    tpl.published_at = published_at
    tpl.created_at = _now()
    tpl.updated_at = _now()
    return tpl


def _make_user(user_id: str = USER_A, name: str = "Alice") -> MagicMock:
    u = MagicMock()
    u.id = uuid.UUID(user_id) if isinstance(user_id, str) else user_id
    u.name = name
    u.reputation_score = 4.5
    return u


def _make_db() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.execute = AsyncMock()
    db.scalar = AsyncMock(return_value=0)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.delete = AsyncMock()
    db.close = AsyncMock()

    async def _refresh(obj):
        # Simulate DB defaults for freshly-created ORM objects that the
        # router returns through a Pydantic response model.
        now = _now()
        if getattr(obj, "use_count", None) is None:
            obj.use_count = 0
        if getattr(obj, "import_count", None) is None:
            obj.import_count = 0
        if getattr(obj, "marketplace_tags", None) is None:
            obj.marketplace_tags = []
        if getattr(obj, "created_at", None) is None:
            obj.created_at = now
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = now
        if getattr(obj, "is_public", None) is None:
            obj.is_public = False
    db.refresh = _refresh
    return db


def _scalar_result(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalars = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=[value] if value is not None else []))
    )
    return r


def _scalars_result(items: list) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalars = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=items))
    )
    return r


def _db_override(mock_db):
    async def _override():
        yield mock_db
    return _override


# ── Valid request payloads ──────────────────────────────────────────────────

CREATE_PAYLOAD = {
    "name": "Research Template",
    "description": "Quick web research",
    "task_type": "web_research",
    "task_input": {"url": "https://example.com"},
    "task_config": {"priority": "medium"},
    "icon": "🔍",
}

UPDATE_PAYLOAD = {
    "name": "Updated Name",
    "description": "Updated description",
}


# =============================================================================
# Auth Guards — all personal endpoints require a token
# =============================================================================

class TestAuthGuards:
    """Every personal and marketplace endpoint must reject unauthenticated requests."""

    @pytest.mark.asyncio
    async def test_list_templates_requires_auth(self):
        """GET /v1/task-templates — 401 without token."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/task-templates")
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_create_template_requires_auth(self):
        """POST /v1/task-templates — 401 without token."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/task-templates", json=CREATE_PAYLOAD)
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_get_template_requires_auth(self):
        """GET /v1/task-templates/{id} — 401 without token."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/task-templates/{uuid.uuid4()}")
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_update_template_requires_auth(self):
        """PATCH /v1/task-templates/{id} — 401 without token."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(f"/v1/task-templates/{uuid.uuid4()}", json=UPDATE_PAYLOAD)
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_delete_template_requires_auth(self):
        """DELETE /v1/task-templates/{id} — 401 without token."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(f"/v1/task-templates/{uuid.uuid4()}")
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_use_template_requires_auth(self):
        """POST /v1/task-templates/{id}/use — 401 without token."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(f"/v1/task-templates/{uuid.uuid4()}/use")
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_publish_template_requires_auth(self):
        """POST /v1/task-templates/{id}/publish — 401 without token."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(f"/v1/task-templates/{uuid.uuid4()}/publish", json={})
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_browse_marketplace_requires_auth(self):
        """GET /v1/template-marketplace — 401 without token."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/template-marketplace")
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_import_marketplace_requires_auth(self):
        """POST /v1/template-marketplace/{id}/import — 401 without token."""
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(f"/v1/template-marketplace/{uuid.uuid4()}/import")
        assert r.status_code in (401, 403)


# =============================================================================
# Personal Templates — CRUD
# =============================================================================

class TestCreateTemplate:
    """POST /v1/task-templates — create a personal template."""

    @pytest.mark.asyncio
    async def test_create_happy_path(self):
        """Create template returns 201 and calls db.add + db.commit."""
        from main import app
        from core.database import get_db

        db = _make_db()
        db.scalar.return_value = 0  # user has 0 templates (under limit)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/task-templates",
                    json=CREATE_PAYLOAD,
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 201
            db.add.assert_called_once()
            db.commit.assert_awaited_once()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_create_max_limit_reached(self):
        """Create fails with 400 when user already has 50 templates."""
        from main import app
        from core.database import get_db

        db = _make_db()
        db.scalar.return_value = 50  # at the limit
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/task-templates",
                    json=CREATE_PAYLOAD,
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 400
            assert "50" in r.json()["detail"]
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_create_missing_name_422(self):
        """Create with missing name returns 422 validation error."""
        from main import app
        from core.database import get_db

        db = _make_db()
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            payload = {
                "task_type": "web_research",
                "task_input": {},
                "task_config": {},
            }
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/task-templates",
                    json=payload,
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 422
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_create_empty_name_422(self):
        """Create with empty name string returns 422 (min_length=1)."""
        from main import app
        from core.database import get_db

        db = _make_db()
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            payload = {**CREATE_PAYLOAD, "name": ""}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/task-templates",
                    json=payload,
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 422
        finally:
            app.dependency_overrides.clear()


class TestListTemplates:
    """GET /v1/task-templates — list user's personal templates."""

    @pytest.mark.asyncio
    async def test_list_returns_templates_and_total(self):
        """List returns templates array and total count."""
        from main import app
        from core.database import get_db

        tpl = _make_template(user_id=USER_A)
        db = _make_db()
        db.scalar.return_value = 1  # total count
        db.execute.return_value = _scalars_result([tpl])
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/task-templates",
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 200
            body = r.json()
            assert "templates" in body
            assert "total" in body
            assert body["total"] == 1
            assert len(body["templates"]) == 1
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_list_empty(self):
        """List with no templates returns empty array and total=0."""
        from main import app
        from core.database import get_db

        db = _make_db()
        db.scalar.return_value = 0
        db.execute.return_value = _scalars_result([])
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/task-templates",
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 200
            body = r.json()
            assert body["templates"] == []
            assert body["total"] == 0
        finally:
            app.dependency_overrides.clear()


class TestGetTemplate:
    """GET /v1/task-templates/{id} — get a single owned template."""

    @pytest.mark.asyncio
    async def test_get_owned_happy_path(self):
        """Get owned template returns 200."""
        from main import app
        from core.database import get_db

        tpl = _make_template(template_id=TPL_ID, user_id=USER_A)
        db = _make_db()
        db.execute.return_value = _scalar_result(tpl)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/task-templates/{TPL_ID}",
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 200
            body = r.json()
            assert body["id"] == TPL_ID
            assert body["name"] == "My Template"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_get_other_users_template_404(self):
        """Accessing another user's template returns 404 (IDOR protection)."""
        from main import app
        from core.database import get_db

        # The _get_owned query filters on both template_id AND user_id,
        # so if USER_B requests USER_A's template the query returns None → 404
        db = _make_db()
        db.execute.return_value = _scalar_result(None)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/task-templates/{TPL_ID}",
                    headers={"Authorization": f"Bearer {_real_token(USER_B)}"},
                )
            assert r.status_code == 404
            assert "not found" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()


class TestUpdateTemplate:
    """PATCH /v1/task-templates/{id} — partial update."""

    @pytest.mark.asyncio
    async def test_update_happy_path(self):
        """Partial update returns 200 and commits."""
        from main import app
        from core.database import get_db

        tpl = _make_template(template_id=TPL_ID, user_id=USER_A)
        db = _make_db()
        db.execute.return_value = _scalar_result(tpl)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/task-templates/{TPL_ID}",
                    json={"name": "Updated Name"},
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 200
            db.commit.assert_awaited_once()
            # The mock template's name attribute was set
            assert tpl.name == "Updated Name"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_update_other_users_template_404(self):
        """Updating another user's template returns 404."""
        from main import app
        from core.database import get_db

        db = _make_db()
        db.execute.return_value = _scalar_result(None)  # _get_owned returns None
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/task-templates/{TPL_ID}",
                    json={"name": "Hacked"},
                    headers={"Authorization": f"Bearer {_real_token(USER_B)}"},
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()


class TestDeleteTemplate:
    """DELETE /v1/task-templates/{id} — delete a template."""

    @pytest.mark.asyncio
    async def test_delete_happy_path(self):
        """Delete owned template returns 204 and calls db.delete + commit."""
        from main import app
        from core.database import get_db

        tpl = _make_template(template_id=TPL_ID, user_id=USER_A)
        db = _make_db()
        db.execute.return_value = _scalar_result(tpl)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/task-templates/{TPL_ID}",
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 204
            db.delete.assert_awaited_once_with(tpl)
            db.commit.assert_awaited_once()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_delete_other_users_template_404(self):
        """Deleting another user's template returns 404."""
        from main import app
        from core.database import get_db

        db = _make_db()
        db.execute.return_value = _scalar_result(None)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/task-templates/{TPL_ID}",
                    headers={"Authorization": f"Bearer {_real_token(USER_B)}"},
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()


class TestUseTemplate:
    """POST /v1/task-templates/{id}/use — increment counter."""

    @pytest.mark.asyncio
    async def test_use_increments_counter(self):
        """Use endpoint increments use_count and returns expected shape."""
        from main import app
        from core.database import get_db

        tpl = _make_template(template_id=TPL_ID, user_id=USER_A, use_count=3)
        db = _make_db()
        db.execute.return_value = _scalar_result(tpl)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/task-templates/{TPL_ID}/use",
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 200
            body = r.json()
            assert body["id"] == TPL_ID
            assert body["type"] == "web_research"
            assert body["name"] == "My Template"
            assert "default_input" in body
            assert "default_settings" in body
            # use_count was incremented from 3 to 4
            assert tpl.use_count == 4
            db.commit.assert_awaited_once()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_use_other_users_template_404(self):
        """Using another user's template returns 404."""
        from main import app
        from core.database import get_db

        db = _make_db()
        db.execute.return_value = _scalar_result(None)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/task-templates/{TPL_ID}/use",
                    headers={"Authorization": f"Bearer {_real_token(USER_B)}"},
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()


# =============================================================================
# Publish / Unpublish
# =============================================================================

class TestPublishUnpublish:
    """POST/DELETE /v1/task-templates/{id}/publish — marketplace visibility."""

    @pytest.mark.asyncio
    async def test_publish_happy_path(self):
        """Publish sets is_public=True and published_at."""
        from main import app
        from core.database import get_db

        tpl = _make_template(template_id=TPL_ID, user_id=USER_A, is_public=False)
        db = _make_db()
        db.execute.return_value = _scalar_result(tpl)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/task-templates/{TPL_ID}/publish",
                    json={
                        "marketplace_title": "Awesome Research",
                        "marketplace_description": "Deep web research template",
                        "marketplace_tags": ["research", "web"],
                    },
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 200
            assert tpl.is_public is True
            assert tpl.published_at is not None
            assert tpl.marketplace_title == "Awesome Research"
            assert tpl.marketplace_description == "Deep web research template"
            assert tpl.marketplace_tags == ["research", "web"]
            db.commit.assert_awaited_once()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_publish_with_empty_body(self):
        """Publish with empty body still sets is_public=True."""
        from main import app
        from core.database import get_db

        tpl = _make_template(template_id=TPL_ID, user_id=USER_A, is_public=False)
        db = _make_db()
        db.execute.return_value = _scalar_result(tpl)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/task-templates/{TPL_ID}/publish",
                    json={},
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 200
            assert tpl.is_public is True
            assert tpl.published_at is not None
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_unpublish_happy_path(self):
        """Unpublish sets is_public=False and published_at=None."""
        from main import app
        from core.database import get_db

        tpl = _make_template(
            template_id=TPL_ID,
            user_id=USER_A,
            is_public=True,
            published_at=_now(),
        )
        db = _make_db()
        db.execute.return_value = _scalar_result(tpl)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/task-templates/{TPL_ID}/publish",
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 204
            assert tpl.is_public is False
            assert tpl.published_at is None
            db.commit.assert_awaited_once()
        finally:
            app.dependency_overrides.clear()


# =============================================================================
# Marketplace — Browse, Get, Import
# =============================================================================

class TestMarketplaceBrowse:
    """GET /v1/template-marketplace — browse public templates."""

    @pytest.mark.asyncio
    async def test_browse_returns_results(self):
        """Browse marketplace returns templates with author info."""
        from main import app
        from core.database import get_db

        tpl = _make_template(
            user_id=USER_A,
            is_public=True,
            marketplace_title="Public Template",
            published_at=_now(),
        )
        author = _make_user(user_id=USER_A, name="Alice")
        db = _make_db()
        call_num = [0]

        def _side_effect(stmt):
            call_num[0] += 1
            if call_num[0] == 2:
                # Second execute: the author lookup
                return _scalars_result([author])
            # First execute: the template query
            return _scalars_result([tpl])

        db.execute.side_effect = _side_effect
        db.scalar.return_value = 1  # total count
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/template-marketplace",
                    headers={"Authorization": f"Bearer {_real_token(USER_B)}"},
                )
            assert r.status_code == 200
            body = r.json()
            assert "templates" in body
            assert "total" in body
            assert body["total"] == 1
            assert len(body["templates"]) == 1
            assert body["templates"][0]["author_name"] == "Alice"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_browse_empty(self):
        """Browse marketplace with no public templates returns empty list."""
        from main import app
        from core.database import get_db

        db = _make_db()
        db.execute.return_value = _scalars_result([])
        db.scalar.return_value = 0
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/template-marketplace",
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 200
            body = r.json()
            assert body["templates"] == []
            assert body["total"] == 0
            assert body["has_next"] is False
        finally:
            app.dependency_overrides.clear()


class TestMarketplaceGetSingle:
    """GET /v1/template-marketplace/{id} — get a single public template."""

    @pytest.mark.asyncio
    async def test_get_public_template(self):
        """Get a public template returns 200 with author info."""
        from main import app
        from core.database import get_db

        tpl = _make_template(
            template_id=TPL_ID,
            user_id=USER_A,
            is_public=True,
            marketplace_title="Public Research",
            published_at=_now(),
        )
        author = _make_user(user_id=USER_A, name="Alice")
        db = _make_db()
        call_num = [0]

        def _side_effect(stmt):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar_result(tpl)     # template query
            return _scalar_result(author)      # author query

        db.execute.side_effect = _side_effect
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/template-marketplace/{TPL_ID}",
                    headers={"Authorization": f"Bearer {_real_token(USER_B)}"},
                )
            assert r.status_code == 200
            body = r.json()
            assert body["id"] == TPL_ID
            assert body["marketplace_title"] == "Public Research"
            assert body["author_name"] == "Alice"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_get_nonpublic_template_404(self):
        """Getting a non-public template from the marketplace returns 404."""
        from main import app
        from core.database import get_db

        db = _make_db()
        # The query filters is_public=True, so a private template returns None
        db.execute.return_value = _scalar_result(None)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/template-marketplace/{uuid.uuid4()}",
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 404
            assert "marketplace" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()


class TestMarketplaceImport:
    """POST /v1/template-marketplace/{id}/import — import a public template."""

    @pytest.mark.asyncio
    async def test_import_happy_path(self):
        """Import creates a copy with '(imported)' suffix and increments import_count."""
        from main import app
        from core.database import get_db

        original = _make_template(
            template_id=TPL_ID,
            user_id=USER_A,
            is_public=True,
            marketplace_title="Public Template",
            import_count=5,
        )
        db = _make_db()
        # First execute: fetch the original public template
        db.execute.return_value = _scalar_result(original)
        # scalar: count of user's existing templates (under limit)
        db.scalar.return_value = 3
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/template-marketplace/{TPL_ID}/import",
                    headers={"Authorization": f"Bearer {_real_token(USER_B)}"},
                )
            assert r.status_code == 201
            # Original import_count incremented
            assert original.import_count == 6
            # db.add called with the copy
            db.add.assert_called_once()
            db.commit.assert_awaited_once()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_import_not_found_404(self):
        """Import a non-existent or non-public template returns 404."""
        from main import app
        from core.database import get_db

        db = _make_db()
        db.execute.return_value = _scalar_result(None)
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/template-marketplace/{uuid.uuid4()}/import",
                    headers={"Authorization": f"Bearer {_real_token(USER_B)}"},
                )
            assert r.status_code == 404
            assert "marketplace" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_import_own_template_400(self):
        """Importing your own template returns 400."""
        from main import app
        from core.database import get_db

        own_tpl = _make_template(
            template_id=TPL_ID,
            user_id=USER_A,
            is_public=True,
        )
        db = _make_db()
        db.execute.return_value = _scalar_result(own_tpl)
        db.scalar.return_value = 3  # under limit
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/template-marketplace/{TPL_ID}/import",
                    headers={"Authorization": f"Bearer {_real_token(USER_A)}"},
                )
            assert r.status_code == 400
            assert "cannot import your own" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_import_at_max_limit_400(self):
        """Import fails with 400 when user already has 50 templates."""
        from main import app
        from core.database import get_db

        original = _make_template(
            template_id=TPL_ID,
            user_id=USER_A,
            is_public=True,
        )
        db = _make_db()
        db.execute.return_value = _scalar_result(original)
        db.scalar.return_value = 50  # at the limit
        app.dependency_overrides[get_db] = _db_override(db)

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/template-marketplace/{TPL_ID}/import",
                    headers={"Authorization": f"Bearer {_real_token(USER_B)}"},
                )
            assert r.status_code == 400
            assert "50" in r.json()["detail"]
        finally:
            app.dependency_overrides.clear()


# =============================================================================
# Schema validation (Pydantic)
# =============================================================================

class TestSchemaValidation:
    """Pure Pydantic validation for request schemas."""

    def test_create_request_valid(self):
        """Valid create request passes validation."""
        from models.schemas import RequesterTemplateCreateRequest
        req = RequesterTemplateCreateRequest(**CREATE_PAYLOAD)
        assert req.name == "Research Template"
        assert req.task_type == "web_research"

    def test_create_request_missing_task_type_422(self):
        """Create request without task_type raises ValidationError."""
        import pydantic
        from models.schemas import RequesterTemplateCreateRequest
        with pytest.raises(pydantic.ValidationError):
            RequesterTemplateCreateRequest(name="Test", task_input={}, task_config={})

    def test_create_request_name_too_long(self):
        """Create request with name > 255 chars raises ValidationError."""
        import pydantic
        from models.schemas import RequesterTemplateCreateRequest
        with pytest.raises(pydantic.ValidationError):
            RequesterTemplateCreateRequest(
                name="x" * 256,
                task_type="web_research",
                task_input={},
                task_config={},
            )

    def test_update_request_all_optional(self):
        """Update request with empty body is valid (all fields optional)."""
        from models.schemas import RequesterTemplateUpdateRequest
        req = RequesterTemplateUpdateRequest()
        assert req.name is None
        assert req.description is None
        assert req.task_input is None
        assert req.task_config is None
        assert req.icon is None

    def test_publish_request_defaults(self):
        """Publish request with empty body has correct defaults."""
        from models.schemas import TemplatePublishRequest
        req = TemplatePublishRequest()
        assert req.marketplace_title is None
        assert req.marketplace_description is None
        assert req.marketplace_tags == []

    def test_publish_request_title_too_long(self):
        """Publish request with title > 255 chars raises ValidationError."""
        import pydantic
        from models.schemas import TemplatePublishRequest
        with pytest.raises(pydantic.ValidationError):
            TemplatePublishRequest(marketplace_title="x" * 256)

    def test_create_request_icon_too_long(self):
        """Create request with icon > 8 chars raises ValidationError."""
        import pydantic
        from models.schemas import RequesterTemplateCreateRequest
        with pytest.raises(pydantic.ValidationError):
            RequesterTemplateCreateRequest(
                name="Test",
                task_type="web_research",
                task_input={},
                task_config={},
                icon="x" * 9,
            )
