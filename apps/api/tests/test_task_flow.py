"""Full task-flow E2E tests (mocked DB).

Covers the complete human-task happy path:
  1. Requester POSTs /v1/tasks (label_text, 1 assignment) → 201
  2. Worker POSTs /v1/worker/tasks/{task_id}/claim        → 200
  3. Worker POSTs /v1/worker/tasks/{task_id}/submit       → 200
  4. Requester POSTs /v1/tasks/{task_id}/submissions/{assignment_id}/approve → 200

Each step has its own isolated mock DB session (as in production — a fresh
AsyncSession is created per-request via DI).  Shared UUIDs simulate the same
database rows being read in subsequent requests.

Additional tests cover negative paths:
  - Insufficient credits → 402
  - Worker claiming a task they already hold → 409
  - Submitting with an expired assignment → 410
  - Non-owner cannot approve → 404
  - Worker cannot approve (role guard) → 404 (task not found, owned by requester)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# Must precede app imports
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "flow-test-secret")
os.environ.setdefault("API_KEY_SALT",  "flow-test-salt")
os.environ.setdefault("DEBUG", "true")

import asyncio
import pytest
from httpx import AsyncClient, ASGITransport


# ── Background-task suppression ───────────────────────────────────────────────
# Route handlers fire background tasks via asyncio.create_task() for webhooks,
# emails, and notifications. These tasks try to open a real DB/network
# connection in tests and fail, leaking unhandled exceptions into the next
# test's event loop via Starlette's anyio task group.
#
# We suppress the two primary fire-and-forget functions by patching them to
# async no-ops for the duration of every test in this module.

_NOOP = AsyncMock()


@pytest.fixture(autouse=True)
def _suppress_background_webhooks():
    """Patch fire_persistent_endpoints and fire_webhook_for_task at all
    call-sites (tasks.py and worker.py import them directly) to async no-ops.
    This prevents DB/network connections from leaking between tests."""
    noop = AsyncMock()
    with (
        patch("routers.tasks.fire_persistent_endpoints", noop),
        patch("routers.tasks.fire_webhook_for_task",     noop),
        patch("routers.worker.fire_persistent_endpoints", noop),
        patch("routers.worker.fire_webhook_for_task",     noop),
    ):
        yield


# ── Shared UUIDs ──────────────────────────────────────────────────────────────

REQUESTER_ID = str(uuid.uuid4())
WORKER_ID     = str(uuid.uuid4())
TASK_ID       = str(uuid.uuid4())
ASSIGN_ID     = str(uuid.uuid4())


# ── Low-level mock helpers ────────────────────────────────────────────────────

def _scalar_result(value):
    """Wrap *value* as a SQLAlchemy scalar-result mock."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return r


def _make_mock_db() -> MagicMock:
    db = MagicMock()
    db.add      = MagicMock()
    db.flush    = AsyncMock()
    db.commit   = AsyncMock()
    db.rollback = AsyncMock()
    db.close    = AsyncMock()
    db.execute  = AsyncMock()
    # scalar() is used by claim_task for COUNT queries
    db.scalar   = AsyncMock(return_value=0)

    # refresh() should stamp a UUID on the object if it doesn't have one yet.
    # This mirrors what SQLAlchemy does when refreshing from the DB after a flush.
    async def _refresh(obj):
        if hasattr(obj, "id") and obj.id is None:
            import uuid as _uuid
            obj.id = _uuid.uuid4()
        if hasattr(obj, "status") and obj.status is None:
            pass  # leave as-is

    db.refresh = _refresh
    return db


async def _db_gen(mock_db):
    """Async generator that yields *mock_db* — used as a FastAPI DI override."""
    yield mock_db


def _db_override(mock_db):
    """Return an async-generator *function* FastAPI will recognise as a
    generator dependency (inspect.isasyncgenfunction == True)."""
    async def _override():
        yield mock_db
    return _override


def _real_token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


# ── Mock object factories ─────────────────────────────────────────────────────

def _make_requester(credits: int = 1000) -> MagicMock:
    u = MagicMock()
    u.id                     = uuid.UUID(REQUESTER_ID)
    u.email                  = "requester@test.com"
    u.role                   = "requester"
    u.credits                = credits
    u.name                   = "Test Requester"
    u.is_banned              = False
    u.is_admin               = False
    u.totp_enabled           = False
    u.plan                   = "free"
    u.credit_alert_threshold = None   # disables credit-alert path
    u.credit_alert_fired     = False
    u.created_at             = datetime.now(timezone.utc)
    u.token_version          = 0
    return u


