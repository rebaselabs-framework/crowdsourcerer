"""Tests for search routers: unified search, task search, and global search.

Covers:
  Unified Search (GET /v1/search):
    1.  No auth → 401
    2.  Empty results → SearchResults with total=0, empty arrays
    3.  Results with tasks, pipelines, templates → correct structure
    4.  q too short (1 char) → 422
    5.  q too long (>200 chars) → 422
    6.  entity_types="task" → only tasks returned, pipelines/templates empty
    7.  entity_types="pipeline" → only pipelines returned
    8.  entity_types="template" → only templates returned
    9.  Custom limit parameter respected

  Task Search (GET /v1/search/tasks):
   10.  No auth → 401
   11.  Empty results → total=0, items=[], has_next=False
   12.  Happy path with results → correct item fields
   13.  q too short (1 char) → 422
   14.  Pagination: page/page_size/has_next calculated correctly
   15.  Status filter parameter accepted
   16.  No q param → returns all tasks (q is optional)

  Global Search (GET /v1/search/global):
   17.  No auth → 401
   18.  Empty results → total=0, empty arrays
   19.  Happy path with tasks → task results with correct fields
   20.  Happy path with workers → worker results with display_name, tier, url
   21.  Happy path with workers found by skill → worker_ids from skill query
   22.  Happy path with orgs → org results when user is a member
   23.  No org memberships → orgs array empty, no 5th query
   24.  q too long (>200 chars) → 422
   25.  Combined results → total is sum of all entity counts
   26.  Worker display_name falls back to email prefix when name is None
"""

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NOW = datetime.now(timezone.utc)


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
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _scalars_result(items):
    """Mock db.execute return that supports .scalars().all() and .fetchall()."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=items)
    scalars_mock.__iter__ = MagicMock(return_value=iter(items))
    r.scalars = MagicMock(return_value=scalars_mock)
    r.fetchall = MagicMock(return_value=[(getattr(i, "id", i),) for i in items])
    return r


# ── Mock object factories ────────────────────────────────────────────────────


def _make_task(**kw):
    t = MagicMock()
    t.id = kw.get("id", uuid.uuid4())
    t.type = kw.get("type", "web_research")
    t.task_instructions = kw.get("instructions", "Find info about AI")
    t.status = kw.get("status", "completed")
    t.execution_mode = kw.get("execution_mode", "ai")
    t.priority = kw.get("priority", "normal")
    t.credits_used = kw.get("credits_used", 10)
    t.created_at = kw.get("created_at", NOW)
    t.completed_at = kw.get("completed_at", NOW)
    t.output = kw.get("output", {"result": "test"})
    t.input = kw.get("input", {"query": "test"})
    t.user_id = kw.get("user_id", uuid.UUID(USER_ID))
    t.tags = kw.get("tags", [])
    return t


def _make_pipeline(**kw):
    p = MagicMock()
    p.id = kw.get("id", uuid.uuid4())
    p.name = kw.get("name", "Test Pipeline")
    p.description = kw.get("description", "A test pipeline description")
    p.is_active = kw.get("is_active", True)
    p.created_at = kw.get("created_at", NOW)
    p.user_id = kw.get("user_id", uuid.UUID(USER_ID))
    return p


def _make_template(**kw):
    t = MagicMock()
    t.id = kw.get("id", uuid.uuid4())
    t.name = kw.get("name", "Test Template")
    t.task_type = kw.get("task_type", "web_research")
    t.description = kw.get("description", "A test template")
    t.category = kw.get("category", "General")
    t.is_public = kw.get("is_public", True)
    t.is_featured = kw.get("is_featured", False)
    t.use_count = kw.get("use_count", 5)
    t.created_at = kw.get("created_at", NOW)
    t.creator_id = kw.get("creator_id", uuid.UUID(USER_ID))
    return t


def _make_worker(**kw):
    w = MagicMock()
    w.id = kw.get("id", uuid.uuid4())
    w.name = kw.get("name", "Test Worker")
    w.email = kw.get("email", "worker@example.com")
    w.role = kw.get("role", "worker")
    w.is_active = kw.get("is_active", True)
    w.worker_level = kw.get("worker_level", 3)
    return w


def _make_org(**kw):
    o = MagicMock()
    o.id = kw.get("id", uuid.uuid4())
    o.name = kw.get("name", "Test Org")
    o.slug = kw.get("slug", "test-org")
    return o


# ═════════════════════════════════════════════════════════════════════════════
# Unified Search — GET /v1/search
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_unified_search_no_auth():
    """GET /v1/search without auth → 401."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/search", params={"q": "test"})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_unified_search_empty_results():
    """GET /v1/search with no matching rows → total=0, empty arrays."""
    from main import app
    from core.database import get_db

    db = _make_mock_db()
    # Three queries: tasks, pipelines, templates — all return empty
    db.execute = AsyncMock(side_effect=[
        _scalars_result([]),
        _scalars_result([]),
        _scalars_result([]),
    ])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/search", params={"q": "test"}, headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["query"] == "test"
        assert body["total"] == 0
        assert body["tasks"] == []
        assert body["pipelines"] == []
        assert body["templates"] == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unified_search_with_results():
    """GET /v1/search returns tasks, pipelines, and templates in correct format."""
    from main import app
    from core.database import get_db

    task = _make_task(instructions="Find info about machine learning")
    pipeline = _make_pipeline(name="ML Pipeline", description="Processes ML data")
    template = _make_template(name="ML Research Template", task_type="web_research")

    db = _make_mock_db()
    db.execute = AsyncMock(side_effect=[
        _scalars_result([task]),
        _scalars_result([pipeline]),
        _scalars_result([template]),
    ])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/search", params={"q": "ML"}, headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        assert len(body["tasks"]) == 1
        assert len(body["pipelines"]) == 1
        assert len(body["templates"]) == 1

        # Verify task result shape
        t = body["tasks"][0]
        assert t["entity_type"] == "task"
        assert t["id"] == str(task.id)
        assert "url" in t
        assert t["url"].startswith("/dashboard/tasks/")

        # Verify pipeline result shape
        p = body["pipelines"][0]
        assert p["entity_type"] == "pipeline"
        assert p["title"] == "ML Pipeline"
        assert p["url"] == "/dashboard/pipelines"

        # Verify template result shape
        tmpl = body["templates"][0]
        assert tmpl["entity_type"] == "template"
        assert tmpl["title"] == "ML Research Template"
        assert tmpl["url"] == "/dashboard/marketplace"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unified_search_q_too_short():
    """GET /v1/search with q=1 char → 422 validation error."""
    from main import app
    from core.database import get_db

    db = _make_mock_db()
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/search", params={"q": "x"}, headers=_auth())
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unified_search_q_too_long():
    """GET /v1/search with q > 200 chars → 422 validation error."""
    from main import app
    from core.database import get_db

    db = _make_mock_db()
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/search", params={"q": "a" * 201}, headers=_auth())
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unified_search_entity_types_task_only():
    """GET /v1/search?entity_types=task → only tasks queried, pipelines/templates empty."""
    from main import app
    from core.database import get_db

    task = _make_task(instructions="Research AI papers")
    db = _make_mock_db()
    # Only 1 query for tasks
    db.execute = AsyncMock(return_value=_scalars_result([task]))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search",
                params={"q": "AI", "entity_types": "task"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["tasks"]) == 1
        assert body["pipelines"] == []
        assert body["templates"] == []
        assert body["total"] == 1
        # Only one DB execute call (tasks only)
        assert db.execute.call_count == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unified_search_entity_types_pipeline_only():
    """GET /v1/search?entity_types=pipeline → only pipelines queried."""
    from main import app
    from core.database import get_db

    pipeline = _make_pipeline(name="Data Ingestion Pipeline")
    db = _make_mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([pipeline]))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search",
                params={"q": "Data", "entity_types": "pipeline"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["tasks"] == []
        assert len(body["pipelines"]) == 1
        assert body["templates"] == []
        assert body["total"] == 1
        assert db.execute.call_count == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unified_search_entity_types_template_only():
    """GET /v1/search?entity_types=template → only templates queried."""
    from main import app
    from core.database import get_db

    template = _make_template(name="Summarization Template")
    db = _make_mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([template]))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search",
                params={"q": "Summarize", "entity_types": "template"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["tasks"] == []
        assert body["pipelines"] == []
        assert len(body["templates"]) == 1
        assert body["total"] == 1
        assert db.execute.call_count == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unified_search_custom_limit():
    """GET /v1/search?limit=5 is accepted without error."""
    from main import app
    from core.database import get_db

    db = _make_mock_db()
    db.execute = AsyncMock(side_effect=[
        _scalars_result([]),
        _scalars_result([]),
        _scalars_result([]),
    ])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search",
                params={"q": "test", "limit": 5},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
    finally:
        app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# Task Search — GET /v1/search/tasks
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_task_search_no_auth():
    """GET /v1/search/tasks without auth → 401."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/search/tasks")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_task_search_empty_results():
    """GET /v1/search/tasks with no matches → total=0, empty items."""
    from main import app
    from core.database import get_db

    db = _make_mock_db()
    # scalar for count query returns 0
    db.scalar = AsyncMock(return_value=0)
    # execute for the row query returns empty
    db.execute = AsyncMock(return_value=_scalars_result([]))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/tasks",
                params={"q": "nonexistent"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["items"] == []
        assert body["page"] == 1
        assert body["has_next"] is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_task_search_happy_path():
    """GET /v1/search/tasks with results → correct item structure."""
    from main import app
    from core.database import get_db

    task_id = uuid.uuid4()
    task = _make_task(
        id=task_id,
        type="web_research",
        instructions="Find info about machine learning",
        status="completed",
        execution_mode="ai",
        priority="high",
        credits_used=25,
        tags=["ml", "research"],
    )

    db = _make_mock_db()
    db.scalar = AsyncMock(return_value=1)
    db.execute = AsyncMock(return_value=_scalars_result([task]))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/tasks",
                params={"q": "machine"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1

        item = body["items"][0]
        assert item["id"] == str(task_id)
        assert item["type"] == "web_research"
        assert item["status"] == "completed"
        assert item["execution_mode"] == "ai"
        assert item["priority"] == "high"
        assert item["credits_used"] == 25
        assert item["has_output"] is True
        assert item["tags"] == ["ml", "research"]
        assert "title" in item
        assert item["url"] == f"/dashboard/tasks/{task_id}"
        assert "match_context" in item
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_task_search_q_too_short():
    """GET /v1/search/tasks?q=x (1 char) → 422 validation error."""
    from main import app
    from core.database import get_db

    db = _make_mock_db()
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/tasks",
                params={"q": "x"},
                headers=_auth(),
            )
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_task_search_pagination():
    """GET /v1/search/tasks pagination: has_next=True when more pages exist."""
    from main import app
    from core.database import get_db

    task = _make_task()
    db = _make_mock_db()
    # total=25, page_size=10 → has_next=True on page 1
    db.scalar = AsyncMock(return_value=25)
    db.execute = AsyncMock(return_value=_scalars_result([task]))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/tasks",
                params={"q": "test", "page": 1, "page_size": 10},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 25
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert body["has_next"] is True
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_task_search_status_filter():
    """GET /v1/search/tasks?status=completed is accepted."""
    from main import app
    from core.database import get_db

    task = _make_task(status="completed")
    db = _make_mock_db()
    db.scalar = AsyncMock(return_value=1)
    db.execute = AsyncMock(return_value=_scalars_result([task]))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/tasks",
                params={"status": "completed"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_task_search_no_q_returns_all():
    """GET /v1/search/tasks without q param → returns tasks (q is optional)."""
    from main import app
    from core.database import get_db

    task = _make_task()
    db = _make_mock_db()
    db.scalar = AsyncMock(return_value=1)
    db.execute = AsyncMock(return_value=_scalars_result([task]))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/search/tasks", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        # match_context is None when no search term
        assert body["items"][0]["match_context"] is None
    finally:
        app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# Global Search — GET /v1/search/global
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_global_search_no_auth():
    """GET /v1/search/global without auth → 401."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/search/global", params={"q": "test"})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_global_search_empty_results():
    """GET /v1/search/global with no matches → total=0, all arrays empty."""
    from main import app
    from core.database import get_db

    db = _make_mock_db()
    # 4 queries: tasks, worker skills, workers, org memberships
    db.execute = AsyncMock(side_effect=[
        _scalars_result([]),  # tasks
        _scalars_result([]),  # worker skills by task_type (fetchall)
        _scalars_result([]),  # workers
        _scalars_result([]),  # org memberships (fetchall)
    ])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/global",
                params={"q": "test"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["query"] == "test"
        assert body["total"] == 0
        assert body["tasks"] == []
        assert body["workers"] == []
        assert body["orgs"] == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_global_search_with_tasks():
    """GET /v1/search/global returns task results with correct fields."""
    from main import app
    from core.database import get_db

    task_id = uuid.uuid4()
    task = _make_task(
        id=task_id,
        type="web_research",
        instructions="Find info about AI startups",
        status="completed",
    )

    db = _make_mock_db()
    db.execute = AsyncMock(side_effect=[
        _scalars_result([task]),   # tasks
        _scalars_result([]),       # worker skills
        _scalars_result([]),       # workers
        _scalars_result([]),       # org memberships
    ])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/global",
                params={"q": "AI"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert len(body["tasks"]) == 1

        t = body["tasks"][0]
        assert t["id"] == str(task_id)
        assert t["type"] == "web_research"
        assert t["status"] == "completed"
        assert t["url"] == f"/dashboard/tasks/{task_id}"
        assert "title" in t
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_global_search_with_workers():
    """GET /v1/search/global returns worker results with display_name, tier, url."""
    from main import app
    from core.database import get_db

    worker_id = uuid.uuid4()
    worker = _make_worker(id=worker_id, name="Alice Smith", worker_level=5)

    db = _make_mock_db()
    db.execute = AsyncMock(side_effect=[
        _scalars_result([]),         # tasks
        _scalars_result([]),         # worker skills (fetchall → empty)
        _scalars_result([worker]),   # workers
        _scalars_result([]),         # org memberships
    ])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/global",
                params={"q": "Alice"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert len(body["workers"]) == 1

        w = body["workers"][0]
        assert w["id"] == str(worker_id)
        assert w["display_name"] == "Alice Smith"
        assert w["tier"] == "Level 5"
        assert w["url"] == f"/workers/{worker_id}"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_global_search_workers_by_skill():
    """GET /v1/search/global finds workers via skill match (worker_ids from skill query)."""
    from main import app
    from core.database import get_db

    worker_id = uuid.uuid4()
    worker = _make_worker(id=worker_id, name="Skill Expert", worker_level=4)

    # Build a fetchall result that returns [(worker_id,)]
    skill_result = MagicMock()
    skill_scalars = MagicMock()
    skill_scalars.all = MagicMock(return_value=[])
    skill_result.scalars = MagicMock(return_value=skill_scalars)
    skill_result.fetchall = MagicMock(return_value=[(worker_id,)])

    db = _make_mock_db()
    db.execute = AsyncMock(side_effect=[
        _scalars_result([]),       # tasks
        skill_result,              # worker skills by task_type → [(worker_id,)]
        _scalars_result([worker]), # workers query (includes worker_id from skill match)
        _scalars_result([]),       # org memberships
    ])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/global",
                params={"q": "web_research"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["workers"]) == 1
        assert body["workers"][0]["id"] == str(worker_id)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_global_search_with_orgs():
    """GET /v1/search/global returns org results when user is a member."""
    from main import app
    from core.database import get_db

    org_id = uuid.uuid4()
    org = _make_org(id=org_id, name="Acme Corp", slug="acme-corp")

    # Org membership result: fetchall returns [(org_id,)]
    membership_result = MagicMock()
    membership_scalars = MagicMock()
    membership_scalars.all = MagicMock(return_value=[])
    membership_result.scalars = MagicMock(return_value=membership_scalars)
    membership_result.fetchall = MagicMock(return_value=[(org_id,)])

    db = _make_mock_db()
    db.execute = AsyncMock(side_effect=[
        _scalars_result([]),       # tasks
        _scalars_result([]),       # worker skills
        _scalars_result([]),       # workers
        membership_result,         # org memberships → [(org_id,)]
        _scalars_result([org]),    # orgs query (conditional, triggered by member_org_ids)
    ])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/global",
                params={"q": "Acme"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["orgs"]) == 1

        o = body["orgs"][0]
        assert o["id"] == str(org_id)
        assert o["name"] == "Acme Corp"
        assert o["slug"] == "acme-corp"
        assert o["url"] == "/dashboard/team"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_global_search_no_org_memberships():
    """GET /v1/search/global with no org memberships → orgs empty, no 5th query."""
    from main import app
    from core.database import get_db

    db = _make_mock_db()
    # 4 queries: tasks, skills, workers, org memberships (empty → no 5th query)
    db.execute = AsyncMock(side_effect=[
        _scalars_result([]),  # tasks
        _scalars_result([]),  # worker skills
        _scalars_result([]),  # workers
        _scalars_result([]),  # org memberships → empty fetchall
    ])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/global",
                params={"q": "anything"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["orgs"] == []
        # Only 4 execute calls — no 5th for org details
        assert db.execute.call_count == 4
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_global_search_q_too_long():
    """GET /v1/search/global with q > 200 chars → 422 validation error."""
    from main import app
    from core.database import get_db

    db = _make_mock_db()
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/global",
                params={"q": "z" * 201},
                headers=_auth(),
            )
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_global_search_combined_total():
    """GET /v1/search/global total is sum of tasks + workers + orgs."""
    from main import app
    from core.database import get_db

    task = _make_task(instructions="Search test data")
    worker = _make_worker(name="Test Worker")

    db = _make_mock_db()
    db.execute = AsyncMock(side_effect=[
        _scalars_result([task]),    # 1 task
        _scalars_result([]),        # skills
        _scalars_result([worker]),  # 1 worker
        _scalars_result([]),        # org memberships (empty)
    ])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/global",
                params={"q": "Test"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2  # 1 task + 1 worker + 0 orgs
        assert len(body["tasks"]) == 1
        assert len(body["workers"]) == 1
        assert len(body["orgs"]) == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_global_search_worker_display_name_fallback():
    """Workers with name=None use email prefix as display_name."""
    from main import app
    from core.database import get_db

    worker_id = uuid.uuid4()
    worker = _make_worker(id=worker_id, name=None, email="janedoe@example.com")

    db = _make_mock_db()
    db.execute = AsyncMock(side_effect=[
        _scalars_result([]),        # tasks
        _scalars_result([]),        # skills
        _scalars_result([worker]),  # workers
        _scalars_result([]),        # org memberships
    ])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/search/global",
                params={"q": "jane"},
                headers=_auth(),
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["workers"]) == 1
        assert body["workers"][0]["display_name"] == "janedoe"
    finally:
        app.dependency_overrides.clear()
