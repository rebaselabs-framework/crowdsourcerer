"""Tests for task dependency endpoints and uncovered task extra endpoints.

Part 1 — Task Dependencies Router (routers/task_dependencies.py):
  POST   /v1/tasks/{task_id}/dependencies       — add dependency
  GET    /v1/tasks/{task_id}/dependencies       — list upstream deps
  GET    /v1/tasks/{task_id}/dependents          — list downstream dependents
  DELETE /v1/tasks/{task_id}/dependencies/{id}   — remove dependency

Part 2 — Uncovered Task Endpoints (routers/tasks.py):
  GET  /v1/tasks/review-summary                 — pending submission count
  GET  /v1/tasks/tags                           — tag frequency stats
  GET  /v1/tasks/{task_id}/duplicate-params     — form pre-fill params
  GET  /v1/tasks/{task_id}/analytics            — assignment analytics
  GET  /v1/tasks/{task_id}/related              — same-type tasks
  GET  /v1/tasks/{task_id}/suggested-workers    — top workers for type
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_USER_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"
TASK_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TASK_ID2 = "cccccccc-cccc-cccc-cccc-cccccccccccc"
DEP_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
WORKER_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _token(user_id: str) -> str:
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
        # Simulate DB-generated defaults after commit
        if not getattr(obj, "created_at", None):
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
    """Wrap a single value in a mock execute() result."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value)
    r.scalar = MagicMock(return_value=value if not isinstance(value, MagicMock) else 0)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all = MagicMock(return_value=[])
    return r


