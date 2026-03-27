"""Security tests: IDOR protection in the worker marketplace task-detail endpoint.

GET /v1/worker/tasks/{task_id}  (worker.py → get_marketplace_task)

Before the fix a worker could call this endpoint with ANY task UUID and receive
the full task details (input payload, output, instructions, etc.) regardless of:
  - task status (assigned, completed, cancelled, ...)
  - which requester owns the task
  - whether the worker has any relationship with the task

After the fix the endpoint only shows a task when EITHER:
  1. task.status == "open"  (visible in the marketplace), OR
  2. the calling worker has an active/submitted assignment on that task.

Tests in this module verify the IDOR protection is in place.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "security-test-secret")
os.environ.setdefault("API_KEY_SALT", "security-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Shared IDs ────────────────────────────────────────────────────────────────

WORKER_ID    = str(uuid.uuid4())
REQUESTER_ID = str(uuid.uuid4())
TASK_ID      = str(uuid.uuid4())
ASSIGN_ID    = str(uuid.uuid4())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _real_token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _scalar_result(value):
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
    db.scalar   = AsyncMock(return_value=0)

    async def _refresh(obj): pass
    db.refresh = _refresh
    return db


def _db_override(mock_db):
    async def _override():
        yield mock_db
    return _override


def _make_worker_user() -> MagicMock:
    u = MagicMock()
    u.id   = uuid.UUID(WORKER_ID)
    u.role = "worker"
    u.is_banned = False
    u.reputation_score = 100.0
    u.plan = "free"
    u.created_at = datetime.now(timezone.utc)
    return u


def _make_task(status: str = "open") -> MagicMock:
    t = MagicMock()
    t.id                    = uuid.UUID(TASK_ID)
    t.user_id               = uuid.UUID(REQUESTER_ID)
    t.type                  = "label_text"
    t.status                = status
    t.execution_mode        = "human"
    t.input                 = {"text": "classify this"}
    t.output                = None
    t.error                 = None
    t.credits_used          = None
    t.duration_ms           = None
    t.task_metadata         = None
    t.worker_reward_credits = 5
    t.assignments_required  = 1
    t.assignments_completed = 0
    t.task_instructions     = "Please classify the text."
    t.is_gold_standard      = False
    t.consensus_strategy    = "any_first"
    t.dispute_status        = None
    t.winning_assignment_id = None
    t.org_id                = None
    t.tags                  = []
    t.scheduled_at          = None
    t.priority_escalated_at = None
    t.cached                = False
    t.created_at            = datetime.now(timezone.utc)
    t.started_at            = None
    t.completed_at          = None
    t.priority              = "normal"
    return t


# ── Background noise suppression ─────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _suppress_background():
    noop = AsyncMock()
    with (
        patch("routers.worker.fire_persistent_endpoints", noop),
        patch("routers.worker.fire_webhook_for_task",     noop),
    ):
        yield


# ── App fixture ───────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    from main import app as _app
    return _app


@pytest.fixture
def worker_auth():
    return {"Authorization": f"Bearer {_real_token(WORKER_ID)}"}


# ═══════════════════════════════════════════════════════════════════════════════
# IDOR protection tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestMarketplaceTaskDetailIDOR:
    """Verify that GET /v1/worker/tasks/{task_id} enforces access control."""

    # ── Helper: build a DB mock that returns different objects per call ────────

    def _db_with_sequence(self, *per_call_returns):
        """Return a DB mock whose .execute() yields different values per call."""
        db = _make_mock_db()
        call_counts = [0]

        def _side(stmt, *args, **kwargs):
            i = call_counts[0]
            call_counts[0] += 1
            val = per_call_returns[i] if i < len(per_call_returns) else None
            return _scalar_result(val)

        db.execute.side_effect = _side
        return db

    # ── 1. Requires authentication ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_requires_auth(self, app):
        """No token → 401."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/worker/tasks/{TASK_ID}")
        assert r.status_code == 401

    # ── 2. Non-workers are rejected ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_non_worker_gets_403(self, app, worker_auth):
        """A user with role='requester' is rejected with 403."""
        non_worker = _make_worker_user()
        non_worker.role = "requester"

        db = self._db_with_sequence(non_worker)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/v1/worker/tasks/{TASK_ID}", headers=worker_auth)
            assert r.status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)

    # ── 3. Open task is visible (normal marketplace browsing) ─────────────────

    @pytest.mark.asyncio
    async def test_open_task_is_visible(self, app, worker_auth):
        """Open marketplace task → 200 with task data."""
        worker = _make_worker_user()
        task   = _make_task(status="open")

        # DB call 1: user lookup → worker
        # DB call 2: task query → returns task (status == open, query matches)
        db = self._db_with_sequence(worker, task)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/v1/worker/tasks/{TASK_ID}", headers=worker_auth)
            assert r.status_code == 200
            body = r.json()
            assert body["id"] == TASK_ID
        finally:
            app.dependency_overrides.pop(get_db, None)

    # ── 4. Non-open task NOT accessible (IDOR protection) ────────────────────

    @pytest.mark.asyncio
    async def test_assigned_task_not_accessible_without_assignment(self, app, worker_auth):
        """An 'assigned' task belonging to another worker → 404 (IDOR protected).

        The DB mock simulates the fixed query returning None because the task
        is not open and this worker has no assignment on it.
        """
        worker = _make_worker_user()
        # task is assigned but DB returns None because our worker has no assignment
        db = self._db_with_sequence(worker, None)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/v1/worker/tasks/{TASK_ID}", headers=worker_auth)
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_completed_task_not_accessible_without_assignment(self, app, worker_auth):
        """A completed task from another requester → 404."""
        worker = _make_worker_user()
        db = self._db_with_sequence(worker, None)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/v1/worker/tasks/{TASK_ID}", headers=worker_auth)
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)

    # ── 5. Assigned task IS accessible with a valid assignment ────────────────

    @pytest.mark.asyncio
    async def test_assigned_task_accessible_with_own_assignment(self, app, worker_auth):
        """An 'assigned' task where this worker has an active assignment → 200.

        The DB mock simulates the fixed query returning the task because
        the worker's assignment_id appears in the subquery.
        """
        worker = _make_worker_user()
        task   = _make_task(status="assigned")  # task not open, but worker has it

        # DB call 2 returns the task (because the subquery found the assignment)
        db = self._db_with_sequence(worker, task)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/v1/worker/tasks/{TASK_ID}", headers=worker_auth)
            assert r.status_code == 200
            body = r.json()
            assert body["id"] == TASK_ID
        finally:
            app.dependency_overrides.pop(get_db, None)

    # ── 6. Nonexistent task → 404 ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_nonexistent_task_404(self, app, worker_auth):
        """Random UUID that doesn't exist in DB → 404."""
        worker = _make_worker_user()
        db = self._db_with_sequence(worker, None)
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            random_id = str(uuid.uuid4())
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/v1/worker/tasks/{random_id}", headers=worker_auth)
            assert r.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Trigger error-leakage protection tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestTriggerErrorLeakage:
    """Verify that trigger execution errors do NOT leak exception details."""

    @pytest.mark.asyncio
    async def test_webhook_trigger_error_does_not_leak_details(self, app):
        """When a webhook trigger fails, the response must NOT include the exception message."""
        from core.database import get_db

        # Build a DB that returns a trigger
        trigger = MagicMock()
        trigger.id             = uuid.uuid4()
        trigger.user_id        = uuid.UUID(REQUESTER_ID)
        trigger.trigger_type   = "webhook"
        trigger.webhook_token  = "test-token-xyz"
        trigger.is_active      = True
        trigger.pipeline_id    = uuid.uuid4()
        trigger.default_input  = {}

        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar_result(trigger))

        app.dependency_overrides[get_db] = _db_override(db)
        try:
            with patch(
                "routers.triggers._fire_trigger",
                AsyncMock(side_effect=RuntimeError("DB connection string: postgres://secret@host:5432/db")),
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    r = await c.post(
                        "/v1/pipelines/webhooks/test-token-xyz",
                        json={"key": "value"},
                    )
            assert r.status_code == 500
            body = r.json()
            detail = body.get("detail", "")
            # The raw exception message MUST NOT appear in the response
            assert "postgres://" not in detail, "DB connection string leaked in error response!"
            assert "secret" not in detail, "Secret credential leaked in error response!"
            # A generic message is fine
            assert "Pipeline execution failed" in detail or "execution failed" in detail.lower()
        finally:
            app.dependency_overrides.pop(get_db, None)