def _make_worker(is_banned: bool = False) -> MagicMock:
    w = MagicMock()
    w.id                        = uuid.UUID(WORKER_ID)
    w.email                     = "worker@test.com"
    w.role                      = "worker"
    w.credits                   = 100
    w.name                      = "Test Worker"
    w.is_banned                 = is_banned
    w.reputation_score          = 100.0
    w.worker_xp                 = 0
    w.worker_level              = 1
    w.worker_tasks_completed    = 0
    w.worker_streak_days        = 0
    w.worker_last_active_date   = None
    w.worker_reliability        = None
    w.plan                      = "free"
    w.token_version             = 0
    u_attrs = {"id": uuid.UUID(WORKER_ID), "email": "worker@test.com"}
    return w


def _make_open_task(status: str = "open") -> MagicMock:
    t = MagicMock()
    t.id                    = uuid.UUID(TASK_ID)
    t.user_id               = uuid.UUID(REQUESTER_ID)
    t.type                  = "label_text"
    t.status                = status
    t.execution_mode        = "human"
    t.input                 = {"text": "classify this sentence"}
    t.output                = None
    t.worker_reward_credits = 5
    t.assignments_required  = 1
    t.assignments_completed = 0
    t.consensus_strategy    = "any_first"
    t.priority              = "normal"
    t.claim_timeout_minutes = 60
    t.application_mode      = False
    t.assigned_team_id      = None
    t.min_reputation_score  = None
    t.min_skill_level       = None
    t.webhook_url           = None
    t.webhook_events        = []
    t.certified_only        = False
    t.tags                  = []
    t.org_id                = None
    t.cached                = False
    t.created_at            = datetime.now(timezone.utc)
    t.completed_at          = None
    t.winning_assignment_id = None
    return t


def _make_assignment(status: str = "active", expired: bool = False) -> MagicMock:
    a = MagicMock()
    a.id               = uuid.UUID(ASSIGN_ID)
    a.task_id          = uuid.UUID(TASK_ID)
    a.worker_id        = uuid.UUID(WORKER_ID)
    a.status           = status
    a.response         = None
    a.worker_note      = None
    a.earnings_credits = 5
    a.xp_earned        = 10
    a.claimed_at       = datetime.now(timezone.utc) - timedelta(minutes=5)
    a.submitted_at     = None if status == "active" else datetime.now(timezone.utc)
    if expired:
        a.timeout_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    else:
        a.timeout_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return a


# ── Fixture ───────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    from main import app as _app
    return _app


@pytest.fixture
def requester_headers():
    return {"Authorization": f"Bearer {_real_token(REQUESTER_ID)}"}


@pytest.fixture
def worker_headers():
    return {"Authorization": f"Bearer {_real_token(WORKER_ID)}"}


# ── Step 1: Create task ───────────────────────────────────────────────────────