def _scalars_list(items: list):
    """Wrap a list in a mock execute() result with .scalars().all()."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalar_one = MagicMock(return_value=items[0] if items else None)
    r.scalar = MagicMock(return_value=len(items))
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    r.all = MagicMock(return_value=items)
    return r


def _make_task(
    task_id: str = TASK_ID,
    user_id: str = USER_ID,
    status: str = "pending",
    task_type: str = "label_image",
    execution_mode: str = "human",
):
    t = MagicMock()
    t.id = uuid.UUID(task_id)
    t.user_id = user_id
    t.type = task_type
    t.status = status
    t.execution_mode = execution_mode
    t.input = {"title": "Test Task", "url": "https://example.com/img.jpg"}
    t.output = None
    t.priority = "normal"
    t.tags = ["test", "label"]
    t.scheduled_at = None
    t.created_at = NOW
    t.completed_at = None
    t.credits_used = 5
    t.webhook_url = None
    t.webhook_events = None
    t.worker_reward_credits = 2
    t.assignments_required = 1
    t.claim_timeout_minutes = 30
    t.task_instructions = "Label this image"
    t.consensus_strategy = "any_first"
    t.min_skill_level = None
    t.is_gold_standard = False
    t.gold_answer = None
    t.org_id = None
    t.duration_ms = None
    t.title = "Test Task"
    return t


def _make_dep(
    dep_id: str = DEP_ID,
    task_id: str = TASK_ID,
    depends_on_id: str = TASK_ID2,
):
    d = MagicMock()
    d.id = uuid.UUID(dep_id)
    d.task_id = uuid.UUID(task_id)
    d.depends_on_id = uuid.UUID(depends_on_id)
    d.created_at = NOW
    return d


def _make_app():
    from fastapi import FastAPI
    from routers.task_dependencies import router as deps_router
    from routers.tasks import router as tasks_router
    from core.database import get_db

    app = FastAPI()
    app.include_router(tasks_router)
    app.include_router(deps_router)
    return app, get_db


# ===========================================================================
# PART 1: Task Dependencies Router
# ===========================================================================

# ── POST /v1/tasks/{task_id}/dependencies ─────────────────────────────────

@pytest.mark.asyncio
async def test_add_dependency_happy_path():
    """Successfully add a dependency between two owned pending tasks."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    upstream = _make_task(task_id=TASK_ID2, status="pending")

    call_count = 0

    async def _execute_side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # _get_owned_task for task_id
            return _scalar(task)
        if call_count == 2:
            # upstream task lookup
            return _scalar(upstream)
        if call_count == 3:
            # cycle check — no cycle
            return _scalar(None)
        if call_count == 4:
            # duplicate check — not duplicate
            return _scalar(None)
        return _scalar(None)

    db.execute = AsyncMock(side_effect=_execute_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/dependencies",
            json={"depends_on_id": TASK_ID2},
            headers=_auth(),
        )
    assert r.status_code == 201
    body = r.json()
    assert body["task_id"] == TASK_ID
    assert body["depends_on_id"] == TASK_ID2
    assert body["depends_on_title"] == "Test Task"
    assert body["depends_on_status"] == "pending"
    db.add.assert_called_once()
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_add_dependency_self_dep_rejected():
    """A task cannot depend on itself."""
    app, get_db = _make_app()
    db = _make_mock_db()
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/dependencies",
            json={"depends_on_id": TASK_ID},
            headers=_auth(),
        )
    assert r.status_code == 400
    assert "cannot depend on itself" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_add_dependency_task_not_found():
    """404 when the downstream task does not exist."""
    app, get_db = _make_app()
    db = _make_mock_db()
    db.execute = AsyncMock(return_value=_scalar(None))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/dependencies",
            json={"depends_on_id": TASK_ID2},
            headers=_auth(),
        )
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_add_dependency_not_owned_403():
    """403 when the task belongs to a different user."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task(user_id=OTHER_USER_ID)
    db.execute = AsyncMock(return_value=_scalar(task))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/dependencies",
            json={"depends_on_id": TASK_ID2},
            headers=_auth(),
        )
    assert r.status_code == 403
    assert "not your task" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_add_dependency_task_not_pending():
    """Only pending tasks can have dependencies added."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task(status="completed")
    db.execute = AsyncMock(return_value=_scalar(task))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/dependencies",
            json={"depends_on_id": TASK_ID2},
            headers=_auth(),
        )
    assert r.status_code == 400
    assert "pending" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_add_dependency_upstream_not_found():
    """404 when the upstream task does not exist."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        return _scalar(None)  # upstream not found

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/dependencies",
            json={"depends_on_id": TASK_ID2},
            headers=_auth(),
        )
    assert r.status_code == 404
    assert "upstream" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_add_dependency_upstream_different_user():
    """403 when upstream belongs to another user."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    upstream = _make_task(task_id=TASK_ID2, user_id=OTHER_USER_ID)

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        return _scalar(upstream)

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/dependencies",
            json={"depends_on_id": TASK_ID2},
            headers=_auth(),
        )
    assert r.status_code == 403
    assert "another user" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_add_dependency_cycle_detected():
    """400 when a direct cycle would be created."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    upstream = _make_task(task_id=TASK_ID2)
    existing_cycle = _make_dep()  # upstream already depends on task

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        if call_count == 2:
            return _scalar(upstream)
        if call_count == 3:
            return _scalar(existing_cycle)  # cycle found
        return _scalar(None)

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/dependencies",
            json={"depends_on_id": TASK_ID2},
            headers=_auth(),
        )
    assert r.status_code == 400
    assert "cycle" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_add_dependency_duplicate_409():
    """409 when the dependency already exists."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    upstream = _make_task(task_id=TASK_ID2)
    existing_dep = _make_dep()

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        if call_count == 2:
            return _scalar(upstream)
        if call_count == 3:
            return _scalar(None)  # no cycle
        if call_count == 4:
            return _scalar(existing_dep)  # duplicate
        return _scalar(None)

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/dependencies",
            json={"depends_on_id": TASK_ID2},
            headers=_auth(),
        )
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_add_dependency_unauthenticated():
    """401 when no auth header is provided."""
    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/dependencies",
            json={"depends_on_id": TASK_ID2},
        )
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_add_dependency_task_status_running():
    """400 for tasks in 'running' status."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task(status="running")
    db.execute = AsyncMock(return_value=_scalar(task))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/dependencies",
            json={"depends_on_id": TASK_ID2},
            headers=_auth(),
        )
    assert r.status_code == 400
    assert "pending" in r.json()["detail"].lower()


# ── GET /v1/tasks/{task_id}/dependencies ──────────────────────────────────

@pytest.mark.asyncio
async def test_list_dependencies_happy_path():
    """List upstream dependencies for a task."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    dep = _make_dep()
    upstream = _make_task(task_id=TASK_ID2, status="completed")

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # _get_owned_task
            return _scalar(task)
        # join query: returns (dep, upstream) tuples
        r = MagicMock()
        r.all = MagicMock(return_value=[(dep, upstream)])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/dependencies",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["depends_on_id"] == TASK_ID2
    assert body[0]["depends_on_status"] == "completed"


