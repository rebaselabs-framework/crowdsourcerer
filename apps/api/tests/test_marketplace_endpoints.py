"""Tests for the marketplace router (task template CRUD, rating, cloning).

Covers:
  1.  GET  /v1/marketplace/templates          — unauthenticated → 401
  2.  GET  /v1/marketplace/templates          — happy path: returns paginated list
  3.  GET  /v1/marketplace/templates          — empty list
  4.  GET  /v1/marketplace/templates          — filter by task_type
  5.  GET  /v1/marketplace/templates          — filter by category
  6.  GET  /v1/marketplace/templates          — filter by execution_mode
  7.  GET  /v1/marketplace/templates          — search query
  8.  GET  /v1/marketplace/templates          — sort=popular
  9.  GET  /v1/marketplace/templates          — sort=newest
  10. GET  /v1/marketplace/templates          — sort=top_rated
  11. GET  /v1/marketplace/templates/{id}     — unauthenticated → 401
  12. GET  /v1/marketplace/templates/{id}     — happy path
  13. GET  /v1/marketplace/templates/{id}     — not found → 404
  14. POST /v1/marketplace/templates          — unauthenticated → 401
  15. POST /v1/marketplace/templates          — happy path → 201
  16. POST /v1/marketplace/templates          — missing required field → 422
  17. PATCH /v1/marketplace/templates/{id}    — unauthenticated → 401
  18. PATCH /v1/marketplace/templates/{id}    — happy path
  19. PATCH /v1/marketplace/templates/{id}    — not owner → 404
  20. DELETE /v1/marketplace/templates/{id}   — unauthenticated → 401
  21. DELETE /v1/marketplace/templates/{id}   — happy path → 204
  22. DELETE /v1/marketplace/templates/{id}   — not owner → 404
  23. POST /v1/marketplace/templates/{id}/rate — unauthenticated → 401
  24. POST /v1/marketplace/templates/{id}/rate — happy path (new rating)
  25. POST /v1/marketplace/templates/{id}/rate — update existing rating
  26. POST /v1/marketplace/templates/{id}/rate — template not found → 404
  27. POST /v1/marketplace/templates/{id}/rate — rating out of range → 400
  28. POST /v1/marketplace/templates/{id}/use  — unauthenticated → 401
  29. POST /v1/marketplace/templates/{id}/use  — happy path
  30. POST /v1/marketplace/templates/{id}/use  — not found → 404
  31. POST /v1/marketplace/templates/{id}/clone-task — unauthenticated → 401
  32. POST /v1/marketplace/templates/{id}/clone-task — happy path → 201
  33. POST /v1/marketplace/templates/{id}/clone-task — insufficient credits → 402
  34. POST /v1/marketplace/templates/{id}/clone-task — template not found → 404
  35. GET  /v1/marketplace/categories          — unauthenticated → 401
  36. GET  /v1/marketplace/categories          — happy path
  37. GET  /v1/marketplace/categories          — empty → []
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

USER_ID = str(uuid.uuid4())
OTHER_USER_ID = str(uuid.uuid4())
TEMPLATE_ID = str(uuid.uuid4())


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
        # Simulate DB defaults that SQLAlchemy would set after INSERT
        if getattr(obj, 'rating_count', 'MISSING') is None:
            obj.rating_count = 0
        if getattr(obj, 'rating_sum', 'MISSING') is None:
            obj.rating_sum = 0
        if getattr(obj, 'use_count', 'MISSING') is None:
            obj.use_count = 0
        if getattr(obj, 'created_at', None) is None:
            obj.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        if getattr(obj, 'updated_at', None) is None:
            obj.updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
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
    r.scalar = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all = MagicMock(return_value=[])
    return r


def _result_scalar(val):
    """Mock an execute() result where .scalar() returns val (for count queries)."""
    r = MagicMock()
    r.scalar = MagicMock(return_value=val)
    r.scalar_one_or_none = MagicMock(return_value=val)
    r.scalar_one = MagicMock(return_value=val)
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


def _make_user(user_id=None):
    u = MagicMock()
    u.id = uuid.UUID(user_id or USER_ID)
    u.is_admin = False
    u.role = "requester"
    u.credits = 1000
    u.token_version = 0
    u.is_active = True
    u.is_banned = False
    u.plan = "free"
    return u


def _make_template(template_id=None, creator_id=None):
    t = MagicMock()
    t.id = uuid.UUID(template_id or TEMPLATE_ID)
    t.creator_id = uuid.UUID(creator_id or USER_ID)
    t.name = "Test Template"
    t.description = "A test template"
    t.task_type = "web_research"
    t.execution_mode = "ai"
    t.category = "research"
    t.tags = ["test"]
    t.task_config = {"prompt": "test"}
    t.example_input = {"url": "https://example.com"}
    t.is_public = True
    t.is_featured = False
    t.use_count = 5
    t.rating_sum = 12
    t.rating_count = 3
    t.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t.updated_at = None
    return t


def _template_body():
    return {
        "name": "Test",
        "description": "Test template",
        "task_type": "web_research",
        "execution_mode": "ai",
        "category": "research",
        "tags": ["test"],
        "task_config": {"prompt": "test"},
        "example_input": {"url": "https://example.com"},
        "is_public": True,
    }


def _auth_header(user_id=None):
    return {"Authorization": f"Bearer {_token(user_id or USER_ID)}"}


# ── Test classes ─────────────────────────────────────────────────────────────


class TestListTemplates:
    """GET /v1/marketplace/templates"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/marketplace/templates")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template()
        # db.scalar is called by _ensure_system_templates (returns >0 to skip seeding)
        # AND by the total count query
        db.scalar = AsyncMock(return_value=5)
        # db.execute is called by the main query
        db.execute = AsyncMock(return_value=_scalars_result([template]))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/marketplace/templates", headers=_auth_header())
            assert r.status_code == 200
            body = r.json()
            assert "items" in body
            assert "total" in body
            assert body["total"] == 5
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_empty_list(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.scalar = AsyncMock(return_value=1)  # skip seeding, total=1 but no items
        db.execute = AsyncMock(return_value=_scalars_result([]))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/marketplace/templates", headers=_auth_header())
            assert r.status_code == 200
            body = r.json()
            assert body["items"] == []
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_filter_by_task_type(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.scalar = AsyncMock(return_value=1)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/marketplace/templates",
                    params={"task_type": "web_research"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_filter_by_category(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.scalar = AsyncMock(return_value=1)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/marketplace/templates",
                    params={"category": "research"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_filter_by_execution_mode(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.scalar = AsyncMock(return_value=1)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/marketplace/templates",
                    params={"execution_mode": "ai"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_search_query(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.scalar = AsyncMock(return_value=1)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/marketplace/templates",
                    params={"search": "classification"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_sort_popular(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.scalar = AsyncMock(return_value=1)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/marketplace/templates",
                    params={"sort": "popular"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_sort_newest(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.scalar = AsyncMock(return_value=1)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/marketplace/templates",
                    params={"sort": "newest"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_sort_top_rated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.scalar = AsyncMock(return_value=1)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/marketplace/templates",
                    params={"sort": "top_rated"},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestGetTemplate:
    """GET /v1/marketplace/templates/{id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(f"/v1/marketplace/templates/{TEMPLATE_ID}")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template()
        db.execute = AsyncMock(return_value=_scalar(template))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["id"] == TEMPLATE_ID
            assert body["name"] == "Test Template"
            assert body["task_type"] == "web_research"
            assert body["avg_rating"] == 4.0  # 12 / 3
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
                    f"/v1/marketplace/templates/{uuid.uuid4()}",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestCreateTemplate:
    """POST /v1/marketplace/templates"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post("/v1/marketplace/templates", json=_template_body())
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        # create_template calls db.add, db.commit, db.refresh
        # db.refresh is already mocked to no-op
        # After refresh, the template object's attributes are read —
        # but since we're adding a real TaskTemplateDB-like object, we need
        # the mock refresh to leave the object intact.

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/marketplace/templates",
                    json=_template_body(),
                    headers=_auth_header(),
                )
            assert r.status_code == 201
            body = r.json()
            assert body["name"] == "Test"
            assert body["task_type"] == "web_research"
            assert body["is_public"] is True
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_missing_required_field(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                # Missing 'task_type' which is required
                r = await c.post(
                    "/v1/marketplace/templates",
                    json={"description": "no name or task_type"},
                    headers=_auth_header(),
                )
            assert r.status_code == 422
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestUpdateTemplate:
    """PATCH /v1/marketplace/templates/{id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}",
                    json=_template_body(),
                )
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template(creator_id=USER_ID)
        db.execute = AsyncMock(return_value=_scalar(template))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}",
                    json=_template_body(),
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["name"] == "Test"
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_not_owner(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        # Query returns None because WHERE creator_id = user_id won't match
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}",
                    json=_template_body(),
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestDeleteTemplate:
    """DELETE /v1/marketplace/templates/{id}"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(f"/v1/marketplace/templates/{TEMPLATE_ID}")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template(creator_id=USER_ID)
        db.execute = AsyncMock(return_value=_scalar(template))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 204
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_not_owner(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestRateTemplate:
    """POST /v1/marketplace/templates/{id}/rate"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}/rate",
                    json={"rating": 4},
                )
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_new_rating(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                # First execute: select template with_for_update
                return _scalar(template)
            if call_num[0] == 2:
                # Second execute: select existing rating
                return _scalar(None)  # No existing rating
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}/rate",
                    json={"rating": 4},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["your_rating"] == 4
            assert body["template_id"] == TEMPLATE_ID
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_update_existing_rating(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template()
        existing_rating = MagicMock()
        existing_rating.rating = 3  # Old rating
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(template)
            if call_num[0] == 2:
                return _scalar(existing_rating)  # Existing rating found
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}/rate",
                    json={"rating": 5},
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["your_rating"] == 5
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_template_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{uuid.uuid4()}/rate",
                    json={"rating": 4},
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_rating_out_of_range_zero(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template()
        db.execute = AsyncMock(return_value=_scalar(template))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}/rate",
                    json={"rating": 0},
                    headers=_auth_header(),
                )
            assert r.status_code == 400
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_rating_out_of_range_six(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template()
        db.execute = AsyncMock(return_value=_scalar(template))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}/rate",
                    json={"rating": 6},
                    headers=_auth_header(),
                )
            assert r.status_code == 400
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestUseTemplate:
    """POST /v1/marketplace/templates/{id}/use"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(f"/v1/marketplace/templates/{TEMPLATE_ID}/use")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template()
        db.execute = AsyncMock(return_value=_scalar(template))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}/use",
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["template_id"] == TEMPLATE_ID
            assert body["task_type"] == "web_research"
            assert body["execution_mode"] == "ai"
            assert body["task_config"] == {"prompt": "test"}
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
                    f"/v1/marketplace/templates/{uuid.uuid4()}/use",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestCloneTemplateAsTask:
    """POST /v1/marketplace/templates/{id}/clone-task"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(f"/v1/marketplace/templates/{TEMPLATE_ID}/clone-task")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("core.quotas.record_task_creation", new_callable=AsyncMock)
    @patch("core.quotas.enforce_task_creation_quota", new_callable=AsyncMock)
    async def test_happy_path(self, mock_enforce, mock_record):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template()
        user = _make_user()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                # _get_template: select template
                return _scalar(template)
            if call_num[0] == 2:
                # select user with_for_update
                return _scalar(user)
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        # Mock task object returned after refresh
        task_mock = MagicMock()
        task_mock.id = uuid.uuid4()
        task_mock.status = "pending"

        async def _refresh(obj):
            if hasattr(obj, 'user_id') and not hasattr(obj, 'creator_id'):
                # This is the task object
                obj.id = task_mock.id
                obj.status = "pending"
        db.refresh = _refresh

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}/clone-task",
                    headers=_auth_header(),
                )
            assert r.status_code == 201
            body = r.json()
            assert "task_id" in body
            assert body["task_type"] == "web_research"
            assert body["execution_mode"] == "ai"
            assert body["template_name"] == "Test Template"
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("core.quotas.record_task_creation", new_callable=AsyncMock)
    @patch("core.quotas.enforce_task_creation_quota", new_callable=AsyncMock)
    async def test_insufficient_credits(self, mock_enforce, mock_record):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template()
        # Set credits_cost in task_config
        template.task_config = {"credits_cost": 500}
        user = _make_user()
        user.credits = 10  # Not enough
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(template)
            if call_num[0] == 2:
                return _scalar(user)
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}/clone-task",
                    headers=_auth_header(),
                )
            assert r.status_code == 402
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_template_not_found(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(None))

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{uuid.uuid4()}/clone-task",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("core.quotas.record_task_creation", new_callable=AsyncMock)
    @patch("core.quotas.enforce_task_creation_quota", new_callable=AsyncMock)
    async def test_user_not_found_after_template(self, mock_enforce, mock_record):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template()
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(template)
            if call_num[0] == 2:
                return _scalar(None)  # User not found
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}/clone-task",
                    headers=_auth_header(),
                )
            assert r.status_code == 404
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    @patch("core.quotas.record_task_creation", new_callable=AsyncMock)
    @patch("core.quotas.enforce_task_creation_quota", new_callable=AsyncMock)
    async def test_human_mode_no_credit_check(self, mock_enforce, mock_record):
        """Human execution mode should not check credits for the AI cost."""
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        template = _make_template()
        template.execution_mode = "human"
        template.task_config = {"reward_credits": 5, "workers": 2}
        user = _make_user()
        user.credits = 0  # Zero credits, but human mode shouldn't charge
        call_num = [0]

        def _side_effect(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                return _scalar(template)
            if call_num[0] == 2:
                return _scalar(user)
            return _scalar(None)

        db.execute = AsyncMock(side_effect=_side_effect)

        task_mock = MagicMock()
        task_mock.id = uuid.uuid4()
        task_mock.status = "open"

        async def _refresh(obj):
            if hasattr(obj, 'user_id') and not hasattr(obj, 'creator_id'):
                obj.id = task_mock.id
                obj.status = "open"
        db.refresh = _refresh

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/marketplace/templates/{TEMPLATE_ID}/clone-task",
                    headers=_auth_header(),
                )
            assert r.status_code == 201
            body = r.json()
            assert body["execution_mode"] == "human"
            assert body["status"] == "open"
        finally:
            _app.dependency_overrides.pop(get_db, None)


class TestListCategories:
    """GET /v1/marketplace/categories"""

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get("/v1/marketplace/categories")
            assert r.status_code == 401 or r.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_happy_path(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        # _ensure_system_templates: db.scalar returns >0 to skip seeding
        db.scalar = AsyncMock(return_value=5)
        # Main query: db.execute returns category rows
        row1 = MagicMock()
        row1.__getitem__ = lambda self, i: ("research", 3)[i]
        row2 = MagicMock()
        row2.__getitem__ = lambda self, i: ("nlp", 2)[i]
        result = MagicMock()
        result.all = MagicMock(return_value=[("research", 3), ("nlp", 2)])
        db.execute = AsyncMock(return_value=result)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/marketplace/categories",
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert isinstance(body, list)
            assert len(body) == 2
            assert body[0]["category"] == "research"
            assert body[0]["count"] == 3
        finally:
            _app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_empty_categories(self):
        from main import app as _app
        from core.database import get_db

        db = _make_mock_db()
        db.scalar = AsyncMock(return_value=5)  # skip seeding
        result = MagicMock()
        result.all = MagicMock(return_value=[])
        db.execute = AsyncMock(return_value=result)

        _app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                r = await c.get(
                    "/v1/marketplace/categories",
                    headers=_auth_header(),
                )
            assert r.status_code == 200
            body = r.json()
            assert body == []
        finally:
            _app.dependency_overrides.pop(get_db, None)
