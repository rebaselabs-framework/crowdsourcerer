"""Comprehensive tests for the task comments API.

Covers:
  - Auth: 401 on all endpoints without a token
  - List comments: happy path as requester, as worker, worker cannot see internal
  - Post comment: requester happy path, worker happy path, unassigned worker 403
  - Internal notes: requester can post, worker cannot
  - Edit: within 15-min window, after window (403), others' comment (403)
  - Delete: own comment (204), others' comment (403)
  - Admin overrides: edit/delete any comment, post internal notes
  - Comment cap: 200 limit returns 429
  - Parent comment validation: missing parent 404
  - Task not found: 404
  - Schema validation: empty body, body too long (>500 chars)
  - Notification side-effects: post triggers create_notification
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Stable IDs ────────────────────────────────────────────────────────────────

REQUESTER_ID = str(uuid.uuid4())
WORKER_ID = str(uuid.uuid4())
OUTSIDER_ID = str(uuid.uuid4())
ADMIN_ID = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())
COMMENT_ID = str(uuid.uuid4())
PARENT_COMMENT_ID = str(uuid.uuid4())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _headers(user_id: str) -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


def _mock_user(
    user_id: str,
    name: str = "Test User",
    email: str = "test@example.com",
    is_admin: bool = False,
) -> MagicMock:
    u = MagicMock()
    u.id = uuid.UUID(user_id)
    u.name = name
    u.email = email
    u.is_admin = is_admin
    u.is_active = True
    u.is_banned = False
    u.role = "both"
    u.plan = "free"
    u.token_version = 0
    u.reputation_score = 100.0
    u.created_at = _now()
    return u


def _mock_task(task_id: str = TASK_ID, requester_id: str = REQUESTER_ID) -> MagicMock:
    t = MagicMock()
    t.id = uuid.UUID(task_id)
    t.user_id = uuid.UUID(requester_id)
    t.type = "label_text"
    t.status = "assigned"
    return t


def _mock_comment(
    comment_id: str = COMMENT_ID,
    task_id: str = TASK_ID,
    user_id: str = REQUESTER_ID,
    body: str = "Hello world",
    is_internal: bool = False,
    parent_id: str | None = None,
    created_at: datetime | None = None,
    edited_at: datetime | None = None,
) -> MagicMock:
    c = MagicMock()
    c.id = uuid.UUID(comment_id)
    c.task_id = uuid.UUID(task_id)
    c.user_id = uuid.UUID(user_id) if isinstance(user_id, str) else user_id
    c.parent_id = uuid.UUID(parent_id) if parent_id else None
    c.body = body
    c.is_internal = is_internal
    c.edited_at = edited_at
    c.created_at = created_at or _now()
    return c


def _make_mock_db() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    db.delete = AsyncMock()
    db.execute = AsyncMock()
    db.scalar = AsyncMock(return_value=0)
    db.get = AsyncMock(return_value=None)

    async def _refresh(obj):
        pass
    db.refresh = _refresh
    return db


def _scalars_result(items: list) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalars = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=items))
    )
    return r


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _suppress_notifications():
    """Suppress notification side-effects by default."""
    noop = AsyncMock()
    with patch("routers.comments.create_notification", noop):
        yield noop


@pytest.fixture(autouse=True)
def _disable_rate_limit():
    """Disable slowapi rate limiting so tests are not flaky."""
    with patch("routers.comments.limiter.limit", lambda *a, **kw: lambda fn: fn):
        yield


@pytest.fixture
def app():
    from main import app as _app
    return _app


# ═════════════════════════════════════════════════════════════════════════════
# 1. Authentication — 401 for all endpoints
# ═════════════════════════════════════════════════════════════════════════════

class TestAuth401:
    """All comment endpoints require authentication."""

    @pytest.mark.asyncio
    async def test_list_comments_no_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/tasks/{TASK_ID}/comments")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_post_comment_no_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/comments",
                json={"body": "Test"},
            )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_edit_comment_no_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                json={"body": "Edited"},
            )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_delete_comment_no_auth(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}")
        assert r.status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
# 2. Task Not Found — 404
# ═════════════════════════════════════════════════════════════════════════════

class TestTaskNotFound:

    @pytest.mark.asyncio
    async def test_list_comments_task_not_found(self, app):
        db = _make_mock_db()
        db.get = AsyncMock(return_value=None)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/tasks/{uuid.uuid4()}/comments",
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 404
            assert "task" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_post_comment_task_not_found(self, app):
        db = _make_mock_db()
        db.get = AsyncMock(return_value=None)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{uuid.uuid4()}/comments",
                    json={"body": "Hello"},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# 3. List Comments
# ═════════════════════════════════════════════════════════════════════════════

class TestListComments:

    @pytest.mark.asyncio
    async def test_list_comments_as_requester(self, app):
        """Requester can list all comments including internal ones."""
        task = _mock_task()
        requester = _mock_user(REQUESTER_ID, "Requester")
        comment = _mock_comment(body="Public comment")
        internal = _mock_comment(
            comment_id=str(uuid.uuid4()),
            body="Internal note",
            is_internal=True,
        )

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)

        # scalar calls: first for total count
        db.scalar = AsyncMock(return_value=2)

        exec_call = [0]
        async def _execute(stmt, *a, **kw):
            nonlocal exec_call
            exec_call[0] += 1
            if exec_call[0] == 1:
                # Comments query
                return _scalars_result([comment, internal])
            if exec_call[0] == 2:
                # Authors query
                return _scalars_result([requester])
            return _scalars_result([])
        db.execute = AsyncMock(side_effect=_execute)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/tasks/{TASK_ID}/comments",
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 200
            body = r.json()
            assert body["total"] == 2
            assert "comments" in body
            assert len(body["comments"]) == 2
            # Verify output shape
            first = body["comments"][0]
            assert "id" in first
            assert "task_id" in first
            assert "user_id" in first
            assert "author_name" in first
            assert "parent_id" in first
            assert "body" in first
            assert "is_internal" in first
            assert "edited_at" in first
            assert "created_at" in first
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_list_comments_as_worker_hides_internal(self, app):
        """Assigned worker can list comments but internal notes are filtered out."""
        task = _mock_task()
        worker = _mock_user(WORKER_ID, "Worker")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return worker
            return None
        db.get = AsyncMock(side_effect=_get)

        # Worker is not requester, so access check calls db.scalar for assignment count
        # Then total count query also calls db.scalar
        scalar_call = [0]
        async def _scalar(stmt):
            scalar_call[0] += 1
            if scalar_call[0] == 1:
                return 1  # assignment exists
            return 1  # total count of visible comments
        db.scalar = AsyncMock(side_effect=_scalar)

        public_comment = _mock_comment(body="Public comment", is_internal=False)
        exec_call = [0]
        async def _execute(stmt, *a, **kw):
            nonlocal exec_call
            exec_call[0] += 1
            if exec_call[0] == 1:
                return _scalars_result([public_comment])
            if exec_call[0] == 2:
                return _scalars_result([_mock_user(REQUESTER_ID, "Requester")])
            return _scalars_result([])
        db.execute = AsyncMock(side_effect=_execute)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/tasks/{TASK_ID}/comments",
                    headers=_headers(WORKER_ID),
                )
            assert r.status_code == 200
            body = r.json()
            # Worker only sees non-internal comments
            for cmt in body["comments"]:
                assert cmt["is_internal"] is False
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_list_comments_unassigned_worker_403(self, app):
        """User who is neither requester, admin, nor assigned worker gets 403."""
        task = _mock_task()
        outsider = _mock_user(OUTSIDER_ID, "Outsider")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return outsider
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=0)  # no assignment

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/tasks/{TASK_ID}/comments",
                    headers=_headers(OUTSIDER_ID),
                )
            assert r.status_code == 403
        finally:
            app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# 4. Post Comment
# ═════════════════════════════════════════════════════════════════════════════

class TestPostComment:

    @pytest.mark.asyncio
    async def test_post_comment_as_requester(self, app):
        """Requester can post a comment and gets back the correct shape."""
        task = _mock_task()
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)

        # scalar: comment count
        db.scalar = AsyncMock(return_value=5)

        # After flush + commit + refresh, the comment object gets its fields set
        async def _refresh(obj):
            if not hasattr(obj, "_refreshed"):
                obj.id = uuid.uuid4()
                obj.created_at = _now()
                obj.edited_at = None
                obj._refreshed = True
        db.refresh = _refresh

        # execute: workers query for notification (requester posting)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Great progress!"},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 201
            body = r.json()
            assert body["body"] == "Great progress!"
            assert body["is_internal"] is False
            assert body["author_name"] == "Requester"
            assert body["parent_id"] is None
            assert body["task_id"] == TASK_ID
            assert body["edited_at"] is None
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_post_comment_as_assigned_worker(self, app):
        """Assigned worker can post a comment."""
        task = _mock_task()
        worker = _mock_user(WORKER_ID, "Worker")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return worker
            return None
        db.get = AsyncMock(side_effect=_get)

        # scalar calls: assignment count (1), comment count (5)
        scalar_call = [0]
        async def _scalar(stmt):
            scalar_call[0] += 1
            if scalar_call[0] == 1:
                return 1  # assignment exists
            return 5  # comment count
        db.scalar = AsyncMock(side_effect=_scalar)

        async def _refresh(obj):
            if not hasattr(obj, "_refreshed"):
                obj.id = uuid.uuid4()
                obj.created_at = _now()
                obj.edited_at = None
                obj._refreshed = True
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Working on it!"},
                    headers=_headers(WORKER_ID),
                )
            assert r.status_code == 201
            body = r.json()
            assert body["body"] == "Working on it!"
            assert body["author_name"] == "Worker"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_post_comment_unassigned_worker_403(self, app):
        """User not assigned to the task and not the requester gets 403."""
        task = _mock_task()
        outsider = _mock_user(OUTSIDER_ID, "Outsider")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return outsider
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=0)  # no assignment

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Trying to comment"},
                    headers=_headers(OUTSIDER_ID),
                )
            assert r.status_code == 403
            assert "not assigned" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# 5. Internal Notes
# ═════════════════════════════════════════════════════════════════════════════

class TestInternalNotes:

    @pytest.mark.asyncio
    async def test_requester_can_post_internal_note(self, app):
        """Requester can post an internal note (is_internal=True)."""
        task = _mock_task()
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=5)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        async def _refresh(obj):
            if not hasattr(obj, "_refreshed"):
                obj.id = uuid.uuid4()
                obj.created_at = _now()
                obj.edited_at = None
                obj._refreshed = True
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Private note", "is_internal": True},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 201
            assert r.json()["is_internal"] is True
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_worker_cannot_post_internal_note(self, app):
        """Assigned worker cannot post internal notes (403)."""
        task = _mock_task()
        worker = _mock_user(WORKER_ID, "Worker")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return worker
            return None
        db.get = AsyncMock(side_effect=_get)

        # Worker is assigned (access check passes) but cannot post internal
        scalar_call = [0]
        async def _scalar(stmt):
            scalar_call[0] += 1
            return 1  # assignment exists
        db.scalar = AsyncMock(side_effect=_scalar)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Sneaky internal", "is_internal": True},
                    headers=_headers(WORKER_ID),
                )
            assert r.status_code == 403
            assert "internal" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_admin_can_post_internal_note(self, app):
        """Admin can post internal notes even though they are not the requester."""
        task = _mock_task()
        admin = _mock_user(ADMIN_ID, "Admin", "admin@example.com", is_admin=True)

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return admin
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=5)  # comment count
        db.execute = AsyncMock(return_value=_scalars_result([]))

        async def _refresh(obj):
            if not hasattr(obj, "_refreshed"):
                obj.id = uuid.uuid4()
                obj.created_at = _now()
                obj.edited_at = None
                obj._refreshed = True
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Admin internal note", "is_internal": True},
                    headers=_headers(ADMIN_ID),
                )
            assert r.status_code == 201
            assert r.json()["is_internal"] is True
        finally:
            app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# 6. Edit Comment
# ═════════════════════════════════════════════════════════════════════════════

class TestEditComment:

    @pytest.mark.asyncio
    async def test_edit_own_comment_within_window(self, app):
        """Author can edit their comment within the 15-minute window."""
        recent_time = _now() - timedelta(minutes=5)
        comment = _mock_comment(
            user_id=REQUESTER_ID,
            body="Original",
            created_at=recent_time,
        )
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskCommentDB, UserDB
            if model is TaskCommentDB:
                return comment
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)

        async def _refresh(obj):
            pass
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                    json={"body": "Edited body"},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 200
            assert r.json()["body"] == "Edited body"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_edit_comment_after_15_min_window_403(self, app):
        """Author cannot edit their comment after 15 minutes."""
        old_time = _now() - timedelta(minutes=20)
        comment = _mock_comment(
            user_id=REQUESTER_ID,
            body="Old comment",
            created_at=old_time,
        )
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskCommentDB, UserDB
            if model is TaskCommentDB:
                return comment
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                    json={"body": "Too late edit"},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 403
            assert "15 minutes" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_edit_others_comment_403(self, app):
        """A user cannot edit another user's comment."""
        comment = _mock_comment(
            user_id=REQUESTER_ID,
            body="Not yours",
            created_at=_now() - timedelta(minutes=2),
        )
        worker = _mock_user(WORKER_ID, "Worker")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskCommentDB, UserDB
            if model is TaskCommentDB:
                return comment
            if model is UserDB:
                return worker
            return None
        db.get = AsyncMock(side_effect=_get)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                    json={"body": "Hijack attempt"},
                    headers=_headers(WORKER_ID),
                )
            assert r.status_code == 403
            assert "own" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_edit_comment_not_found_404(self, app):
        """Editing a nonexistent comment returns 404."""
        db = _make_mock_db()
        user = _mock_user(REQUESTER_ID, "Requester")

        async def _get(model, pk):
            from models.db import UserDB
            if model is UserDB:
                return user
            return None  # comment not found
        db.get = AsyncMock(side_effect=_get)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/tasks/{TASK_ID}/comments/{uuid.uuid4()}",
                    json={"body": "Edit what?"},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# 7. Delete Comment
