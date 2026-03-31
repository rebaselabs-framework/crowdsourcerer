"""Security tests: task message authorization (IDOR protection).

POST /v1/tasks/{task_id}/messages
GET  /v1/tasks/{task_id}/messages

Before the fix, any authenticated user could send messages on any task to any
other user, even if neither party was involved with the task. This enabled:
  - Spam/harassment via unsolicited DMs about someone else's task
  - Information leakage about task existence

After the fix:
  - Both sender AND recipient must be task participants (requester or assigned worker)
  - GET /messages also checks participant status before revealing any messages
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Shared IDs ─────────────────────────────────────────────────────────────

REQUESTER_ID = str(uuid.uuid4())
WORKER_ID    = str(uuid.uuid4())
OUTSIDER_ID  = str(uuid.uuid4())
TASK_ID      = str(uuid.uuid4())


# ── Helpers ────────────────────────────────────────────────────────────────

def _real_token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _make_user(uid: str, name: str = "TestUser") -> MagicMock:
    u = MagicMock()
    u.id = uuid.UUID(uid)
    u.name = name
    u.email = f"{name.lower()}@example.com"
    u.role = "both"
    u.is_banned = False
    u.is_admin = False
    u.reputation_score = 100.0
    u.plan = "free"
    u.token_version = 0
    u.created_at = datetime.now(timezone.utc)
    return u


def _make_task() -> MagicMock:
    t = MagicMock()
    t.id = uuid.UUID(TASK_ID)
    t.user_id = uuid.UUID(REQUESTER_ID)
    t.type = "label_text"
    t.status = "assigned"
    return t


def _make_assignment(worker_id: str) -> MagicMock:
    a = MagicMock()
    a.id = uuid.uuid4()
    a.task_id = uuid.UUID(TASK_ID)
    a.worker_id = uuid.UUID(worker_id)
    a.status = "active"
    return a


def _make_mock_db() -> MagicMock:
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
        pass
    db.refresh = _refresh
    return db


def _db_override(mock_db):
    async def _override():
        yield mock_db
    return _override


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return r


# ── Background suppression ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _suppress_background():
    noop = AsyncMock()
    with patch("routers.task_messages.create_notification", noop):
        yield


@pytest.fixture
def app():
    from main import app as _app
    return _app


# ═════════════════════════════════════════════════════════════════════════
# send_message authorization tests
# ═════════════════════════════════════════════════════════════════════════

class TestSendMessageAuth:
    """Verify that POST /v1/tasks/{task_id}/messages enforces access control."""

    @pytest.mark.asyncio
    async def test_outsider_cannot_send_message(self, app):
        """A user not involved with the task gets 403."""
        task = _make_task()
        outsider = _make_user(OUTSIDER_ID, "Outsider")
        requester = _make_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()
        call_count = [0]

        async def _execute(stmt, *a, **kw):
            call_count[0] += 1
            i = call_count[0]
            if i == 1:  # task lookup
                return _scalar_result(task)
            return _scalar_result(None)

        db.execute = _execute
        # _is_task_participant checks: task.user_id != outsider, then db.scalar for assignment count
        db.scalar = AsyncMock(return_value=0)  # no assignment found

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            headers = {"Authorization": f"Bearer {_real_token(OUTSIDER_ID)}"}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/messages",
                    json={"body": "Hello!", "recipient_id": REQUESTER_ID},
                    headers=headers,
                )
            assert r.status_code == 403
            assert "not involved" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_requester_can_send_to_assigned_worker(self, app):
        """The task requester can message an assigned worker → 201."""
        task = _make_task()
        requester = _make_user(REQUESTER_ID, "Requester")
        worker = _make_user(WORKER_ID, "Worker")

        db = _make_mock_db()
        call_count = [0]

        async def _execute(stmt, *a, **kw):
            call_count[0] += 1
            i = call_count[0]
            if i == 1:  # task lookup
                return _scalar_result(task)
            if i == 2:  # recipient (worker) lookup
                return _scalar_result(worker)
            if i == 3:  # sender (requester) lookup for username
                return _scalar_result(requester)
            return _scalar_result(None)

        db.execute = _execute
        # _is_task_participant for sender: requester == task.user_id → True (no scalar needed)
        # _is_task_participant for recipient: worker != task.user_id → check assignment
        db.scalar = AsyncMock(return_value=1)  # assignment exists

        # Make db.refresh set the required fields on the message object
        async def _refresh(obj):
            if not hasattr(obj, '_refreshed'):
                obj.id = uuid.uuid4()
                obj.is_read = False
                obj.created_at = datetime.now(timezone.utc)
                obj._refreshed = True

        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            headers = {"Authorization": f"Bearer {_real_token(REQUESTER_ID)}"}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/messages",
                    json={"body": "How is the task going?", "recipient_id": WORKER_ID},
                    headers=headers,
                )
            assert r.status_code == 201
            body = r.json()
            assert body["body"] == "How is the task going?"
            assert body["sender_username"] == "Requester"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_requester_cannot_send_to_uninvolved_user(self, app):
        """Requester cannot message someone who is not involved with the task."""
        task = _make_task()
        requester = _make_user(REQUESTER_ID, "Requester")
        outsider = _make_user(OUTSIDER_ID, "Outsider")

        db = _make_mock_db()
        call_count = [0]

        async def _execute(stmt, *a, **kw):
            call_count[0] += 1
            i = call_count[0]
            if i == 1:  # task lookup
                return _scalar_result(task)
            if i == 2:  # recipient (outsider) lookup
                return _scalar_result(outsider)
            return _scalar_result(None)

        db.execute = _execute
        # _is_task_participant for sender: requester == task.user_id → True
        # _is_task_participant for recipient: outsider != task.user_id → check assignment → 0
        db.scalar = AsyncMock(return_value=0)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            headers = {"Authorization": f"Bearer {_real_token(REQUESTER_ID)}"}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/messages",
                    json={"body": "Hi!", "recipient_id": OUTSIDER_ID},
                    headers=headers,
                )
            assert r.status_code == 403
            assert "recipient" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_no_auth_returns_401(self, app):
        """No token → 401."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/messages",
                json={"body": "Hello!", "recipient_id": REQUESTER_ID},
            )
        assert r.status_code == 401


# ═════════════════════════════════════════════════════════════════════════
# get_task_messages authorization tests
# ═════════════════════════════════════════════════════════════════════════

class TestGetMessagesAuth:
    """Verify that GET /v1/tasks/{task_id}/messages enforces access control."""

    @pytest.mark.asyncio
    async def test_outsider_cannot_read_messages(self, app):
        """A user not involved with the task gets 403."""
        task = _make_task()

        db = _make_mock_db()
        call_count = [0]

        async def _execute(stmt, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:  # task lookup
                return _scalar_result(task)
            return _scalar_result(None)

        db.execute = _execute
        db.scalar = AsyncMock(return_value=0)  # no assignment

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            headers = {"Authorization": f"Bearer {_real_token(OUTSIDER_ID)}"}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/tasks/{TASK_ID}/messages",
                    headers=headers,
                )
            assert r.status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_task_not_found_returns_404(self, app):
        """Non-existent task → 404."""
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar_result(None))

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            headers = {"Authorization": f"Bearer {_real_token(REQUESTER_ID)}"}
            random_id = str(uuid.uuid4())
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/tasks/{random_id}/messages",
                    headers=headers,
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)