class TestCreateHumanTask:
    """Tests for POST /v1/tasks (human task creation)."""

    @pytest.mark.asyncio
    async def test_create_label_text_task_success(self, app, requester_headers):
        """Requester with sufficient credits can create a human task → 201."""
        requester = _make_requester(credits=1000)
        db = _make_mock_db()
        db.execute.return_value = _scalar_result(requester)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            with (
                patch("core.quotas.enforce_task_creation_quota", new=AsyncMock()),
                patch("core.quotas.enforce_task_burst_limit",    new=AsyncMock()),
                patch("core.quotas.record_task_creation",        new=AsyncMock()),
                patch("core.quotas.record_task_burst",           new=AsyncMock()),
                patch("core.credit_alerts.maybe_fire_credit_alert", new=AsyncMock()),
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    r = await c.post("/v1/tasks", json={
                        "type": "label_text",
                        "input": {"text": "classify this"},
                        "assignments_required": 1,
                        "worker_reward_credits": 5,
                    }, headers=requester_headers)

            assert r.status_code == 201, r.text
            data = r.json()
            assert "task_id" in data
            assert data["status"] in ("open", "pending")
            assert data["estimated_credits"] > 0
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_create_task_insufficient_credits(self, app, requester_headers):
        """Requester with 0 credits gets 402 Insufficient Credits."""
        requester = _make_requester(credits=0)
        db = _make_mock_db()
        db.execute.return_value = _scalar_result(requester)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            with (
                patch("core.quotas.enforce_task_creation_quota", new=AsyncMock()),
                patch("core.quotas.enforce_task_burst_limit",    new=AsyncMock()),
                patch("core.quotas.record_task_creation",        new=AsyncMock()),
                patch("core.quotas.record_task_burst",           new=AsyncMock()),
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    r = await c.post("/v1/tasks", json={
                        "type": "label_text",
                        "input": {"text": "classify this"},
                        "assignments_required": 1,
                        "worker_reward_credits": 5,
                    }, headers=requester_headers)

            assert r.status_code == 402
            detail = r.json()["detail"]
            assert detail["error"] == "insufficient_credits"
            assert "required"  in detail
            assert "available" in detail
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_create_task_user_not_found(self, app, requester_headers):
        """Returns 404 if the requester's user record doesn't exist in DB."""
        db = _make_mock_db()
        db.execute.return_value = _scalar_result(None)  # user not found

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/tasks", json={
                    "type": "label_text",
                    "input": {"text": "test"},
                }, headers=requester_headers)

            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_create_task_no_auth_401(self, app):
        """Task creation without auth → 401."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/tasks", json={"type": "label_text", "input": {}})
        assert r.status_code == 401


# ── Step 2: Claim task ────────────────────────────────────────────────────────

class TestClaimTask:
    """Tests for POST /v1/worker/tasks/{task_id}/claim."""

    def _db_for_claim(self, worker: MagicMock, task: MagicMock) -> MagicMock:
        """Build a mock DB for a successful claim: worker found, task found,
        no existing claims, no active assignments, no worker overload."""
        db = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(worker)   # user lookup
            if call_count == 2:
                return _scalar_result(task)     # task lookup
            return _scalar_result(None)         # any further execute() calls

        db.execute.side_effect = _side_effect
        # db.scalar() used for COUNT queries — all return 0 (fresh slate)
        db.scalar = AsyncMock(return_value=0)
        return db

    @pytest.mark.asyncio
    async def test_claim_open_task_success(self, app, worker_headers):
        """A worker can claim an open human task → 200 with assignment_id."""
        worker = _make_worker()
        task   = _make_open_task(status="open")
        db     = self._db_for_claim(worker, task)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{TASK_ID}/claim",
                    headers=worker_headers,
                )

            assert r.status_code == 200, r.text
            data = r.json()
            assert "assignment_id" in data
            assert data["task_id"] == TASK_ID
            assert "timeout_at" in data
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_claim_by_requester_returns_403(self, app):
        """A requester-role user cannot claim a task → 403."""
        requester_id = str(uuid.uuid4())
        headers      = {"Authorization": f"Bearer {_real_token(requester_id)}"}
        requester    = _make_requester()
        db = _make_mock_db()
        db.execute.return_value = _scalar_result(requester)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{TASK_ID}/claim",
                    headers=headers,
                )
            assert r.status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_claim_banned_worker_returns_403(self, app, worker_headers):
        """A banned worker cannot claim tasks → 403."""
        worker = _make_worker(is_banned=True)
        db = _make_mock_db()
        db.execute.return_value = _scalar_result(worker)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{TASK_ID}/claim",
                    headers=worker_headers,
                )
            assert r.status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_claim_nonexistent_task_returns_404(self, app, worker_headers):
        """Claiming a task that doesn't exist → 404."""
        worker = _make_worker()
        db = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(worker)  # user lookup
            return _scalar_result(None)        # task not found

        db.execute.side_effect = _side_effect
        db.scalar = AsyncMock(return_value=0)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{TASK_ID}/claim",
                    headers=worker_headers,
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_claim_already_claimed_returns_409(self, app, worker_headers):
        """Claiming a task the worker already holds → 409."""
        worker = _make_worker()
        task   = _make_open_task()
        db     = _make_mock_db()
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

        # First scalar() call (existing claim check) returns 1 → already claimed
        scalar_call_count = 0
        async def _scalar_side(*args, **kwargs):
            nonlocal scalar_call_count
            scalar_call_count += 1
            if scalar_call_count == 1:
                return 1  # existing active claim found
            return 0

        db.scalar.side_effect = _scalar_side

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{TASK_ID}/claim",
                    headers=worker_headers,
                )
            assert r.status_code == 409
            assert "already" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_claim_requires_auth(self, app):
        """Unauthenticated claim → 401."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(f"/v1/worker/tasks/{TASK_ID}/claim")
        assert r.status_code == 401


# ── Step 3: Submit task ───────────────────────────────────────────────────────

class TestSubmitTask:
    """Tests for POST /v1/worker/tasks/{task_id}/submit."""

    def _db_for_submit(
        self,
        assignment: MagicMock,
        task: MagicMock,
        worker: MagicMock,
    ) -> MagicMock:
        """Build a mock DB for a successful submit."""
        db = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(assignment)  # active assignment lookup
            if call_count == 2:
                return _scalar_result(task)        # task lookup
            if call_count == 3:
                return _scalar_result(worker)      # worker user lookup
            # Any further calls (daily challenge, badges, requester notify) → None
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            return r

        db.execute.side_effect = _side_effect
        db.scalar = AsyncMock(return_value=0)  # released/timed_out count
        return db

    @pytest.mark.asyncio
    async def test_submit_task_success(self, app, worker_headers):
        """Worker submits a response to their active assignment → 200."""
        assignment = _make_assignment(status="active")
        task       = _make_open_task(status="assigned")
        worker     = _make_worker()
        db         = self._db_for_submit(assignment, task, worker)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{TASK_ID}/submit",
                    json={"response": {"label": "positive", "confidence": 0.9}},
                    headers=worker_headers,
                )

            assert r.status_code == 200, r.text
            data = r.json()
            assert "assignment_id" in data
            assert data["status"] == "submitted"
            assert data["earnings_credits"] == assignment.earnings_credits
            assert data["xp_earned"] >= 0
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_submit_no_active_assignment_404(self, app, worker_headers):
        """Submitting without an active assignment → 404."""
        db = _make_mock_db()
        db.execute.return_value = _scalar_result(None)  # no active assignment

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{TASK_ID}/submit",
                    json={"response": {"label": "positive"}},
                    headers=worker_headers,
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_submit_expired_assignment_410(self, app, worker_headers):
        """Submitting after the assignment timeout → 410 Gone."""
        assignment = _make_assignment(status="active", expired=True)
        db = _make_mock_db()
        db.execute.return_value = _scalar_result(assignment)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{TASK_ID}/submit",
                    json={"response": {"label": "positive"}},
                    headers=worker_headers,
                )
            assert r.status_code == 410
            assert "expired" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_submit_requires_auth(self, app):
        """Unauthenticated submit → 401."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/worker/tasks/{TASK_ID}/submit",
                json={"response": {"label": "positive"}},
            )
        assert r.status_code == 401