# ═════════════════════════════════════════════════════════════════════════════

class TestDeleteComment:

    @pytest.mark.asyncio
    async def test_delete_own_comment(self, app):
        """Author can delete their own comment and gets 204."""
        comment = _mock_comment(user_id=REQUESTER_ID)
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskCommentDB, UserDB
            if model is TaskCommentDB:
                return comment
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 204
            db.delete.assert_called_once_with(comment)
            db.commit.assert_awaited()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_delete_others_comment_403(self, app):
        """A non-admin cannot delete someone else's comment."""
        comment = _mock_comment(user_id=REQUESTER_ID)
        worker = _mock_user(WORKER_ID, "Worker")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskCommentDB, UserDB
            if model is TaskCommentDB:
                return comment
            if model is UserDB:
                return worker
            return None
        db.get = AsyncMock(side_effect=_get)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                    headers=_headers(WORKER_ID),
                )
            assert r.status_code == 403
            assert "own" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_delete_comment_not_found_404(self, app):
        """Deleting a nonexistent comment returns 404."""
        db = _make_mock_db()
        user = _mock_user(REQUESTER_ID, "Requester")

        async def _get(model, pk):
            from models.db import UserDB
            if model is UserDB:
                return user
            return None
        db.get = AsyncMock(side_effect=_get)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/tasks/{TASK_ID}/comments/{uuid.uuid4()}",
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# 8. Admin Overrides
# ═════════════════════════════════════════════════════════════════════════════