@pytest.mark.asyncio
async def test_list_dependencies_empty():
    """Empty list when task has no dependencies."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/dependencies",
            headers=_auth(),
        )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_dependencies_task_not_found():
    """404 when task does not exist."""
    app, get_db = _make_app()
    db = _make_mock_db()
    db.execute = AsyncMock(return_value=_scalar(None))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/dependencies",
            headers=_auth(),
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_dependencies_unauthenticated():
    """401 without auth."""
    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/v1/tasks/{TASK_ID}/dependencies")
    assert r.status_code in (401, 403)


# ── GET /v1/tasks/{task_id}/dependents ────────────────────────────────────

@pytest.mark.asyncio
async def test_list_dependents_happy_path():
    """List downstream dependents for a task."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    dep = _make_dep(task_id=TASK_ID2, depends_on_id=TASK_ID)
    downstream = _make_task(task_id=TASK_ID2, status="pending")

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[(dep, downstream)])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/dependents",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["task_id"] == TASK_ID2


@pytest.mark.asyncio
async def test_list_dependents_empty():
    """Empty list when no downstream tasks depend on this one."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/dependents",
            headers=_auth(),
        )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_dependents_not_owned():
    """403 when the task belongs to another user."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task(user_id=OTHER_USER_ID)
    db.execute = AsyncMock(return_value=_scalar(task))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/dependents",
            headers=_auth(),
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_list_dependents_unauthenticated():
    """401 without auth."""
    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/v1/tasks/{TASK_ID}/dependents")
    assert r.status_code in (401, 403)


# ── DELETE /v1/tasks/{task_id}/dependencies/{dep_id} ──────────────────────