# ── Step 4: Approve submission ────────────────────────────────────────────────

class TestApproveSubmission:
    """Tests for POST /v1/tasks/{task_id}/submissions/{assignment_id}/approve."""

    def _db_for_approve(
        self,
        task: MagicMock,
        assignment: MagicMock,
    ) -> MagicMock:
        """Build a mock DB for a successful approval."""
        db = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(task)        # task lookup (owned by requester)
            if call_count == 2:
                return _scalar_result(assignment)  # assignment lookup
            # Further calls (worker email lookup, skill update) → None
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            return r

        db.execute.side_effect = _side_effect
        return db

    @pytest.mark.asyncio
    async def test_approve_submitted_assignment_success(self, app, requester_headers):
        """Requester approves a submitted assignment → 200."""
        task       = _make_open_task(status="completed")
        assignment = _make_assignment(status="submitted")
        db         = self._db_for_approve(task, assignment)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={},
                    headers=requester_headers,
                )

            assert r.status_code == 200, r.text
            data = r.json()
            assert data["status"] == "approved"
            assert data["assignment_id"] == ASSIGN_ID
            assert "approved" in data["message"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_approve_already_approved_returns_200(self, app, requester_headers):
        """Approving an already-approved assignment is idempotent → 200."""
        task       = _make_open_task(status="completed")
        assignment = _make_assignment(status="approved")  # already approved
        db         = self._db_for_approve(task, assignment)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={},
                    headers=requester_headers,
                )
            assert r.status_code == 200
            assert r.json()["status"] == "approved"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_approve_wrong_status_returns_409(self, app, requester_headers):
        """Approving a 'rejected' assignment → 409 Conflict."""
        task       = _make_open_task(status="open")
        assignment = _make_assignment(status="rejected")
        db         = self._db_for_approve(task, assignment)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={},
                    headers=requester_headers,
                )
            assert r.status_code == 409
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_approve_task_not_owned_returns_404(self, app):
        """A user who does not own the task cannot approve submissions → 404."""
        other_user_id = str(uuid.uuid4())
        other_headers = {"Authorization": f"Bearer {_real_token(other_user_id)}"}

        db = _make_mock_db()
        db.execute.return_value = _scalar_result(None)   # task not found for this user

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={},
                    headers=other_headers,
                )
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_approve_requires_auth(self, app):
        """Unauthenticated approve → 401."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                json={},
            )
        assert r.status_code == 401


# ── Reject submission ─────────────────────────────────────────────────────────

class TestRejectSubmission:
    """Tests for POST /v1/tasks/{task_id}/submissions/{assignment_id}/reject."""

    def _db_for_reject(self, task: MagicMock, assignment: MagicMock) -> MagicMock:
        db = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(task)
            if call_count == 2:
                return _scalar_result(assignment)
            if call_count == 3:
                # requester lookup for refund
                return _scalar_result(_make_requester())
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            return r

        db.execute.side_effect = _side_effect
        return db

    @pytest.mark.asyncio
    async def test_reject_submitted_assignment_success(self, app, requester_headers):
        """Requester rejects a submitted assignment → 200 with refund message."""
        task       = _make_open_task(status="completed")
        assignment = _make_assignment(status="submitted")
        db         = self._db_for_reject(task, assignment)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/reject",
                    json={"reason": "The label was incorrect."},
                    headers=requester_headers,
                )

            assert r.status_code == 200, r.text
            data = r.json()
            assert data["status"] == "rejected"
            assert "refund" in data["message"].lower() or "rejected" in data["message"].lower()
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_reject_active_assignment_returns_409(self, app, requester_headers):
        """Rejecting an 'active' assignment (not yet submitted) → 409."""
        task       = _make_open_task(status="assigned")
        assignment = _make_assignment(status="active")
        db         = self._db_for_reject(task, assignment)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/reject",
                    json={},
                    headers=requester_headers,
                )
            assert r.status_code == 409
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_reject_requires_auth(self, app):
        """Unauthenticated reject → 401."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/reject",
                json={"reason": "bad"},
            )
        assert r.status_code == 401


