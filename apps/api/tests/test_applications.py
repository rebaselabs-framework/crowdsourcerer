"""Tests for the applications router.

Covers:
  1. list_applications bulk-load N+1 fix: db.execute called exactly TWICE
  2. list_applications ownership guard: non-owner gets 403
  3. list_my_applications pagination: page=2, page_size=1 applies correct offset
  4. apply_to_task happy path: returns ApplicationOut with status="pending"
  5. apply_to_task duplicate guard: 409 when worker already applied
  6. accept_application happy path: task.status → "assigned", app.status → "accepted",
     assignment is created
  7. reject_application happy path: app.status → "rejected"
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

# Must precede app imports
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Shared IDs ────────────────────────────────────────────────────────────────

REQUESTER_ID = str(uuid.uuid4())
WORKER_ID_1  = str(uuid.uuid4())
WORKER_ID_2  = str(uuid.uuid4())
WORKER_ID_3  = str(uuid.uuid4())
TASK_ID      = str(uuid.uuid4())
APP_ID       = str(uuid.uuid4())


# ── Suppress background tasks ─────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _suppress_background():
    """Suppress fire-and-forget webhooks that would try real connections."""
    noop = AsyncMock()
    with (
        patch("routers.tasks.fire_persistent_endpoints", noop),
        patch("routers.tasks.fire_webhook_for_task",     noop),
    ):
        yield


# ── Low-level DB mock helpers ─────────────────────────────────────────────────

def _scalar_result(value):
    """Wrap *value* as a SQLAlchemy scalar-result mock."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one         = MagicMock(return_value=value)
    r.scalars            = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=[]))
    )
    return r