@pytest.mark.asyncio
async def test_remove_dependency_happy_path():
    """Successfully remove a dependency."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    dep = _make_dep()

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        return _scalar(dep)

    db.execute = AsyncMock(side_effect=_side)
    deleted_objects = []

    async def _track_delete(obj):
        deleted_objects.append(obj)

    db.delete = _track_delete
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete(
            f"/v1/tasks/{TASK_ID}/dependencies/{DEP_ID}",
            headers=_auth(),
        )
    assert r.status_code == 204
    assert len(deleted_objects) == 1
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_remove_dependency_not_found():
    """404 when the dependency edge does not exist."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        return _scalar(None)  # dep not found

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete(
            f"/v1/tasks/{TASK_ID}/dependencies/{DEP_ID}",
            headers=_auth(),
        )
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_remove_dependency_task_not_found():
    """404 when the parent task does not exist."""
    app, get_db = _make_app()
    db = _make_mock_db()
    db.execute = AsyncMock(return_value=_scalar(None))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete(
            f"/v1/tasks/{TASK_ID}/dependencies/{DEP_ID}",
            headers=_auth(),
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_remove_dependency_unauthenticated():
    """401 without auth."""
    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete(f"/v1/tasks/{TASK_ID}/dependencies/{DEP_ID}")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_remove_dependency_not_owned():
    """403 when the task belongs to another user."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task(user_id=OTHER_USER_ID)
    db.execute = AsyncMock(return_value=_scalar(task))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete(
            f"/v1/tasks/{TASK_ID}/dependencies/{DEP_ID}",
            headers=_auth(),
        )
    assert r.status_code == 403


# ===========================================================================
# PART 2: Uncovered Task Endpoints
# ===========================================================================

# ── GET /v1/tasks/review-summary ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_summary_happy_path():
    """Returns pending_count for submitted worker assignments."""
    app, get_db = _make_app()
    db = _make_mock_db()

    result = MagicMock()
    result.scalar = MagicMock(return_value=7)
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/tasks/review-summary", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"pending_count": 7}


@pytest.mark.asyncio
async def test_review_summary_zero():
    """Returns 0 when no submissions are pending."""
    app, get_db = _make_app()
    db = _make_mock_db()

    result = MagicMock()
    result.scalar = MagicMock(return_value=0)
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/tasks/review-summary", headers=_auth())
    assert r.status_code == 200
    assert r.json()["pending_count"] == 0


@pytest.mark.asyncio
async def test_review_summary_null_scalar():
    """Returns 0 when scalar returns None (no matching rows)."""
    app, get_db = _make_app()
    db = _make_mock_db()

    result = MagicMock()
    result.scalar = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/tasks/review-summary", headers=_auth())
    assert r.status_code == 200
    assert r.json()["pending_count"] == 0


@pytest.mark.asyncio
async def test_review_summary_unauthenticated():
    """401 without auth."""
    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/tasks/review-summary")
    assert r.status_code in (401, 403)


# ── GET /v1/tasks/tags ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tags_happy_path():
    """Returns tag stats sorted by frequency."""
    app, get_db = _make_app()
    db = _make_mock_db()

    # Simulate rows of tags arrays
    tags_rows = [
        ["python", "ml"],
        ["python", "data"],
        ["ml"],
    ]
    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=tags_rows)))
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/tasks/tags", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    # python=2, ml=2, data=1
    tags_by_name = {t["tag"]: t["count"] for t in body}
    assert tags_by_name["python"] == 2
    assert tags_by_name["ml"] == 2
    assert tags_by_name["data"] == 1


@pytest.mark.asyncio
async def test_tags_empty():
    """Empty list when no tasks have tags."""
    app, get_db = _make_app()
    db = _make_mock_db()

    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/tasks/tags", headers=_auth())
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_tags_none_entries_skipped():
    """None entries in tags arrays are handled gracefully."""
    app, get_db = _make_app()
    db = _make_mock_db()

    tags_rows = [
        None,  # some tasks have None tags
        ["valid"],
    ]
    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=tags_rows)))
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/tasks/tags", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["tag"] == "valid"


@pytest.mark.asyncio
async def test_tags_unauthenticated():
    """401 without auth."""
    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/tasks/tags")
    assert r.status_code in (401, 403)


# ── GET /v1/tasks/{task_id}/duplicate-params ──────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_params_happy_path():
    """Returns task params for form pre-fill."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task(execution_mode="human")
    db.execute = AsyncMock(return_value=_scalar(task))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/duplicate-params",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["source_task_id"] == TASK_ID
    assert body["type"] == "label_image"
    assert body["priority"] == "normal"
    assert body["tags"] == ["test", "label"]
    # Human task params
    assert body["worker_reward_credits"] == 2
    assert body["assignments_required"] == 1
    assert body["claim_timeout_minutes"] == 30
    assert body["task_instructions"] == "Label this image"
    assert body["consensus_strategy"] == "any_first"


@pytest.mark.asyncio
async def test_duplicate_params_ai_task():
    """AI tasks do not include human-specific params."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task(execution_mode="ai", task_type="llm_generate")
    db.execute = AsyncMock(return_value=_scalar(task))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/duplicate-params",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "llm_generate"
    # No human-specific params
    assert "worker_reward_credits" not in body
    assert "assignments_required" not in body


@pytest.mark.asyncio
async def test_duplicate_params_with_webhook():
    """Webhook params are included when present."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    task.webhook_url = "https://example.com/hook"
    task.webhook_events = ["task.completed"]
    db.execute = AsyncMock(return_value=_scalar(task))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/duplicate-params",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["webhook_url"] == "https://example.com/hook"
    assert body["webhook_events"] == ["task.completed"]


@pytest.mark.asyncio
async def test_duplicate_params_not_found():
    """404 when task not found or not owned."""
    app, get_db = _make_app()
    db = _make_mock_db()
    db.execute = AsyncMock(return_value=_scalar(None))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/duplicate-params",
            headers=_auth(),
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_params_unauthenticated():
    """401 without auth."""
    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/v1/tasks/{TASK_ID}/duplicate-params")
    assert r.status_code in (401, 403)


# ── GET /v1/tasks/{task_id}/analytics ─────────────────────────────────────

def _make_assignment(
    status: str = "approved",
    earnings: int = 10,
    worker_id: str = WORKER_ID,
    has_response: bool = True,
):
    a = MagicMock()
    a.status = status
    a.earnings_credits = earnings
    a.submitted_at = NOW
    a.claimed_at = NOW - timedelta(minutes=5)
    a.worker_id = uuid.UUID(worker_id)
    a.response = {"label": "cat"} if has_response else None
    a.xp_earned = 5
    return a


@pytest.mark.asyncio
async def test_analytics_happy_path():
    """Returns full analytics with assignments."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task(status="completed")

    assignment1 = _make_assignment(status="approved", earnings=10)
    assignment2 = _make_assignment(status="rejected", earnings=0)

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        # Assignments query: returns (assignment, worker_name) tuples
        r = MagicMock()
        r.all = MagicMock(return_value=[
            (assignment1, "Alice"),
            (assignment2, "Bob"),
        ])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/analytics",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == TASK_ID
    assert body["task_type"] == "label_image"
    assert body["total_assignments"] == 2
    assert body["approved_count"] == 1
    assert body["rejected_count"] == 1
    assert body["pending_count"] == 0
    assert body["total_credits_paid"] == 10  # 10 + 0
    assert body["avg_response_minutes"] is not None
    assert body["is_gold_standard"] is False
    assert len(body["assignments"]) == 2


@pytest.mark.asyncio
async def test_analytics_no_assignments():
    """Analytics with zero assignments."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task(status="open")

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/analytics",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["total_assignments"] == 0
    assert body["approved_count"] == 0
    assert body["avg_response_minutes"] is None
    assert body["assignments"] == []


@pytest.mark.asyncio
async def test_analytics_gold_standard():
    """Gold standard task with accuracy calculation."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    task.is_gold_standard = True
    task.gold_answer = {"label": "cat"}

    # One accurate, one inaccurate
    a1 = _make_assignment(status="approved")
    a1.response = {"label": "cat"}
    a2 = _make_assignment(status="approved")
    a2.response = {"label": "dog"}

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[(a1, "Alice"), (a2, "Bob")])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/analytics",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["is_gold_standard"] is True
    assert body["accuracy_rate"] == 0.5  # 1 of 2 correct


@pytest.mark.asyncio
async def test_analytics_response_distribution():
    """Response distribution tracks label frequencies."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()

    a1 = _make_assignment(status="approved")
    a1.response = {"label": "cat"}
    a2 = _make_assignment(status="approved")
    a2.response = {"label": "cat"}
    a3 = _make_assignment(status="submitted")
    a3.response = {"label": "dog"}

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[(a1, "A"), (a2, "B"), (a3, "C")])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/analytics",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["response_distribution"]["cat"] == 2
    assert body["response_distribution"]["dog"] == 1