# ── Full happy-path flow (sequential steps sharing UUIDs) ─────────────────────

class TestFullHappyPath:
    """Documents the full requester→worker→requester approval flow as four
    sequential steps, each with their own isolated mock DB session.

    This is primarily a documentation / regression-anchor test to confirm
    all four happy-path steps produce the expected HTTP codes and
    response shapes.
    """

    @pytest.mark.asyncio
    async def test_step1_create_task(self, app, requester_headers):
        """Step 1: Requester creates a label_text task → 201."""
        requester = _make_requester(credits=500)
        db = _make_mock_db()
        db.execute.return_value = _scalar_result(requester)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            with (
                patch("core.quotas.enforce_task_creation_quota", new=AsyncMock()),
                patch("core.quotas.enforce_task_burst_limit",    new=AsyncMock()),
                patch("core.quotas.record_task_creation",        new=AsyncMock()),
                patch("core.quotas.record_task_burst",           new=AsyncMock()),
                patch("core.credit_alerts.maybe_fire_credit_alert", new=AsyncMock()),
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    r = await c.post("/v1/tasks", json={
                        "type": "label_text",
                        "input": {"text": "Is this sentence positive or negative?"},
                        "assignments_required": 1,
                        "worker_reward_credits": 5,
                    }, headers=requester_headers)

            assert r.status_code == 201
            assert r.json()["status"] in ("open", "pending")
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_step2_claim_task(self, app, worker_headers):
        """Step 2: Worker claims the task → 200 with assignment_id."""
        worker = _make_worker()
        task   = _make_open_task(status="open")
        db     = _make_mock_db()
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
        db.scalar = AsyncMock(return_value=0)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{TASK_ID}/claim",
                    headers=worker_headers,
                )

            assert r.status_code == 200
            data = r.json()
            assert "assignment_id" in data
            assert data["task_id"] == TASK_ID
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_step3_submit_task(self, app, worker_headers):
        """Step 3: Worker submits their response → 200 with earnings."""
        assignment = _make_assignment(status="active")
        task       = _make_open_task(status="assigned")
        worker     = _make_worker()
        db         = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(assignment)
            if call_count == 2:
                return _scalar_result(task)
            if call_count == 3:
                return _scalar_result(worker)
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            return r

        db.execute.side_effect = _side_effect
        db.scalar = AsyncMock(return_value=0)

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/worker/tasks/{TASK_ID}/submit",
                    json={"response": {"label": "positive", "confidence": 0.95}},
                    headers=worker_headers,
                )

            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "submitted"
            assert data["earnings_credits"] == 5
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_step4_approve_submission(self, app, requester_headers):
        """Step 4: Requester approves the submission → 200 confirmed."""
        task       = _make_open_task(status="completed")
        assignment = _make_assignment(status="submitted")
        db         = _make_mock_db()
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(task)
            if call_count == 2:
                return _scalar_result(assignment)
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            return r

        db.execute.side_effect = _side_effect

        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/v1/tasks/{TASK_ID}/submissions/{ASSIGN_ID}/approve",
                    json={},
                    headers=requester_headers,
                )

            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "approved"
            assert data["assignment_id"] == ASSIGN_ID
        finally:
            app.dependency_overrides.pop(get_db, None)