class TestAdminOverrides:

    @pytest.mark.asyncio
    async def test_admin_can_edit_any_comment(self, app):
        """Admin can edit any user's comment regardless of authorship or time."""
        old_time = _now() - timedelta(hours=2)  # well past 15 min window
        comment = _mock_comment(
            user_id=WORKER_ID,
            body="Worker wrote this",
            created_at=old_time,
        )
        admin = _mock_user(ADMIN_ID, "Admin", "admin@example.com", is_admin=True)

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskCommentDB, UserDB
            if model is TaskCommentDB:
                return comment
            if model is UserDB:
                return admin
            return None
        db.get = AsyncMock(side_effect=_get)

        async def _refresh(obj):
            pass
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                    json={"body": "Admin edited this"},
                    headers=_headers(ADMIN_ID),
                )
            assert r.status_code == 200
            assert r.json()["body"] == "Admin edited this"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_admin_can_delete_any_comment(self, app):
        """Admin can delete any user's comment."""
        comment = _mock_comment(user_id=WORKER_ID)
        admin = _mock_user(ADMIN_ID, "Admin", "admin@example.com", is_admin=True)

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskCommentDB, UserDB
            if model is TaskCommentDB:
                return comment
            if model is UserDB:
                return admin
            return None
        db.get = AsyncMock(side_effect=_get)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                    headers=_headers(ADMIN_ID),
                )
            assert r.status_code == 204
            db.delete.assert_called_once_with(comment)
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_admin_can_list_comments_on_any_task(self, app):
        """Admin can list comments even if not requester or assigned."""
        task = _mock_task()
        admin = _mock_user(ADMIN_ID, "Admin", "admin@example.com", is_admin=True)
        comment = _mock_comment(body="Some comment", user_id=REQUESTER_ID)

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return admin
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=1)

        requester_mock = _mock_user(REQUESTER_ID, "Requester")
        exec_call = [0]
        async def _execute(stmt, *a, **kw):
            nonlocal exec_call
            exec_call[0] += 1
            if exec_call[0] == 1:
                return _scalars_result([comment])
            if exec_call[0] == 2:
                return _scalars_result([requester_mock])
            return _scalars_result([])
        db.execute = AsyncMock(side_effect=_execute)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    f"/v1/tasks/{TASK_ID}/comments",
                    headers=_headers(ADMIN_ID),
                )
            assert r.status_code == 200
            assert r.json()["total"] == 1
        finally:
            app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# 9. Comment Cap (200 per task)