@pytest.mark.asyncio
async def test_analytics_not_found():
    """404 when task not found or not owned."""
    app, get_db = _make_app()
    db = _make_mock_db()
    db.execute = AsyncMock(return_value=_scalar(None))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/analytics",
            headers=_auth(),
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_analytics_unauthenticated():
    """401 without auth."""
    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/v1/tasks/{TASK_ID}/analytics")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_analytics_response_time_calculation():
    """Average response minutes calculated from claimed_at to submitted_at."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()

    a1 = _make_assignment(status="approved")
    a1.claimed_at = NOW - timedelta(minutes=10)
    a1.submitted_at = NOW
    a2 = _make_assignment(status="approved")
    a2.claimed_at = NOW - timedelta(minutes=20)
    a2.submitted_at = NOW

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[(a1, "A"), (a2, "B")])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/analytics",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    # (10 + 20) / 2 = 15
    assert body["avg_response_minutes"] == pytest.approx(15.0, abs=0.1)


# ── GET /v1/tasks/{task_id}/related ───────────────────────────────────────

@pytest.mark.asyncio
async def test_related_tasks_happy_path():
    """Returns related tasks of the same type."""
    app, get_db = _make_app()
    db = _make_mock_db()

    related1 = _make_task(task_id=TASK_ID2, status="completed")
    related1.credits_used = 3

    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[related1])))
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/related",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["id"] == TASK_ID2
    assert body[0]["type"] == "label_image"
    assert body[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_related_tasks_empty():
    """Empty list when no related tasks exist."""
    app, get_db = _make_app()
    db = _make_mock_db()

    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/related",
            headers=_auth(),
        )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_related_tasks_custom_limit():
    """Respects the limit query parameter."""
    app, get_db = _make_app()
    db = _make_mock_db()

    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/related?limit=3",
            headers=_auth(),
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_related_tasks_unauthenticated():
    """401 without auth."""
    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/v1/tasks/{TASK_ID}/related")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_related_tasks_response_shape():
    """Response items have the expected keys."""
    app, get_db = _make_app()
    db = _make_mock_db()

    t = _make_task(task_id=TASK_ID2, status="open")
    t.completed_at = NOW

    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[t])))
    db.execute = AsyncMock(return_value=result)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/related",
            headers=_auth(),
        )
    assert r.status_code == 200
    item = r.json()[0]
    expected_keys = {"id", "type", "status", "execution_mode", "credits_used",
                     "priority", "tags", "created_at", "completed_at"}
    assert expected_keys == set(item.keys())


# ── GET /v1/tasks/{task_id}/suggested-workers ─────────────────────────────

def _make_worker_skill(worker_id: str = WORKER_ID):
    s = MagicMock()
    s.worker_id = uuid.UUID(worker_id)
    s.proficiency_level = 3
    s.accuracy = 0.95
    s.tasks_completed = 42
    s.verified = True
    s.last_task_at = NOW - timedelta(hours=2)
    return s


def _make_user_for_worker(name: str = "Alice", worker_id: str = WORKER_ID):
    u = MagicMock()
    u.id = uuid.UUID(worker_id)
    u.name = name
    u.email = "alice@example.com"
    u.avatar_url = "https://example.com/alice.jpg"
    u.reputation_score = 85.5
    u.availability_status = "available"
    u.is_banned = False
    return u


@pytest.mark.asyncio
async def test_suggested_workers_happy_path():
    """Returns ranked workers for the task type."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    skill = _make_worker_skill()
    user = _make_user_for_worker()

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[(skill, user)])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/suggested-workers",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    w = body[0]
    assert w["worker_id"] == WORKER_ID
    assert w["display_name"] == "Alice"
    assert w["proficiency_level"] == 3
    assert w["accuracy"] == 0.95
    assert w["tasks_completed"] == 42
    assert w["verified"] is True
    assert w["reputation_score"] == 85.5
    assert w["availability_status"] == "available"