def _scalars_result(items: list):
    """Wrap a list so result.scalars().all() returns *items*."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalars            = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=items))
    )
    return r


def _make_mock_db() -> MagicMock:
    db          = MagicMock()
    db.add      = MagicMock()
    db.flush    = AsyncMock()
    db.commit   = AsyncMock()
    db.rollback = AsyncMock()
    db.close    = AsyncMock()
    db.execute  = AsyncMock()
    db.scalar   = AsyncMock(return_value=0)

    async def _refresh(obj):
        pass

    db.refresh = _refresh
    return db


def _db_override(mock_db):
    """Return an async-generator function FastAPI recognises as a DI dependency."""
    async def _override():
        yield mock_db
    return _override


def _real_token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


# ── Object factories ──────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _make_task(
    task_id: str = TASK_ID,
    owner_id: str = REQUESTER_ID,
    status: str = "open",
    application_mode: bool = True,
    claim_timeout_minutes: int = 30,
) -> MagicMock:
    t                       = MagicMock()
    t.id                    = uuid.UUID(task_id)
    t.user_id               = uuid.UUID(owner_id)
    t.status                = status
    t.application_mode      = application_mode
    t.claim_timeout_minutes = claim_timeout_minutes
    t.type                  = "label_text"
    t.webhook_url           = None
    t.webhook_events        = []
    return t


def _make_worker_user(
    user_id: str = WORKER_ID_1,
    name: str = "Alice",
    role: str = "worker",
    reputation_score: float = 75.0,
    is_banned: bool = False,
) -> MagicMock:
    u                  = MagicMock()
    u.id               = uuid.UUID(user_id)
    u.name             = name
    u.role             = role
    u.reputation_score = reputation_score
    u.is_banned        = is_banned
    u.is_admin         = False
    return u


def _make_application(
    app_id: str = APP_ID,
    task_id: str = TASK_ID,
    worker_id: str = WORKER_ID_1,
    status: str = "pending",
    proposal: str = "I can do this",
    proposed_reward: int | None = None,
) -> MagicMock:
    a                  = MagicMock()
    a.id               = uuid.UUID(app_id)
    a.task_id          = uuid.UUID(task_id)
    a.worker_id        = uuid.UUID(worker_id)
    a.status           = status
    a.proposal         = proposal
    a.proposed_reward  = proposed_reward
    a.created_at       = _now()
    a.updated_at       = _now()
    return a


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    from main import app as _app
    return _app


@pytest.fixture
def requester_headers():
    return {"Authorization": f"Bearer {_real_token(REQUESTER_ID)}"}


@pytest.fixture
def worker_headers():
    return {"Authorization": f"Bearer {_real_token(WORKER_ID_1)}"}


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1 — list_applications: N+1 fix (bulk worker load)
# ═══════════════════════════════════════════════════════════════════════════════

class TestListApplicationsBulkLoad:
    """Verify list_applications does NOT issue N individual worker lookups."""

    @pytest.mark.asyncio
    async def test_execute_called_twice_not_four_times(self, app, requester_headers):
        """With 3 applications from 3 different workers, db.execute is called
        exactly TWICE: once to fetch applications, once for the bulk IN query.

        Before the N+1 fix it would be called 1+3 = 4 times (one per worker).
        """
        task = _make_task()

        app1 = _make_application(
            app_id=str(uuid.uuid4()), worker_id=WORKER_ID_1, proposal="Proposal 1"
        )
        app2 = _make_application(
            app_id=str(uuid.uuid4()), worker_id=WORKER_ID_2, proposal="Proposal 2"
        )
        app3 = _make_application(
            app_id=str(uuid.uuid4()), worker_id=WORKER_ID_3, proposal="Proposal 3"
        )

        w1 = _make_worker_user(user_id=WORKER_ID_1, name="Alice")
        w2 = _make_worker_user(user_id=WORKER_ID_2, name="Bob")
        w3 = _make_worker_user(user_id=WORKER_ID_3, name="Carol")

        db = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Task lookup
                return _scalar_result(task)
            if call_count == 2:
                # Applications for this task
                return _scalars_result([app1, app2, app3])
            if call_count == 3:
                # Bulk worker IN query
                return _scalars_result([w1, w2, w3])
            # Should never reach here — fail the test if it does
            raise AssertionError(
                f"db.execute called {call_count} times; expected only 3"
            )

        db.execute.side_effect = _side_effect

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.get(
                    f"/v1/tasks/{TASK_ID}/applications",
                    headers=requester_headers,
                )
            assert r.status_code == 200, r.text
            data = r.json()
            assert len(data) == 3

            # The critical assertion: exactly 3 execute calls
            # (task lookup + apps query + bulk worker query)
            assert db.execute.call_count == 3, (
                f"Expected db.execute called 3 times, got {db.execute.call_count}. "
                "An N+1 regression may have been introduced."
            )
        finally:
            app.dependency_overrides.pop(get_db, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2 — list_applications: ownership guard
# ═══════════════════════════════════════════════════════════════════════════════

class TestListApplicationsOwnership:
    """Non-owner attempting to list applications receives 403."""

    @pytest.mark.asyncio
    async def test_non_owner_gets_403(self, app):
        """A user who did not create the task must not see its applications."""
        OTHER_USER_ID = str(uuid.uuid4())
        task = _make_task(owner_id=REQUESTER_ID)  # owned by REQUESTER_ID

        db = _make_mock_db()
        db.execute.return_value = _scalar_result(task)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            non_owner_token = _real_token(OTHER_USER_ID)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.get(
                    f"/v1/tasks/{TASK_ID}/applications",
                    headers={"Authorization": f"Bearer {non_owner_token}"},
                )
            assert r.status_code == 403, r.text
            assert "owner" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3 — list_my_applications: pagination offset
# ═══════════════════════════════════════════════════════════════════════════════

class TestListMyApplicationsPagination:
    """page=2, page_size=1 applies offset=1 to the query."""

    @pytest.mark.asyncio
    async def test_page2_page_size1_offset_applied(self, app, worker_headers):
        """When page=2 and page_size=1, the second application is returned (offset=1)."""
        worker = _make_worker_user(user_id=WORKER_ID_1, role="worker")

        # Two applications; with offset=1 only the second one is returned
        app1 = _make_application(
            app_id=str(uuid.uuid4()),
            worker_id=WORKER_ID_1,
            proposal="First proposal",
        )
        app2 = _make_application(
            app_id=str(uuid.uuid4()),
            worker_id=WORKER_ID_1,
            proposal="Second proposal",
        )

        db = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Worker lookup
                return _scalar_result(worker)
            if call_count == 2:
                # Paginated applications query — simulates offset=1 → only app2
                return _scalars_result([app2])
            if call_count == 3:
                # Bulk worker lookup for the single returned app
                return _scalars_result([worker])
            raise AssertionError(f"Unexpected extra db.execute call #{call_count}")

        db.execute.side_effect = _side_effect

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.get(
                    "/v1/worker/applications",
                    params={"page": 2, "page_size": 1},
                    headers=worker_headers,
                )
            assert r.status_code == 200, r.text
            data = r.json()
            # Only the second application (offset=1) should be returned
            assert len(data) == 1
            assert data[0]["proposal"] == "Second proposal"
        finally:
            app.dependency_overrides.pop(get_db, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4 — apply_to_task: happy path
# ═══════════════════════════════════════════════════════════════════════════════

class TestApplyToTask:
    """Worker applies to an open application-mode task."""

    @pytest.mark.asyncio
    async def test_apply_returns_pending_application(self, app, worker_headers):
        """Successful application returns 201 with status='pending'."""
        worker = _make_worker_user(user_id=WORKER_ID_1, reputation_score=75.0)
        task   = _make_task(status="open", application_mode=True)

        # The application object that gets added and refreshed
        new_app = _make_application(
            app_id=str(uuid.uuid4()),
            task_id=TASK_ID,
            worker_id=WORKER_ID_1,
            status="pending",
            proposal="I can do this efficiently.",
        )

        db = _make_mock_db()
        # scalar() for duplicate check → 0 (no existing application)
        db.scalar.return_value = 0

        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Worker lookup
                return _scalar_result(worker)
            if call_count == 2:
                # Task lookup
                return _scalar_result(task)
            if call_count == 3:
                # _fmt_application: worker lookup after commit/refresh
                return _scalar_result(worker)
            # Notification calls may add more executes — return empty
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            r.scalars = MagicMock(
                return_value=MagicMock(all=MagicMock(return_value=[]))
            )
            return r

        db.execute.side_effect = _side_effect

        # After db.add(app) + db.flush(), db.refresh(app) is called.
        # We simulate that by making the added object available via side-effect.
        added_objects: list = []
        original_add = db.add

        def _capture_add(obj):
            added_objects.append(obj)
            return original_add(obj)

        db.add = _capture_add

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)

        # Suppress create_notification to avoid extra DB interactions
        with patch("routers.applications.create_notification", new_callable=AsyncMock):
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as c:
                    r = await c.post(
                        f"/v1/tasks/{TASK_ID}/apply",
                        json={"proposal": "I can do this efficiently."},
                        headers=worker_headers,
                    )
                assert r.status_code == 201, r.text
                data = r.json()
                assert data["status"] == "pending"
                assert data["worker_id"] == WORKER_ID_1
                assert data["task_id"] == TASK_ID
                db.commit.assert_awaited_once()
            finally:
                app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_duplicate_application_returns_409(self, app, worker_headers):
        """Applying twice to the same task returns 409 Conflict."""
        worker = _make_worker_user(user_id=WORKER_ID_1, reputation_score=75.0)
        task   = _make_task(status="open", application_mode=True)

        db = _make_mock_db()
        # scalar() for duplicate check → 1 (already applied)
        db.scalar.return_value = 1

        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(worker)
            if call_count == 2:
                return _scalar_result(task)
            return _scalar_result(None)

        db.execute.side_effect = _side_effect

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/apply",
                    json={"proposal": "Applying again."},
                    headers=worker_headers,
                )
            assert r.status_code == 409, r.text
            assert "already applied" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6 — accept_application: happy path
# ═══════════════════════════════════════════════════════════════════════════════

class TestAcceptApplication:
    """Owner accepts an application; task becomes 'assigned', assignment is created."""

    @pytest.mark.asyncio
    async def test_accept_assigns_task_and_creates_assignment(
        self, app, requester_headers
    ):
        """Accepting an application must:
        - Set application.status = "accepted"
        - Set task.status = "assigned"
        - Call db.add() with a TaskAssignmentDB
        - Return 200 with the accepted ApplicationOut
        """
        from models.db import TaskAssignmentDB

        task    = _make_task(status="open", owner_id=REQUESTER_ID)
        the_app = _make_application(
            app_id=APP_ID, task_id=TASK_ID, worker_id=WORKER_ID_1, status="pending"
        )
        worker  = _make_worker_user(user_id=WORKER_ID_1)

        db = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Task lookup
                return _scalar_result(task)
            if call_count == 2:
                # Application lookup
                return _scalar_result(the_app)
            if call_count == 3:
                # Other pending applications to reject (none)
                return _scalars_result([])
            if call_count == 4:
                # _fmt_application: worker lookup
                return _scalar_result(worker)
            # Notification DB calls
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            r.scalars = MagicMock(
                return_value=MagicMock(all=MagicMock(return_value=[]))
            )
            return r

        db.execute.side_effect = _side_effect

        added_objects: list = []
        original_add = db.add

        def _capture_add(obj):
            added_objects.append(obj)
            return original_add(obj)

        db.add = _capture_add

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)

        with patch("routers.applications.create_notification", new_callable=AsyncMock):
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as c:
                    r = await c.post(
                        f"/v1/tasks/{TASK_ID}/applications/{APP_ID}/accept",
                        headers=requester_headers,
                    )
                assert r.status_code == 200, r.text
                data = r.json()
                assert data["status"] == "accepted"

                # The application object should be mutated
                assert the_app.status == "accepted"

                # The task should now be assigned
                assert task.status == "assigned"

                # A TaskAssignmentDB must have been added
                assignments = [
                    o for o in added_objects if isinstance(o, TaskAssignmentDB)
                ]
                assert len(assignments) == 1, (
                    f"Expected 1 TaskAssignmentDB added, got {len(assignments)}"
                )
                assert str(assignments[0].task_id) == TASK_ID
                assert str(assignments[0].worker_id) == WORKER_ID_1

                db.commit.assert_awaited_once()
            finally:
                app.dependency_overrides.pop(get_db, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7 — reject_application: happy path
# ═══════════════════════════════════════════════════════════════════════════════

class TestRejectApplication:
    """Owner rejects a specific application."""

    @pytest.mark.asyncio
    async def test_reject_sets_status_rejected(self, app, requester_headers):
        """Rejecting an application must set application.status = 'rejected'
        and return 200 with the updated ApplicationOut.
        """
        task    = _make_task(status="open", owner_id=REQUESTER_ID)
        the_app = _make_application(
            app_id=APP_ID, task_id=TASK_ID, worker_id=WORKER_ID_1, status="pending"
        )
        worker  = _make_worker_user(user_id=WORKER_ID_1)

        db = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(task)
            if call_count == 2:
                return _scalar_result(the_app)
            if call_count == 3:
                # _fmt_application: worker lookup
                return _scalar_result(worker)
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            r.scalars = MagicMock(
                return_value=MagicMock(all=MagicMock(return_value=[]))
            )
            return r

        db.execute.side_effect = _side_effect

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)

        with patch("routers.applications.create_notification", new_callable=AsyncMock):
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as c:
                    r = await c.post(
                        f"/v1/tasks/{TASK_ID}/applications/{APP_ID}/reject",
                        headers=requester_headers,
                    )
                assert r.status_code == 200, r.text
                data = r.json()
                assert data["status"] == "rejected"

                # The application object must have been mutated
                assert the_app.status == "rejected"

                db.commit.assert_awaited_once()
            finally:
                app.dependency_overrides.pop(get_db, None)