# ═════════════════════════════════════════════════════════════════════════════

class TestCommentCap:

    @pytest.mark.asyncio
    async def test_comment_cap_reached_429(self, app):
        """Posting when there are already 200 comments returns 429."""
        task = _mock_task()
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)
        # Comment count is at the max
        db.scalar = AsyncMock(return_value=200)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "One more?"},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 429
            assert "limit" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_post_at_199_succeeds(self, app):
        """Posting when there are 199 comments still succeeds."""
        task = _mock_task()
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=199)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        async def _refresh(obj):
            if not hasattr(obj, "_refreshed"):
                obj.id = uuid.uuid4()
                obj.created_at = _now()
                obj.edited_at = None
                obj._refreshed = True
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Squeezed in!"},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 201
        finally:
            app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# 10. Parent Comment Validation
# ═════════════════════════════════════════════════════════════════════════════

class TestParentComment:

    @pytest.mark.asyncio
    async def test_parent_comment_not_found_404(self, app):
        """Replying to a nonexistent parent comment returns 404."""
        task = _mock_task()
        requester = _mock_user(REQUESTER_ID, "Requester")
        fake_parent_id = str(uuid.uuid4())

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB, TaskCommentDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return requester
            if model is TaskCommentDB:
                return None  # parent not found
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=5)  # comment count

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Reply", "parent_id": fake_parent_id},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 404
            assert "parent" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_parent_comment_wrong_task_404(self, app):
        """Replying to a parent that belongs to a different task returns 404."""
        task = _mock_task()
        requester = _mock_user(REQUESTER_ID, "Requester")
        # Parent exists but belongs to a different task
        other_task_id = str(uuid.uuid4())
        parent = _mock_comment(
            comment_id=PARENT_COMMENT_ID,
            task_id=other_task_id,
            user_id=REQUESTER_ID,
        )

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB, TaskCommentDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return requester
            if model is TaskCommentDB:
                return parent
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=5)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Cross-task reply", "parent_id": PARENT_COMMENT_ID},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 404
            assert "parent" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_valid_parent_comment_succeeds(self, app):
        """Reply to a valid parent on the same task succeeds."""
        task = _mock_task()
        requester = _mock_user(REQUESTER_ID, "Requester")
        parent = _mock_comment(
            comment_id=PARENT_COMMENT_ID,
            task_id=TASK_ID,
            user_id=WORKER_ID,
        )

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB, TaskCommentDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return requester
            if model is TaskCommentDB:
                return parent
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=5)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        async def _refresh(obj):
            if not hasattr(obj, "_refreshed"):
                obj.id = uuid.uuid4()
                obj.created_at = _now()
                obj.edited_at = None
                obj.parent_id = uuid.UUID(PARENT_COMMENT_ID)
                obj._refreshed = True
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Good reply", "parent_id": PARENT_COMMENT_ID},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 201
            assert r.json()["parent_id"] == PARENT_COMMENT_ID
        finally:
            app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# 11. Schema Validation