@pytest.mark.asyncio
async def test_suggested_workers_empty():
    """Empty list when no workers match."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/suggested-workers",
            headers=_auth(),
        )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_suggested_workers_not_found():
    """404 when task not found or not owned."""
    app, get_db = _make_app()
    db = _make_mock_db()
    db.execute = AsyncMock(return_value=_scalar(None))
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/suggested-workers",
            headers=_auth(),
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_suggested_workers_unauthenticated():
    """401 without auth."""
    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/v1/tasks/{TASK_ID}/suggested-workers")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_suggested_workers_no_name_uses_email_prefix():
    """When worker name is None, display_name falls back to email prefix."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    skill = _make_worker_skill()
    user = _make_user_for_worker(name=None)
    user.name = None

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[(skill, user)])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/suggested-workers",
            headers=_auth(),
        )
    assert r.status_code == 200
    body = r.json()
    assert body[0]["display_name"] == "alice"  # email prefix


@pytest.mark.asyncio
async def test_suggested_workers_null_accuracy():
    """accuracy=None is returned as null in response."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    skill = _make_worker_skill()
    skill.accuracy = None
    user = _make_user_for_worker()

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[(skill, user)])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/suggested-workers",
            headers=_auth(),
        )
    assert r.status_code == 200
    assert r.json()[0]["accuracy"] is None


@pytest.mark.asyncio
async def test_suggested_workers_null_reputation():
    """reputation_score=None defaults to 50.0."""
    app, get_db = _make_app()
    db = _make_mock_db()
    task = _make_task()
    skill = _make_worker_skill()
    user = _make_user_for_worker()
    user.reputation_score = None

    call_count = 0

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(task)
        r = MagicMock()
        r.all = MagicMock(return_value=[(skill, user)])
        return r

    db.execute = AsyncMock(side_effect=_side)
    app.dependency_overrides[get_db] = _db_override(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            f"/v1/tasks/{TASK_ID}/suggested-workers",
            headers=_auth(),
        )
    assert r.status_code == 200
    assert r.json()[0]["reputation_score"] == 50.0


# ===========================================================================
# PART 3: _fmt helper unit tests (task dependencies)
# ===========================================================================

def test_fmt_extracts_title_from_input_dict():
    """_fmt extracts title from upstream.input dict."""
    from routers.task_dependencies import _fmt

    dep = _make_dep()
    upstream = _make_task(task_id=TASK_ID2)
    upstream.input = {"title": "My Special Task"}
    result = _fmt(dep, upstream)
    assert result.depends_on_title == "My Special Task"


def test_fmt_fallback_title_from_type():
    """_fmt falls back to formatted type when input has no title."""
    from routers.task_dependencies import _fmt

    dep = _make_dep()
    upstream = _make_task(task_id=TASK_ID2)
    upstream.input = {"url": "https://example.com"}
    result = _fmt(dep, upstream)
    assert result.depends_on_title == "Label Image"


def test_fmt_non_dict_input():
    """_fmt handles non-dict input by using raw type string."""
    from routers.task_dependencies import _fmt

    dep = _make_dep()
    upstream = _make_task(task_id=TASK_ID2)
    upstream.input = "raw string input"
    result = _fmt(dep, upstream)
    assert result.depends_on_title == "label_image"
