"""Tests for the platform announcements feature.

Covers:
  - GET /v1/announcements: returns active, non-expired announcements
  - GET /v1/announcements: requires no auth (public endpoint)
  - GET /v1/announcements: returns empty list when none active
  - POST /v1/admin/announcements: requires admin auth (403 for non-admin)
  - POST /v1/admin/announcements: validates required fields
  - PATCH /v1/admin/announcements/{id}: requires admin auth
  - DELETE /v1/admin/announcements/{id}: requires admin auth
  - GET /v1/admin/announcements: requires admin auth
  - AnnouncementCreate schema: validates type enum
  - AnnouncementCreate schema: validates target_role enum
  - AnnouncementOut schema: has expected fields
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "ann-test-secret")
os.environ.setdefault("API_KEY_SALT", "ann-test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Shared IDs ────────────────────────────────────────────────────────────────

ADMIN_ID    = str(uuid.uuid4())
NON_ADMIN_ID = str(uuid.uuid4())
ANN_ID      = str(uuid.uuid4())


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one         = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return r


def _scalars_result(items):
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    r.scalar_one_or_none = MagicMock(return_value=None)
    r.scalar_one = MagicMock(return_value=None)
    return r


def _make_mock_db() -> MagicMock:
    db = MagicMock()
    db.add      = MagicMock()
    db.flush    = AsyncMock()
    db.commit   = AsyncMock()
    db.rollback = AsyncMock()
    db.close    = AsyncMock()
    db.delete   = AsyncMock()
    db.execute  = AsyncMock(return_value=_scalars_result([]))
    db.scalar   = AsyncMock(return_value=0)

    async def _refresh(obj): pass
    db.refresh = _refresh
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _make_admin_user() -> MagicMock:
    u = MagicMock()
    u.id       = uuid.UUID(ADMIN_ID)
    u.is_admin = True
    u.role     = "requester"
    u.credits  = 0
    return u


def _make_announcement(
    ann_id: str = ANN_ID,
    type_: str = "info",
    target_role: str = "all",
    is_active: bool = True,
    expires_at=None,
) -> MagicMock:
    ann = MagicMock()
    ann.id          = uuid.UUID(ann_id)
    ann.title       = "Test announcement"
    ann.message     = "This is a test"
    ann.type        = type_
    ann.target_role = target_role
    ann.is_active   = is_active
    ann.starts_at   = datetime.now(timezone.utc) - timedelta(hours=1)
    ann.expires_at  = expires_at
    ann.created_at  = datetime.now(timezone.utc)
    return ann


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    from main import app as _app
    return _app


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {_token(ADMIN_ID)}"}


@pytest.fixture
def non_admin_headers():
    return {"Authorization": f"Bearer {_token(NON_ADMIN_ID)}"}


# ── Public GET /v1/announcements ──────────────────────────────────────────────

class TestGetAnnouncements:

    @pytest.mark.asyncio
    async def test_public_endpoint_no_auth_required(self, app):
        """GET /v1/announcements is callable without a token."""
        db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/announcements")
            # No 401 — public endpoint
            assert r.status_code != 401, r.text
            assert r.status_code == 200
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_none_active(self, app):
        """Returns [] when no announcements exist."""
        db = _make_mock_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/announcements")
            assert r.status_code == 200
            assert r.json() == []
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_returns_active_announcements(self, app):
        """Returns list of active announcements."""
        ann = _make_announcement()
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalars_result([ann]))

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/announcements")
            assert r.status_code == 200
            data = r.json()
            assert isinstance(data, list)
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Admin POST /v1/admin/announcements ───────────────────────────────────────

class TestCreateAnnouncement:

    @pytest.mark.asyncio
    async def test_requires_admin_auth(self, app, non_admin_headers):
        """Non-admin token returns 403."""
        db = _make_mock_db()
        # User lookup returns non-admin
        non_admin_user = MagicMock()
        non_admin_user.is_admin = False
        db.execute = AsyncMock(return_value=_scalar(non_admin_user))

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/admin/announcements",
                    json={"title": "Test", "message": "Hello"},
                    headers=non_admin_headers,
                )
            assert r.status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_requires_auth(self, app):
        """Unauthenticated request returns 401."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/admin/announcements",
                json={"title": "Test", "message": "Hello"},
            )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_title_returns_422(self, app, admin_headers):
        """Missing required title field returns 422 (Pydantic).

        require_admin resolves via DB before body validation, so we need a
        mock DB that returns an admin user to let the dependency pass.
        """
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/admin/announcements",
                    json={"message": "No title here"},
                    headers=admin_headers,
                )
            assert r.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_missing_message_returns_422(self, app, admin_headers):
        """Missing required message field returns 422 (Pydantic)."""
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/admin/announcements",
                    json={"title": "No message here"},
                    headers=admin_headers,
                )
            assert r.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_type_returns_422(self, app, admin_headers):
        """Invalid type value returns 422."""
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/admin/announcements",
                    json={"title": "T", "message": "M", "type": "critical"},  # invalid
                    headers=admin_headers,
                )
            assert r.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_invalid_target_role_returns_422(self, app, admin_headers):
        """Invalid target_role value returns 422."""
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/admin/announcements",
                    json={"title": "T", "message": "M", "target_role": "admin"},  # invalid
                    headers=admin_headers,
                )
            assert r.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Admin PATCH + DELETE + GET ────────────────────────────────────────────────

class TestAdminCrudAuth:

    @pytest.mark.asyncio
    async def test_admin_list_requires_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/admin/announcements")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_patch_requires_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(f"/v1/admin/announcements/{ANN_ID}", json={"is_active": False})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_delete_requires_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(f"/v1/admin/announcements/{ANN_ID}")
        assert r.status_code == 401


# ── Schema unit tests ─────────────────────────────────────────────────────────

class TestAnnouncementSchema:

    def test_announcement_out_has_required_fields(self):
        from routers.announcements import AnnouncementOut
        fields = AnnouncementOut.model_fields
        for f in ("id", "title", "message", "type", "target_role", "is_active",
                  "starts_at", "created_at"):
            assert f in fields, f"Missing field: {f}"

    def test_announcement_out_expires_at_optional(self):
        from routers.announcements import AnnouncementOut
        assert AnnouncementOut.model_fields["expires_at"].default is None

    def test_create_valid_all_types(self):
        """All valid type values are accepted by AnnouncementCreate."""
        from routers.announcements import AnnouncementCreate
        for t in ("info", "warning", "maintenance", "feature"):
            ann = AnnouncementCreate(title="T", message="M", type=t)
            assert ann.type == t

    def test_create_valid_all_roles(self):
        """All valid target_role values are accepted by AnnouncementCreate."""
        from routers.announcements import AnnouncementCreate
        for role in ("all", "requester", "worker"):
            ann = AnnouncementCreate(title="T", message="M", target_role=role)
            assert ann.target_role == role

    def test_create_defaults(self):
        """AnnouncementCreate defaults: type=info, target_role=all, is_active=True."""
        from routers.announcements import AnnouncementCreate
        ann = AnnouncementCreate(title="T", message="M")
        assert ann.type == "info"
        assert ann.target_role == "all"
        assert ann.is_active is True
        assert ann.expires_at is None