# ═════════════════════════════════════════════════════════════════════════════

class TestSchemaValidation:

    @pytest.mark.asyncio
    async def test_empty_body_rejected(self, app):
        """Posting a comment with empty body fails validation (422)."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/comments",
                json={"body": ""},
                headers=_headers(REQUESTER_ID),
            )
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_body_too_long_rejected(self, app):
        """Posting a comment with body > 500 chars fails validation (422)."""
        long_body = "x" * 501
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/comments",
                json={"body": long_body},
                headers=_headers(REQUESTER_ID),
            )
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_body_at_max_length_accepted(self, app):
        """A body exactly 500 chars long passes validation."""
        task = _mock_task()
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=0)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        async def _refresh(obj):
            if not hasattr(obj, "_refreshed"):
                obj.id = uuid.uuid4()
                obj.created_at = _now()
                obj.edited_at = None
                obj._refreshed = True
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "a" * 500},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 201
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_missing_body_field_rejected(self, app):
        """Posting without the body field fails validation (422)."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/comments",
                json={},
                headers=_headers(REQUESTER_ID),
            )
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_edit_empty_body_rejected(self, app):
        """Editing with empty body fails validation (422)."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                json={"body": ""},
                headers=_headers(REQUESTER_ID),
            )
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_edit_body_too_long_rejected(self, app):
        """Editing with body > 500 chars fails validation (422)."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                json={"body": "z" * 501},
                headers=_headers(REQUESTER_ID),
            )
        assert r.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
