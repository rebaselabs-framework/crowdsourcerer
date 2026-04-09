"""Tests for the worker marketplace router.

Covers:
  Browse workers:
    1.  GET /v1/workers/browse — unauthenticated → 401/403
    2.  GET /v1/workers/browse — empty results (no workers)
    3.  GET /v1/workers/browse — pagination fields present

  Invite worker:
    4.  POST /v1/tasks/{id}/invite — unauthenticated → 401/403
    5.  POST /v1/tasks/{id}/invite — happy path → 201
    6.  POST /v1/tasks/{id}/invite — task not found → 404
    7.  POST /v1/tasks/{id}/invite — AI task → 400
    8.  POST /v1/tasks/{id}/invite — closed task → 400
    9.  POST /v1/tasks/{id}/invite — worker not found → 404
   10.  POST /v1/tasks/{id}/invite — duplicate invite → 409

  Bulk invite:
   11.  POST /v1/tasks/{id}/bulk-invite — happy path → 201

  List task invites:
   12.  GET /v1/tasks/{id}/invites — requester sees invite list
   13.  GET /v1/tasks/{id}/invites — task not found → 404

  List worker invites:
   14.  GET /v1/worker/invites — unauthenticated → 401/403
   15.  GET /v1/worker/invites — empty list

  Respond to invite:
   16.  POST /v1/worker/invites/{id}/respond — accept happy path
   17.  POST /v1/worker/invites/{id}/respond — decline happy path
   18.  POST /v1/worker/invites/{id}/respond — expired invite → 400
   19.  POST /v1/worker/invites/{id}/respond — already responded → 400
   20.  POST /v1/worker/invites/{id}/respond — invalid action → 400

  Watchlist:
   21.  POST /v1/worker/watchlist/{id} — add task → 201
   22.  POST /v1/worker/watchlist/{id} — duplicate → already_watching
   23.  POST /v1/worker/watchlist/{id} — task not found → 404
   24.  POST /v1/worker/watchlist/{id} — limit reached → 400
   25.  GET  /v1/worker/watchlist       — returns items
   26.  GET  /v1/worker/watchlist/check/{id} — watching → true
   27.  GET  /v1/worker/watchlist/check/{id} — not watching → false
   28.  DELETE /v1/worker/watchlist/{id} — remove → 200
   29.  DELETE /v1/worker/watchlist/{id} — not in watchlist → 404
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Fixed IDs ────────────────────────────────────────────────────────────────

REQUESTER_ID = str(uuid.uuid4())
WORKER_ID = str(uuid.uuid4())
WORKER_ID_2 = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())
INVITE_ID = str(uuid.uuid4())


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _make_task(
    task_id: str = TASK_ID,
    user_id: str = REQUESTER_ID,
    status: str = "open",
    execution_mode: str = "human",
    task_type: str = "label_image",
) -> MagicMock:
    t = MagicMock()
    t.id = uuid.UUID(task_id)
    t.user_id = uuid.UUID(user_id) if isinstance(user_id, str) else user_id
    t.status = status
    t.execution_mode = execution_mode
    t.type = task_type
    t.input = {"url": "https://example.com/image.jpg"}
    t.assignments_required = 1
    t.assignments_completed = 0
    t.claim_timeout_minutes = 30
    t.worker_reward_credits = 5
    return t


def _make_worker(worker_id: str = WORKER_ID, role: str = "worker") -> MagicMock:
    w = MagicMock()
    w.id = uuid.UUID(worker_id)
    w.name = "Test Worker"
    w.avatar_url = None
    w.bio = "Expert annotator"
    w.role = role
    w.is_active = True
    w.is_banned = False
    w.profile_public = True
    w.reputation_score = 85.0
    w.worker_level = 3
    w.worker_tasks_completed = 50
    w.worker_accuracy = 0.95
    w.token_version = 0
    w.availability_status = "available"
    return w


def _make_invite(
    invite_id: str = INVITE_ID,
    task_id: str = TASK_ID,
    worker_id: str = WORKER_ID,
    requester_id: str = REQUESTER_ID,
    status: str = "pending",
    created_at: datetime | None = None,
    message: str | None = None,
) -> MagicMock:
    inv = MagicMock()
    inv.id = uuid.UUID(invite_id)
    inv.task_id = uuid.UUID(task_id)
    inv.worker_id = uuid.UUID(worker_id)
    inv.requester_id = uuid.UUID(requester_id)
    inv.status = status
    inv.message = message
    inv.created_at = created_at or _now()
    inv.responded_at = None
    return inv


def _make_watchlist_item(
    task_id: str = TASK_ID,
    worker_id: str = WORKER_ID,
) -> MagicMock:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.task_id = uuid.UUID(task_id)
    item.worker_id = uuid.UUID(worker_id)
    item.created_at = _now()
    return item


def _make_db() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    db.delete = AsyncMock()
    db.execute = AsyncMock(return_value=_scalars_result([]))
    db.scalar = AsyncMock(return_value=0)
    db.get = AsyncMock(return_value=None)

    async def _refresh(obj):
        pass
    db.refresh = _refresh
    return db


def _scalar_result(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value)
    r.scalars = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=[value] if value is not None else []))
    )
    return r


def _scalars_result(items: list) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalar_one = MagicMock(return_value=items[0] if items else None)
    # scalars() must be both callable with .all() AND directly iterable
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=items)
    scalars_mock.__iter__ = MagicMock(return_value=iter(items))
    r.scalars = MagicMock(return_value=scalars_mock)
    # The result itself must also be iterable (for row-based iteration like {str(row[0]) for row in result})
    r.__iter__ = MagicMock(return_value=iter([]))
    return r


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


# ─── Browse Workers ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browse_workers_unauthenticated():
    """GET /v1/workers/browse — no token → 401 or 403."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/workers/browse")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_browse_workers_empty():
    """GET /v1/workers/browse — no matching workers → empty items, total=0."""
    from main import app
    from core.database import get_db

    db = _make_db()
    # scalar for count query returns 0
    db.scalar.return_value = 0
    # execute returns empty scalars for the worker query
    db.execute.return_value = _scalars_result([])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/workers/browse",
                headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["page"] == 1
        assert body["pages"] >= 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_browse_workers_pagination_fields():
    """GET /v1/workers/browse — response includes pagination metadata."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.scalar.return_value = 0
    db.execute.return_value = _scalars_result([])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/workers/browse?page=2&limit=10",
                headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "total" in body
        assert "page" in body
        assert "pages" in body
        assert body["page"] == 2
    finally:
        app.dependency_overrides.clear()


# ─── Invite Worker ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_worker_unauthenticated():
    """POST /v1/tasks/{id}/invite — no token → 401 or 403."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            f"/v1/tasks/{TASK_ID}/invite",
            json={"worker_id": WORKER_ID},
        )
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_invite_worker_happy_path():
    """POST /v1/tasks/{id}/invite — valid invite → 201 with invite details."""
    from main import app
    from core.database import get_db

    task = _make_task()
    worker = _make_worker()
    requester = MagicMock()
    requester.name = "Requester Jane"
    requester.id = uuid.UUID(REQUESTER_ID)

    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(task)       # task lookup
        if call_num[0] == 2:
            return _scalar_result(worker)     # worker lookup
        if call_num[0] == 3:
            return _scalar_result(requester)  # requester name lookup
        return _scalar_result(None)

    db.execute.side_effect = _side_effect
    # scalar for duplicate check returns 0 (no existing invite)
    db.scalar.return_value = 0

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/invite",
                json={"worker_id": WORKER_ID, "message": "Please join!"},
                headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 201
        body = r.json()
        assert body["task_id"] == TASK_ID
        assert body["worker_id"] == WORKER_ID
        assert body["status"] == "pending"
        assert "id" in body
        # Verify db.add was called (invite + notification)
        assert db.add.call_count >= 1
        db.commit.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_invite_worker_task_not_found():
    """POST /v1/tasks/{id}/invite — unknown task → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(None)  # task not found

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{uuid.uuid4()}/invite",
                json={"worker_id": WORKER_ID},
                headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 404
        assert "Task not found" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_invite_worker_ai_task_rejected():
    """POST /v1/tasks/{id}/invite — AI execution_mode → 400."""
    from main import app
    from core.database import get_db

    task = _make_task(execution_mode="ai")
    db = _make_db()
    db.execute.return_value = _scalar_result(task)

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/invite",
                json={"worker_id": WORKER_ID},
                headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 400
        assert "human" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_invite_worker_closed_task_rejected():
    """POST /v1/tasks/{id}/invite — completed task → 400."""
    from main import app
    from core.database import get_db

    task = _make_task(status="completed")
    db = _make_db()
    db.execute.return_value = _scalar_result(task)

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/invite",
                json={"worker_id": WORKER_ID},
                headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 400
        assert "open or pending" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_invite_worker_not_found():
    """POST /v1/tasks/{id}/invite — worker does not exist → 404."""
    from main import app
    from core.database import get_db

    task = _make_task()
    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(task)    # task found
        return _scalar_result(None)        # worker not found

    db.execute.side_effect = _side_effect

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/invite",
                json={"worker_id": str(uuid.uuid4())},
                headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 404
        assert "Worker not found" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_invite_worker_duplicate_409():
    """POST /v1/tasks/{id}/invite — already invited → 409."""
    from main import app
    from core.database import get_db

    task = _make_task()
    worker = _make_worker()
    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(task)
        if call_num[0] == 2:
            return _scalar_result(worker)
        return _scalar_result(None)

    db.execute.side_effect = _side_effect
    # scalar for duplicate check returns 1 (existing invite)
    db.scalar.return_value = 1

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/invite",
                json={"worker_id": WORKER_ID},
                headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 409
        assert "already invited" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


# ─── Bulk Invite ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_invite_happy_path():
    """POST /v1/tasks/{id}/bulk-invite — invites valid workers, skips invalid."""
    from main import app
    from core.database import get_db

    task = _make_task()
    worker1 = _make_worker(WORKER_ID)
    worker2 = _make_worker(WORKER_ID_2)
    requester = MagicMock()
    requester.name = "Requester Jane"
    requester.id = uuid.UUID(REQUESTER_ID)

    invalid_worker_id = str(uuid.uuid4())

    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(task)             # task lookup
        if call_num[0] == 2:
            return _scalar_result(requester)        # requester name lookup
        if call_num[0] == 3:
            # valid workers query — scalars() must be iterable for dict comprehension
            return _scalars_result([worker1, worker2])
        if call_num[0] == 4:
            # existing invites query — iterates as rows: {str(row[0]) for row in ei_result}
            r = MagicMock()
            r.__iter__ = MagicMock(return_value=iter([]))
            return r
        # Subsequent execute calls (worker name lookups for notifications, etc.)
        return _scalar_result(None)

    db.execute.side_effect = _side_effect
    db.flush = AsyncMock()  # called per-invite inside the loop

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/bulk-invite",
                json={
                    "worker_ids": [WORKER_ID, WORKER_ID_2, invalid_worker_id],
                    "message": "Join my project!",
                },
                headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 201
        body = r.json()
        assert "invited" in body
        assert "skipped" in body
        assert "invite_ids" in body
        # 2 valid workers invited, 1 invalid skipped
        assert body["invited"] == 2
        assert body["skipped"] == 1
    finally:
        app.dependency_overrides.clear()


# ─── List Task Invites (Requester) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_task_invites_happy_path():
    """GET /v1/tasks/{id}/invites — requester sees invite list."""
    from main import app
    from core.database import get_db

    task = _make_task()
    invite = _make_invite(message="Hello!")
    worker = _make_worker()

    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(task)          # task ownership check
        if call_num[0] == 2:
            return _scalars_result([invite])     # invites for this task
        if call_num[0] == 3:
            return _scalars_result([worker])     # bulk worker lookup
        return _scalars_result([])

    db.execute.side_effect = _side_effect

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                f"/v1/tasks/{TASK_ID}/invites",
                headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["worker_id"] == WORKER_ID
        assert body[0]["status"] == "pending"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_task_invites_task_not_found():
    """GET /v1/tasks/{id}/invites — task not found → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(None)

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                f"/v1/tasks/{uuid.uuid4()}/invites",
                headers={"Authorization": f"Bearer {_token(REQUESTER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ─── List Worker Invites ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_my_invites_unauthenticated():
    """GET /v1/worker/invites — no token → 401 or 403."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/worker/invites")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_list_my_invites_empty():
    """GET /v1/worker/invites — no invites → empty list."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalars_result([])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/worker/invites",
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert len(body) == 0
    finally:
        app.dependency_overrides.clear()


# ─── Respond to Invite ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_respond_invite_accept_happy_path():
    """POST /v1/worker/invites/{id}/respond — accept → 200 with assignment."""
    from main import app
    from core.database import get_db

    invite = _make_invite()
    task = _make_task()
    worker = _make_worker()

    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(invite)   # invite lookup (with_for_update)
        if call_num[0] == 2:
            return _scalar_result(task)     # task lookup (with_for_update)
        if call_num[0] == 3:
            return _scalar_result(worker)   # worker name for notification
        return _scalar_result(None)

    db.execute.side_effect = _side_effect
    # scalar for active_assignments count returns 0 (slot available)
    db.scalar.return_value = 0

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/worker/invites/{INVITE_ID}/respond",
                json={"action": "accept"},
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "accepted"
        assert "assignment_id" in body
        # Verify an assignment was added to the session
        db.add.assert_called()
        db.commit.assert_awaited()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_respond_invite_decline_happy_path():
    """POST /v1/worker/invites/{id}/respond — decline → 200."""
    from main import app
    from core.database import get_db

    invite = _make_invite()
    worker = _make_worker()
    task = _make_task()

    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(invite)   # invite lookup
        if call_num[0] == 2:
            return _scalar_result(worker)   # worker name for notification
        if call_num[0] == 3:
            return _scalar_result(task)     # task label for notification
        return _scalar_result(None)

    db.execute.side_effect = _side_effect

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/worker/invites/{INVITE_ID}/respond",
                json={"action": "decline"},
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "declined"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_respond_invite_expired():
    """POST /v1/worker/invites/{id}/respond — >48h old invite → 400 expired."""
    from main import app
    from core.database import get_db

    # Create invite that is 49 hours old
    old_time = _now() - timedelta(hours=49)
    invite = _make_invite(created_at=old_time)

    db = _make_db()
    db.execute.return_value = _scalar_result(invite)

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/worker/invites/{INVITE_ID}/respond",
                json={"action": "accept"},
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 400
        assert "expired" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_respond_invite_already_responded():
    """POST /v1/worker/invites/{id}/respond — already accepted → 400."""
    from main import app
    from core.database import get_db

    invite = _make_invite(status="accepted")

    db = _make_db()
    db.execute.return_value = _scalar_result(invite)

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/worker/invites/{INVITE_ID}/respond",
                json={"action": "accept"},
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 400
        assert "already" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_respond_invite_invalid_action():
    """POST /v1/worker/invites/{id}/respond — invalid action → 400."""
    from main import app
    from core.database import get_db

    invite = _make_invite()

    db = _make_db()
    db.execute.return_value = _scalar_result(invite)

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/worker/invites/{INVITE_ID}/respond",
                json={"action": "maybe"},
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 400
        assert "accept" in r.json()["detail"].lower() or "decline" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


# ─── Watchlist ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watchlist_add_happy_path():
    """POST /v1/worker/watchlist/{id} — add task → 201."""
    from main import app
    from core.database import get_db

    task = _make_task()
    db = _make_db()
    call_num = [0]

    def _exec_side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(task)    # task exists check
        return _scalar_result(None)

    db.execute.side_effect = _exec_side_effect
    # scalar calls: 1) existing watchlist check = 0, 2) total count = 0
    db.scalar.side_effect = [0, 0]

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/worker/watchlist/{TASK_ID}",
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "watching"
        assert body["task_id"] == TASK_ID
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_watchlist_add_duplicate():
    """POST /v1/worker/watchlist/{id} — already watching → already_watching."""
    from main import app
    from core.database import get_db

    task = _make_task()
    db = _make_db()

    db.execute.return_value = _scalar_result(task)  # task exists
    # scalar for existing watchlist check returns 1 (already watching)
    db.scalar.return_value = 1

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/worker/watchlist/{TASK_ID}",
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        # Duplicate returns 200 (not 201) with status "already_watching"
        assert r.status_code in (200, 201)
        body = r.json()
        assert body["status"] == "already_watching"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_watchlist_add_task_not_found():
    """POST /v1/worker/watchlist/{id} — task does not exist → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(None)  # task not found

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/worker/watchlist/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 404
        assert "Task not found" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_watchlist_add_limit_reached():
    """POST /v1/worker/watchlist/{id} — 100 items → 400."""
    from main import app
    from core.database import get_db

    task = _make_task()
    db = _make_db()

    db.execute.return_value = _scalar_result(task)  # task exists

    scalar_calls = [0]
    original_scalar_return = [0, 100]  # first: not existing, second: count = 100

    async def _scalar_side(stmt):
        idx = scalar_calls[0]
        scalar_calls[0] += 1
        if idx < len(original_scalar_return):
            return original_scalar_return[idx]
        return 0

    db.scalar.side_effect = _scalar_side

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/worker/watchlist/{TASK_ID}",
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 400
        assert "100" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_watchlist_get_returns_items():
    """GET /v1/worker/watchlist — returns watchlist items with task details."""
    from main import app
    from core.database import get_db

    task = _make_task()
    wl_item = _make_watchlist_item()

    db = _make_db()
    call_num = [0]

    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalars_result([wl_item])  # watchlist items
        if call_num[0] == 2:
            return _scalars_result([task])     # bulk task lookup
        return _scalars_result([])

    db.execute.side_effect = _side_effect

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/worker/watchlist",
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["task_id"] == TASK_ID
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_watchlist_check_watching():
    """GET /v1/worker/watchlist/check/{id} — task is watched → watching=true."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.scalar.return_value = 1  # exists

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                f"/v1/worker/watchlist/check/{TASK_ID}",
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["watching"] is True
        assert body["task_id"] == TASK_ID
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_watchlist_check_not_watching():
    """GET /v1/worker/watchlist/check/{id} — task not watched → watching=false."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.scalar.return_value = 0  # does not exist

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                f"/v1/worker/watchlist/check/{TASK_ID}",
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["watching"] is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_watchlist_remove_happy_path():
    """DELETE /v1/worker/watchlist/{id} — item exists → 200 removed."""
    from main import app
    from core.database import get_db

    wl_item = _make_watchlist_item()
    db = _make_db()
    db.execute.return_value = _scalar_result(wl_item)

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(
                f"/v1/worker/watchlist/{TASK_ID}",
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "removed"
        db.delete.assert_called_once()
        db.commit.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_watchlist_remove_not_found():
    """DELETE /v1/worker/watchlist/{id} — not in watchlist → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(None)

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(
                f"/v1/worker/watchlist/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {_token(WORKER_ID)}"},
            )
        assert r.status_code == 404
        assert "Not in watchlist" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()