# 12. Notification Side-Effects
# ═════════════════════════════════════════════════════════════════════════════

class TestNotifications:

    @pytest.mark.asyncio
    async def test_worker_post_notifies_requester(self, _suppress_notifications, app):
        """When an assigned worker posts, create_notification is called for requester."""
        mock_notify = _suppress_notifications
        task = _mock_task()
        worker = _mock_user(WORKER_ID, "Worker")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return worker
            return None
        db.get = AsyncMock(side_effect=_get)

        scalar_call = [0]
        async def _scalar(stmt):
            scalar_call[0] += 1
            if scalar_call[0] == 1:
                return 1  # assignment exists
            return 5  # comment count
        db.scalar = AsyncMock(side_effect=_scalar)

        async def _refresh(obj):
            if not hasattr(obj, "_refreshed"):
                obj.id = uuid.uuid4()
                obj.created_at = _now()
                obj.edited_at = None
                obj._refreshed = True
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Done with subtask"},
                    headers=_headers(WORKER_ID),
                )
            assert r.status_code == 201
            # Notification should have been called targeting the requester
            mock_notify.assert_awaited()
            # The first positional arg after db is user_id — should be the requester
            call_kwargs = mock_notify.call_args
            assert str(call_kwargs.kwargs.get("user_id", "")) == REQUESTER_ID or \
                (len(call_kwargs.args) >= 2 and str(call_kwargs.args[1]) == REQUESTER_ID)
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_internal_note_does_not_notify_workers(self, _suppress_notifications, app):
        """Internal notes from requester should NOT trigger worker notifications."""
        mock_notify = _suppress_notifications
        task = _mock_task()
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=5)
        # The workers query won't execute because is_internal skips notifications
        db.execute = AsyncMock(return_value=_scalars_result([]))

        async def _refresh(obj):
            if not hasattr(obj, "_refreshed"):
                obj.id = uuid.uuid4()
                obj.created_at = _now()
                obj.edited_at = None
                obj._refreshed = True
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "Internal only", "is_internal": True},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 201
            # create_notification should NOT have been called
            mock_notify.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# 13. Edge Cases
# ═════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:

    @pytest.mark.asyncio
    async def test_edit_comment_wrong_task_id_404(self, app):
        """Editing a comment with a task_id mismatch returns 404."""
        other_task_id = str(uuid.uuid4())
        comment = _mock_comment(
            task_id=other_task_id,
            user_id=REQUESTER_ID,
            created_at=_now() - timedelta(minutes=2),
        )
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskCommentDB, UserDB
            if model is TaskCommentDB:
                return comment  # exists but task_id != route task_id
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                    json={"body": "Wrong task"},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_delete_comment_wrong_task_id_404(self, app):
        """Deleting a comment with a task_id mismatch returns 404."""
        other_task_id = str(uuid.uuid4())
        comment = _mock_comment(task_id=other_task_id, user_id=REQUESTER_ID)
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskCommentDB, UserDB
            if model is TaskCommentDB:
                return comment
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.delete(
                    f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_body_is_stripped_on_post(self, app):
        """Leading/trailing whitespace is stripped from the body."""
        task = _mock_task()
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskDB, UserDB
            if model is TaskDB:
                return task
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)
        db.scalar = AsyncMock(return_value=0)
        db.execute = AsyncMock(return_value=_scalars_result([]))

        async def _refresh(obj):
            if not hasattr(obj, "_refreshed"):
                obj.id = uuid.uuid4()
                obj.created_at = _now()
                obj.edited_at = None
                obj._refreshed = True
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/comments",
                    json={"body": "  padded text  "},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 201
            assert r.json()["body"] == "padded text"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_edit_sets_edited_at(self, app):
        """Editing a comment sets the edited_at timestamp."""
        comment = _mock_comment(
            user_id=REQUESTER_ID,
            body="Original",
            created_at=_now() - timedelta(minutes=3),
        )
        requester = _mock_user(REQUESTER_ID, "Requester")

        db = _make_mock_db()

        async def _get(model, pk):
            from models.db import TaskCommentDB, UserDB
            if model is TaskCommentDB:
                return comment
            if model is UserDB:
                return requester
            return None
        db.get = AsyncMock(side_effect=_get)

        async def _refresh(obj):
            pass
        db.refresh = _refresh

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(
                    f"/v1/tasks/{TASK_ID}/comments/{COMMENT_ID}",
                    json={"body": "Updated"},
                    headers=_headers(REQUESTER_ID),
                )
            assert r.status_code == 200
            # The router sets comment.edited_at; verify it was set
            assert comment.edited_at is not None
            assert r.json()["edited_at"] is not None
        finally:
            app.dependency_overrides.clear()
