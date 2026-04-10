"""Tests for admin endpoints (everything except /v1/admin/quality).

Covers 26 admin endpoints with auth, happy-path, not-found, and
invalid-state tests.  Total: 65+ tests.
"""

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "admin-test-secret")
os.environ.setdefault("API_KEY_SALT", "admin-test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Shared IDs ───────────────────────────────────────────────────────────────

ADMIN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
WORKER_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
STRIKE_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
ALERT_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
NOW = datetime.now(timezone.utc)


# ── Mock helpers ─────────────────────────────────────────────────────────────

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
        pass
    db.refresh = _refresh
    async def _delete(obj):
        pass
    db.delete = _delete
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _make_admin_user():
    u = MagicMock()
    u.id = uuid.UUID(ADMIN_ID)
    u.is_admin = True
    u.role = "requester"
    u.credits = 0
    u.token_version = 0
    return u


def _scalar(value):
    """Mock a result from db.execute() that supports scalar_one_or_none/scalar_one/scalars."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value)
    r.scalar = MagicMock(return_value=value if not isinstance(value, MagicMock) else 0)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all = MagicMock(return_value=[])
    return r


def _result_scalar(val):
    """Mock a result from db.execute() where .scalar() returns val (for count queries)."""
    r = MagicMock()
    r.scalar = MagicMock(return_value=val)
    r.scalar_one_or_none = MagicMock(return_value=val)
    r.scalar_one = MagicMock(return_value=val)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all = MagicMock(return_value=[])
    return r


def _result_row(**fields):
    """Mock a result from db.execute() where .one() returns a row with named fields.

    Used for consolidated aggregate queries (e.g. SELECT count() as total, ...).
    """
    row = MagicMock()
    for k, v in fields.items():
        setattr(row, k, v)
    r = MagicMock()
    r.one = MagicMock(return_value=row)
    r.scalar = MagicMock(return_value=fields.get(list(fields.keys())[0]) if fields else 0)
    r.all = MagicMock(return_value=[])
    return r


def _make_user_obj(**overrides):
    """Create a mock user object with sensible defaults."""
    u = MagicMock()
    u.id = uuid.UUID(overrides.get("id", USER_ID))
    u.email = overrides.get("email", "user@test.com")
    u.name = overrides.get("name", "Test User")
    u.plan = overrides.get("plan", "free")
    u.role = overrides.get("role", "requester")
    u.credits = overrides.get("credits", 100)
    u.is_active = overrides.get("is_active", True)
    u.is_admin = overrides.get("is_admin", False)
    u.is_banned = overrides.get("is_banned", False)
    u.ban_reason = overrides.get("ban_reason", None)
    u.ban_expires_at = overrides.get("ban_expires_at", None)
    u.strike_count = overrides.get("strike_count", 0)
    u.reputation_score = overrides.get("reputation_score", 1.0)
    u.worker_tasks_completed = overrides.get("worker_tasks_completed", 0)
    u.worker_level = overrides.get("worker_level", 1)
    u.worker_xp = overrides.get("worker_xp", 0)
    u.worker_accuracy = overrides.get("worker_accuracy", 0.0)
    u.worker_reliability = overrides.get("worker_reliability", 0.0)
    u.worker_streak_days = overrides.get("worker_streak_days", 0)
    u.availability_status = overrides.get("availability_status", "available")
    u.token_version = overrides.get("token_version", 0)
    u.created_at = overrides.get("created_at", NOW)
    return u


def _make_worker_obj(**overrides):
    """Worker mock with worker-specific defaults."""
    defaults = {
        "id": WORKER_ID,
        "role": "worker",
        "email": "worker@test.com",
        "name": "Test Worker",
    }
    defaults.update(overrides)
    return _make_user_obj(**defaults)


def _make_strike_obj(**overrides):
    s = MagicMock()
    s.id = uuid.UUID(overrides.get("id", STRIKE_ID))
    s.worker_id = uuid.UUID(overrides.get("worker_id", WORKER_ID))
    s.issued_by = uuid.UUID(overrides.get("issued_by", ADMIN_ID))
    s.severity = overrides.get("severity", "minor")
    s.reason = overrides.get("reason", "Test reason")
    s.is_active = overrides.get("is_active", True)
    s.expires_at = overrides.get("expires_at", None)
    s.created_at = overrides.get("created_at", NOW)
    return s


def _make_alert_obj(**overrides):
    a = MagicMock()
    a.id = uuid.UUID(overrides.get("id", ALERT_ID))
    a.alert_type = overrides.get("alert_type", "high_error_rate")
    a.severity = overrides.get("severity", "warning")
    a.title = overrides.get("title", "Test Alert")
    a.detail = overrides.get("detail", {"info": "test"})
    a.resolved_at = overrides.get("resolved_at", None)
    a.notified_at = overrides.get("notified_at", None)
    a.created_at = overrides.get("created_at", NOW)
    return a


def _admin_then(*subsequent_returns):
    """Build a side_effect for db.execute that handles admin auth first,
    then returns the provided mock results in order."""
    call_count = 0
    results = list(subsequent_returns)

    async def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar(_make_admin_user())
        idx = call_count - 2
        if idx < len(results):
            return results[idx]
        return _result_scalar(0)

    return _side


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    from main import app as _app
    return _app


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {_token(ADMIN_ID)}"}


# ── Helper to run requests ────────────────────────────────────────────────────

async def _get(app, path, headers, db, **params):
    from core.database import get_db
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.get(path, headers=headers, params=params)
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _post(app, path, headers, db, json=None):
    from core.database import get_db
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.post(path, headers=headers, json=json)
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _patch(app, path, headers, db, json=None):
    from core.database import get_db
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.patch(path, headers=headers, json=json)
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _delete(app, path, headers, db, **params):
    from core.database import get_db
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.delete(path, headers=headers, params=params)
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _noauth(app, method, path):
    """Request without auth token — expect 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        if method == "GET":
            return await c.get(path)
        elif method == "POST":
            return await c.post(path)
        elif method == "PATCH":
            return await c.patch(path)
        elif method == "DELETE":
            return await c.delete(path)


# =============================================================================
# 1. GET /v1/admin/stats
# =============================================================================

class TestAdminStats:

    @pytest.mark.asyncio
    async def test_stats_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/stats")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_stats_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        # Stats endpoint uses consolidated queries (5 db.execute + 1 db.scalar)
        db.execute = AsyncMock(side_effect=_admin_then(
            # 1: user stats (consolidated row)
            _result_row(total=0, active=0, workers=0, new_week=0, credits_sum=0),
            # 2: task stats (consolidated row)
            _result_row(total=0, completed=0, failed=0, running=0, open_human=0, this_week=0),
            # 3: task type breakdown (returns .all() → [])
            _result_scalar(0),
            # 4: assignments (consolidated row)
            _result_row(total=0, submitted=0),
            # 5: webhooks (consolidated row)
            _result_row(total=0, failed=0),
        ))
        db.scalar = AsyncMock(return_value=0)  # credits_purchased
        r = await _get(app, "/v1/admin/stats", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert "users" in data
        assert "tasks" in data
        assert "credits" in data
        assert "webhooks" in data
        assert "generated_at" in data
        assert data["users"]["total"] == 0
        assert data["tasks"]["success_rate"] == 0

    @pytest.mark.asyncio
    async def test_stats_success_rate_calculation(self, app, admin_headers):
        """When there are tasks, success_rate is computed correctly."""
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then(
            # 1: user stats
            _result_row(total=10, active=8, workers=3, new_week=2, credits_sum=500),
            # 2: task stats (100 total, 80 completed → success_rate = 80.0)
            _result_row(total=100, completed=80, failed=10, running=5, open_human=3, this_week=20),
            # 3: task type breakdown
            _result_scalar(0),
            # 4: assignments
            _result_row(total=50, submitted=40),
            # 5: webhooks
            _result_row(total=100, failed=5),
        ))
        db.scalar = AsyncMock(return_value=1000)  # credits_purchased
        r = await _get(app, "/v1/admin/stats", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["tasks"]["success_rate"] == 80.0
        assert data["users"]["total"] == 10
        assert data["webhooks"]["success_rate"] == 95.0


# =============================================================================
# 2. GET /v1/admin/users
# =============================================================================

class TestAdminListUsers:

    @pytest.mark.asyncio
    async def test_list_users_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/users")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_list_users_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        user_mock = _make_user_obj()

        call_count = 0
        async def _side(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(_make_admin_user())
            if call_count == 2:
                # Total count query
                return _result_scalar(1)
            if call_count == 3:
                # Users list query
                r = MagicMock()
                r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[user_mock])))
                return r
            return _result_scalar(0)

        db.execute = AsyncMock(side_effect=_side)
        r = await _get(app, "/v1/admin/users", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["email"] == "user@test.com"

    @pytest.mark.asyncio
    async def test_list_users_pagination(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then(
            _result_scalar(50),
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
        ))
        r = await _get(app, "/v1/admin/users", admin_headers, db, page=2, page_size=10)
        assert r.status_code == 200
        data = r.json()
        assert data["page"] == 2
        assert data["page_size"] == 10
        assert data["total"] == 50
        assert data["has_next"] is True

    @pytest.mark.asyncio
    async def test_list_users_invalid_role(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then())
        r = await _get(app, "/v1/admin/users", admin_headers, db, role="invalid_role")
        assert r.status_code == 422


# =============================================================================
# 3. GET /v1/admin/users/{user_id}
# =============================================================================

class TestAdminGetUser:

    @pytest.mark.asyncio
    async def test_get_user_requires_auth(self, app):
        r = await _noauth(app, "GET", f"/v1/admin/users/{USER_ID}")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_get_user_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        user_mock = _make_user_obj()

        db.execute = AsyncMock(side_effect=_admin_then(
            _scalar(user_mock),   # user lookup
            MagicMock(all=MagicMock(return_value=[])),  # task_counts
        ))
        r = await _get(app, f"/v1/admin/users/{USER_ID}", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == USER_ID
        assert data["email"] == "user@test.com"
        assert "task_stats" in data

    @pytest.mark.asyncio
    async def test_get_user_not_found(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then(
            _scalar(None),  # user not found
        ))
        r = await _get(app, f"/v1/admin/users/{USER_ID}", admin_headers, db)
        assert r.status_code == 404


# =============================================================================
# 4. PATCH /v1/admin/users/{user_id}
# =============================================================================

class TestAdminUpdateUser:

    @pytest.mark.asyncio
    async def test_update_user_requires_auth(self, app):
        r = await _noauth(app, "PATCH", f"/v1/admin/users/{USER_ID}")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_update_user_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        user_mock = _make_user_obj()

        db.execute = AsyncMock(side_effect=_admin_then(
            _scalar(user_mock),
        ))
        r = await _patch(app, f"/v1/admin/users/{USER_ID}", admin_headers, db,
                         json={"plan": "pro", "is_active": True})
        assert r.status_code == 200
        data = r.json()
        assert data["updated"] is True
        assert "plan" in data["changes"]

    @pytest.mark.asyncio
    async def test_update_user_not_found(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then(
            _scalar(None),
        ))
        r = await _patch(app, f"/v1/admin/users/{USER_ID}", admin_headers, db,
                         json={"plan": "pro"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_update_user_no_fields(self, app, admin_headers):
        db = _make_mock_db()
        user_mock = _make_user_obj()
        db.execute = AsyncMock(side_effect=_admin_then(
            _scalar(user_mock),
        ))
        r = await _patch(app, f"/v1/admin/users/{USER_ID}", admin_headers, db, json={})
        assert r.status_code == 200
        data = r.json()
        assert data["updated"] is False

    @pytest.mark.asyncio
    async def test_update_user_invalid_plan(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then())
        r = await _patch(app, f"/v1/admin/users/{USER_ID}", admin_headers, db,
                         json={"plan": "platinum"})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_update_user_credits_too_high(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then())
        r = await _patch(app, f"/v1/admin/users/{USER_ID}", admin_headers, db,
                         json={"credits": 2_000_000})
        assert r.status_code == 422


# =============================================================================
# 5. GET /v1/admin/tasks
# =============================================================================

class TestAdminListTasks:

    @pytest.mark.asyncio
    async def test_list_tasks_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/tasks")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_list_tasks_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        task_mock = MagicMock()
        task_mock.id = uuid.UUID(USER_ID)
        task_mock.user_id = uuid.UUID(ADMIN_ID)
        task_mock.type = "web_research"
        task_mock.status = "completed"
        task_mock.priority = "normal"
        task_mock.execution_mode = "ai"
        task_mock.credits_used = 10
        task_mock.created_at = NOW

        db.execute = AsyncMock(side_effect=_admin_then(
            _result_scalar(1),
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[task_mock])))),
        ))
        r = await _get(app, "/v1/admin/tasks", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert data["total"] == 1
        assert data["items"][0]["type"] == "web_research"

    @pytest.mark.asyncio
    async def test_list_tasks_invalid_status(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then())
        r = await _get(app, "/v1/admin/tasks", admin_headers, db, status="nonexistent")
        assert r.status_code == 422


# =============================================================================
# 6. POST /v1/admin/sweep
# =============================================================================

class TestAdminSweep:

    @pytest.mark.asyncio
    async def test_sweep_requires_auth(self, app):
        r = await _noauth(app, "POST", "/v1/admin/sweep")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_sweep_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))

        with patch("routers.admin.sweep_once", new_callable=AsyncMock) as mock_sweep, \
             patch("routers.admin._sweep_scheduled_tasks", new_callable=AsyncMock) as mock_sched:
            mock_sweep.return_value = {"expired": 0, "reopened": 0}
            mock_sched.return_value = 0
            from core.database import get_db
            app.dependency_overrides[get_db] = _db_override(db)
            try:
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    r = await c.post("/v1/admin/sweep", headers=admin_headers)
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "summary" in data


# =============================================================================
# 7. GET /v1/admin/analytics
# =============================================================================

class TestAdminAnalytics:

    @pytest.mark.asyncio
    async def test_analytics_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/analytics")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_analytics_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        # Analytics does many db.execute calls that return .all() with empty results
        db.execute = AsyncMock(side_effect=_admin_then(
            # daily_tasks, daily_signups, daily_credits, daily_completions
            *[MagicMock(all=MagicMock(return_value=[])) for _ in range(4)],
            # top_workers
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
            # hourly
            MagicMock(all=MagicMock(return_value=[])),
            # payouts
            MagicMock(all=MagicMock(return_value=[])),
        ))
        r = await _get(app, "/v1/admin/analytics", admin_headers, db, days=7)
        assert r.status_code == 200
        data = r.json()
        assert data["days"] == 7
        assert "all_days" in data
        assert len(data["all_days"]) == 7
        assert "daily_tasks" in data
        assert "daily_signups" in data
        assert "hourly_tasks_today" in data
        assert len(data["hourly_tasks_today"]) == 24
        assert "top_workers" in data
        assert "payout_summary" in data


# =============================================================================
# 8. GET /v1/admin/sweeper/status
# =============================================================================

class TestAdminSweeperStatus:

    @pytest.mark.asyncio
    async def test_sweeper_status_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/sweeper/status")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_sweeper_status_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.scalar = AsyncMock(return_value=0)

        with patch("routers.admin.get_sweeper_task") as mock_task:
            mock_task.return_value = None
            r = await _get(app, "/v1/admin/sweeper/status", admin_headers, db)

        assert r.status_code == 200
        data = r.json()
        assert data["sweeper_running"] is False
        assert data["expired_pending_sweep"] == 0
        assert data["timed_out_last_24h"] == 0
        assert "checked_at" in data

    @pytest.mark.asyncio
    async def test_sweeper_status_running(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.scalar = AsyncMock(return_value=3)

        mock_task_obj = MagicMock()
        mock_task_obj.done = MagicMock(return_value=False)

        with patch("routers.admin.get_sweeper_task") as mock_task:
            mock_task.return_value = mock_task_obj
            r = await _get(app, "/v1/admin/sweeper/status", admin_headers, db)

        assert r.status_code == 200
        assert r.json()["sweeper_running"] is True


# =============================================================================
# 9. GET /v1/admin/matching/stats
# =============================================================================

class TestAdminMatchingStats:

    @pytest.mark.asyncio
    async def test_matching_stats_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/matching/stats")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_matching_stats_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then(
            MagicMock(all=MagicMock(return_value=[])),  # proficiency rows
        ))
        db.scalar = AsyncMock(return_value=0)

        r = await _get(app, "/v1/admin/matching/stats", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert "total_skill_profiles" in data
        assert "workers_with_skills" in data
        assert "gated_tasks" in data
        assert "proficiency_by_type" in data


# =============================================================================
# 10. GET /v1/admin/queue
# =============================================================================

class TestAdminQueue:

    @pytest.mark.asyncio
    async def test_queue_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/queue")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_queue_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then(
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),  # tasks
            MagicMock(all=MagicMock(return_value=[])),  # completed_rows
        ))
        r = await _get(app, "/v1/admin/queue", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["queue_size"] == 0
        assert "by_priority" in data
        assert len(data["by_priority"]) == 4
        assert "eta_model" in data

    @pytest.mark.asyncio
    async def test_queue_with_tasks(self, app, admin_headers):
        db = _make_mock_db()
        task_mock = MagicMock()
        task_mock.id = uuid.UUID(USER_ID)
        task_mock.type = "web_research"
        task_mock.status = "queued"
        task_mock.execution_mode = "ai"
        task_mock.priority = "normal"
        task_mock.created_at = NOW
        task_mock.duration_ms = None

        db.execute = AsyncMock(side_effect=_admin_then(
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[task_mock])))),
            MagicMock(all=MagicMock(return_value=[])),
        ))
        r = await _get(app, "/v1/admin/queue", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["queue_size"] == 1


# =============================================================================
# 11. GET /v1/admin/billing/analytics
# =============================================================================

class TestAdminBillingAnalytics:

    @pytest.mark.asyncio
    async def test_billing_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/billing/analytics")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_billing_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then(
            # plan_distribution
            MagicMock(all=MagicMock(return_value=[])),
            # monthly_credits
            MagicMock(all=MagicMock(return_value=[])),
            # monthly_charges
            MagicMock(all=MagicMock(return_value=[])),
            # monthly_new_paid
            MagicMock(all=MagicMock(return_value=[])),
            # top_spenders
            MagicMock(all=MagicMock(return_value=[])),
        ))
        db.scalar = AsyncMock(return_value=0)

        r = await _get(app, "/v1/admin/billing/analytics", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert "mrr_usd" in data
        assert "plan_distribution" in data
        assert "monthly_data" in data
        assert "top_spenders" in data
        assert "total_credits_purchased" in data
        assert "generated_at" in data


# =============================================================================
# 12. GET /v1/admin/workers
# =============================================================================

class TestAdminListWorkers:

    @pytest.mark.asyncio
    async def test_list_workers_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/workers")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_list_workers_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        worker = _make_worker_obj()

        db.execute = AsyncMock(side_effect=_admin_then(
            _result_scalar(1),  # total count
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[worker])))),  # workers
            MagicMock(all=MagicMock(return_value=[])),  # strikes bulk load
        ))
        r = await _get(app, "/v1/admin/workers", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["id"] == WORKER_ID
        assert data["items"][0]["role"] == "worker"

    @pytest.mark.asyncio
    async def test_list_workers_empty(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then(
            _result_scalar(0),
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
        ))
        r = await _get(app, "/v1/admin/workers", admin_headers, db)
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert r.json()["items"] == []


# =============================================================================
# 13. POST /v1/admin/workers/{id}/ban
# =============================================================================

class TestAdminBanWorker:

    @pytest.mark.asyncio
    async def test_ban_requires_auth(self, app):
        r = await _noauth(app, "POST", f"/v1/admin/workers/{WORKER_ID}/ban")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_ban_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        worker = _make_worker_obj()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=worker)

        r = await _post(app, f"/v1/admin/workers/{WORKER_ID}/ban", admin_headers, db,
                        json={"reason": "Spam submissions"})
        assert r.status_code == 200
        data = r.json()
        assert data["banned"] is True
        assert data["worker_id"] == WORKER_ID

    @pytest.mark.asyncio
    async def test_ban_not_found(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=None)

        r = await _post(app, f"/v1/admin/workers/{WORKER_ID}/ban", admin_headers, db,
                        json={"reason": "Spam"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_ban_not_a_worker(self, app, admin_headers):
        db = _make_mock_db()
        non_worker = _make_user_obj(role="requester")
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=non_worker)

        r = await _post(app, f"/v1/admin/workers/{WORKER_ID}/ban", admin_headers, db,
                        json={"reason": "Spam"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_ban_missing_reason(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))

        r = await _post(app, f"/v1/admin/workers/{WORKER_ID}/ban", admin_headers, db,
                        json={})
        assert r.status_code == 422


# =============================================================================
# 14. DELETE /v1/admin/workers/{id}/ban
# =============================================================================

class TestAdminUnbanWorker:

    @pytest.mark.asyncio
    async def test_unban_requires_auth(self, app):
        r = await _noauth(app, "DELETE", f"/v1/admin/workers/{WORKER_ID}/ban")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_unban_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        worker = _make_worker_obj(is_banned=True, ban_reason="Previous offense")
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=worker)

        r = await _delete(app, f"/v1/admin/workers/{WORKER_ID}/ban", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["unbanned"] is True
        assert data["worker_id"] == WORKER_ID

    @pytest.mark.asyncio
    async def test_unban_not_found(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=None)

        r = await _delete(app, f"/v1/admin/workers/{WORKER_ID}/ban", admin_headers, db)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_unban_not_banned(self, app, admin_headers):
        db = _make_mock_db()
        worker = _make_worker_obj(is_banned=False)
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=worker)

        r = await _delete(app, f"/v1/admin/workers/{WORKER_ID}/ban", admin_headers, db)
        assert r.status_code == 400


# =============================================================================
# 15. GET /v1/admin/workers/{id}/strikes
# =============================================================================

class TestAdminListStrikes:

    @pytest.mark.asyncio
    async def test_list_strikes_requires_auth(self, app):
        r = await _noauth(app, "GET", f"/v1/admin/workers/{WORKER_ID}/strikes")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_list_strikes_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        worker = _make_worker_obj()
        strike = _make_strike_obj()

        db.execute = AsyncMock(side_effect=_admin_then(
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[strike])))),
        ))
        db.get = AsyncMock(return_value=worker)

        r = await _get(app, f"/v1/admin/workers/{WORKER_ID}/strikes", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == STRIKE_ID
        assert data[0]["severity"] == "minor"

    @pytest.mark.asyncio
    async def test_list_strikes_worker_not_found(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=None)

        r = await _get(app, f"/v1/admin/workers/{WORKER_ID}/strikes", admin_headers, db)
        assert r.status_code == 404


# =============================================================================
# 16. POST /v1/admin/workers/{id}/strikes
# =============================================================================

class TestAdminAddStrike:

    @pytest.mark.asyncio
    async def test_add_strike_requires_auth(self, app):
        r = await _noauth(app, "POST", f"/v1/admin/workers/{WORKER_ID}/strikes")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_add_strike_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        worker = _make_worker_obj(strike_count=0)
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=worker)

        r = await _post(app, f"/v1/admin/workers/{WORKER_ID}/strikes", admin_headers, db,
                        json={"reason": "Low quality work", "severity": "major"})
        assert r.status_code == 201
        data = r.json()
        assert "strike_id" in data
        assert data["total_strikes"] == 1

    @pytest.mark.asyncio
    async def test_add_strike_worker_not_found(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=None)

        r = await _post(app, f"/v1/admin/workers/{WORKER_ID}/strikes", admin_headers, db,
                        json={"reason": "Bad behavior", "severity": "minor"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_add_strike_not_a_worker(self, app, admin_headers):
        db = _make_mock_db()
        non_worker = _make_user_obj(role="requester")
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=non_worker)

        r = await _post(app, f"/v1/admin/workers/{WORKER_ID}/strikes", admin_headers, db,
                        json={"reason": "Bad behavior", "severity": "minor"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_add_strike_missing_reason(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))

        r = await _post(app, f"/v1/admin/workers/{WORKER_ID}/strikes", admin_headers, db,
                        json={"severity": "minor"})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_add_strike_invalid_severity(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))

        r = await _post(app, f"/v1/admin/workers/{WORKER_ID}/strikes", admin_headers, db,
                        json={"reason": "Bad work", "severity": "nuclear"})
        assert r.status_code == 422


# =============================================================================
# 17. DELETE /v1/admin/workers/{id}/strikes/{strike_id}
# =============================================================================

class TestAdminPardonStrike:

    @pytest.mark.asyncio
    async def test_pardon_requires_auth(self, app):
        r = await _noauth(app, "DELETE", f"/v1/admin/workers/{WORKER_ID}/strikes/{STRIKE_ID}")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_pardon_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        strike = _make_strike_obj(is_active=True)
        worker = _make_worker_obj(strike_count=2)

        call_count = 0
        async def _get_side(model, pk):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return strike  # first get -> strike
            return worker     # second get -> worker

        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(side_effect=_get_side)

        r = await _delete(app, f"/v1/admin/workers/{WORKER_ID}/strikes/{STRIKE_ID}",
                          admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["pardoned"] is True
        assert data["strike_id"] == STRIKE_ID

    @pytest.mark.asyncio
    async def test_pardon_strike_not_found(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=None)

        r = await _delete(app, f"/v1/admin/workers/{WORKER_ID}/strikes/{STRIKE_ID}",
                          admin_headers, db)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_pardon_already_pardoned(self, app, admin_headers):
        db = _make_mock_db()
        strike = _make_strike_obj(is_active=False)
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=strike)

        r = await _delete(app, f"/v1/admin/workers/{WORKER_ID}/strikes/{STRIKE_ID}",
                          admin_headers, db)
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_pardon_wrong_worker_id(self, app, admin_headers):
        db = _make_mock_db()
        # Strike belongs to a different worker
        other_worker_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        strike = _make_strike_obj(worker_id=other_worker_id)
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=strike)

        r = await _delete(app, f"/v1/admin/workers/{WORKER_ID}/strikes/{STRIKE_ID}",
                          admin_headers, db)
        assert r.status_code == 404


# =============================================================================
# 18. GET /v1/admin/health
# =============================================================================

class TestAdminHealth:

    @pytest.mark.asyncio
    async def test_health_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/health")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_health_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        # Build a named-tuple-like mock for the consolidated task stats query
        _task_row = MagicMock()
        _task_row.pending = 0
        _task_row.running = 0
        _task_row.open_tasks = 0
        _task_row.created_1h = 0
        _task_row.completed_1h = 0
        _task_row.failed_1h = 0
        _task_stats_result = MagicMock()
        _task_stats_result.one = MagicMock(return_value=_task_row)

        db.execute = AsyncMock(side_effect=_admin_then(
            # db_ping query
            _result_scalar(1),
            # consolidated task stats (.one())
            _task_stats_result,
            # failing_types
            MagicMock(all=MagicMock(return_value=[])),
            # stuck_ai
            MagicMock(all=MagicMock(return_value=[])),
            # stuck_human
            MagicMock(all=MagicMock(return_value=[])),
        ))
        db.scalar = AsyncMock(return_value=0)

        # The health endpoint re-imports _LAST_SWEEP_AT from core.sweeper at call time
        with patch("core.sweeper._LAST_SWEEP_AT", NOW):
            r = await _get(app, "/v1/admin/health", admin_headers, db)

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "healthy"
        assert "db_ping_ms" in data
        assert "task_queue" in data
        assert "stuck_tasks" in data
        assert "error_rate_1h" in data
        assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_health_degraded_when_sweeper_stale(self, app, admin_headers):
        db = _make_mock_db()
        _task_row = MagicMock()
        _task_row.pending = 0
        _task_row.running = 0
        _task_row.open_tasks = 0
        _task_row.created_1h = 0
        _task_row.completed_1h = 0
        _task_row.failed_1h = 0
        _task_stats_result = MagicMock()
        _task_stats_result.one = MagicMock(return_value=_task_row)

        db.execute = AsyncMock(side_effect=_admin_then(
            _result_scalar(1),
            _task_stats_result,
            MagicMock(all=MagicMock(return_value=[])),
            MagicMock(all=MagicMock(return_value=[])),
            MagicMock(all=MagicMock(return_value=[])),
        ))
        db.scalar = AsyncMock(return_value=0)

        # Sweeper has never run
        with patch("core.sweeper._LAST_SWEEP_AT", None):
            r = await _get(app, "/v1/admin/health", admin_headers, db)

        assert r.status_code == 200
        # With sweeper_ago=None, status should be "degraded"
        assert r.json()["status"] == "degraded"


# =============================================================================
# 19. GET /v1/admin/audit-log
# =============================================================================

class TestAdminAuditLog:

    @pytest.mark.asyncio
    async def test_audit_log_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/audit-log")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_audit_log_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then(
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
            # admin name resolution (no admin ids)
        ))
        db.scalar = AsyncMock(return_value=0)

        r = await _get(app, "/v1/admin/audit-log", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "entries" in data
        assert data["total"] == 0
        assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_audit_log_with_entries(self, app, admin_headers):
        db = _make_mock_db()
        entry = MagicMock()
        entry.id = uuid.UUID(STRIKE_ID)
        entry.admin_id = uuid.UUID(ADMIN_ID)
        entry.action = "ban_worker"
        entry.target_type = "user"
        entry.target_id = WORKER_ID
        entry.detail = {"reason": "spam"}
        entry.ip_address = "127.0.0.1"
        entry.created_at = NOW

        admin_name_row = MagicMock()
        admin_name_row.id = uuid.UUID(ADMIN_ID)
        admin_name_row.name = "Admin"
        admin_name_row.email = "admin@test.com"

        db.scalar = AsyncMock(return_value=1)
        db.execute = AsyncMock(side_effect=_admin_then(
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[entry])))),
            MagicMock(__iter__=MagicMock(return_value=iter([admin_name_row]))),
        ))

        r = await _get(app, "/v1/admin/audit-log", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert len(data["entries"]) == 1
        assert data["entries"][0]["action"] == "ban_worker"


# =============================================================================
# 20. GET /v1/admin/alerts
# =============================================================================

class TestAdminAlerts:

    @pytest.mark.asyncio
    async def test_alerts_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/alerts")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_alerts_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(side_effect=_admin_then(
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
        ))
        db.scalar = AsyncMock(return_value=0)

        r = await _get(app, "/v1/admin/alerts", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "alerts" in data
        assert data["alerts"] == []

    @pytest.mark.asyncio
    async def test_alerts_with_data(self, app, admin_headers):
        db = _make_mock_db()
        alert = _make_alert_obj()

        db.scalar = AsyncMock(return_value=1)
        db.execute = AsyncMock(side_effect=_admin_then(
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[alert])))),
        ))

        r = await _get(app, "/v1/admin/alerts", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert len(data["alerts"]) == 1
        assert data["alerts"][0]["id"] == ALERT_ID
        assert data["alerts"][0]["severity"] == "warning"


# =============================================================================
# 21. POST /v1/admin/alerts/{id}/resolve
# =============================================================================

class TestAdminResolveAlert:

    @pytest.mark.asyncio
    async def test_resolve_requires_auth(self, app):
        r = await _noauth(app, "POST", f"/v1/admin/alerts/{ALERT_ID}/resolve")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_resolve_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        alert = _make_alert_obj(resolved_at=None)
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=alert)

        r = await _post(app, f"/v1/admin/alerts/{ALERT_ID}/resolve", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["alert_id"] == ALERT_ID

    @pytest.mark.asyncio
    async def test_resolve_not_found(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=None)

        r = await _post(app, f"/v1/admin/alerts/{ALERT_ID}/resolve", admin_headers, db)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_resolve_already_resolved(self, app, admin_headers):
        db = _make_mock_db()
        alert = _make_alert_obj(resolved_at=NOW)
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))
        db.get = AsyncMock(return_value=alert)

        r = await _post(app, f"/v1/admin/alerts/{ALERT_ID}/resolve", admin_headers, db)
        assert r.status_code == 409


# =============================================================================
# 22. GET /v1/admin/cache/stats
# =============================================================================

class TestAdminCacheStats:

    @pytest.mark.asyncio
    async def test_cache_stats_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/cache/stats")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_cache_stats_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))

        mock_stats = {
            "total_entries": 42,
            "total_hits": 100,
            "estimated_credits_saved": 500,
        }
        with patch("routers.admin.cache_stats", new_callable=AsyncMock) as mock_fn:
            mock_fn.return_value = mock_stats
            r = await _get(app, "/v1/admin/cache/stats", admin_headers, db)

        assert r.status_code == 200
        data = r.json()
        assert data["total_entries"] == 42
        assert data["total_hits"] == 100


# =============================================================================
# 23. DELETE /v1/admin/cache/flush
# =============================================================================

class TestAdminCacheFlush:

    @pytest.mark.asyncio
    async def test_cache_flush_requires_auth(self, app):
        r = await _noauth(app, "DELETE", "/v1/admin/cache/flush")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_cache_flush_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))

        with patch("routers.admin.cache_flush", new_callable=AsyncMock) as mock_fn:
            mock_fn.return_value = 10
            r = await _delete(app, "/v1/admin/cache/flush", admin_headers, db)

        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["deleted"] == 10

    @pytest.mark.asyncio
    async def test_cache_flush_expired_only(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))

        with patch("routers.admin.cache_flush", new_callable=AsyncMock) as mock_fn:
            mock_fn.return_value = 3
            r = await _delete(app, "/v1/admin/cache/flush", admin_headers, db,
                              expired_only="true")

        assert r.status_code == 200
        assert r.json()["deleted"] == 3


# =============================================================================
# 24. GET /v1/admin/onboarding/funnel
# =============================================================================

class TestAdminOnboardingFunnel:

    @pytest.mark.asyncio
    async def test_onboarding_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/onboarding/funnel")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_onboarding_happy_path(self, app, admin_headers):
        db = _make_mock_db()

        # Stats row from conditional aggregation
        stats_row = MagicMock()
        stats_row.started = 50
        stats_row.step_welcome = 45
        stats_row.step_create_task = 30
        stats_row.step_view_results = 20
        stats_row.step_set_webhook = 10
        stats_row.step_invite_team = 5
        stats_row.completed = 3

        db.execute = AsyncMock(side_effect=_admin_then(
            _result_scalar(100),  # total_requesters (scalar_one)
            MagicMock(one=MagicMock(return_value=stats_row)),  # onboarding stats
        ))

        r = await _get(app, "/v1/admin/onboarding/funnel", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["total_requesters"] == 100
        assert "funnel" in data
        assert "drop_off" in data
        assert data["completion_rate"] == 0.03  # 3/100

    @pytest.mark.asyncio
    async def test_onboarding_zero_requesters(self, app, admin_headers):
        db = _make_mock_db()

        stats_row = MagicMock()
        stats_row.started = 0
        stats_row.step_welcome = 0
        stats_row.step_create_task = 0
        stats_row.step_view_results = 0
        stats_row.step_set_webhook = 0
        stats_row.step_invite_team = 0
        stats_row.completed = 0

        db.execute = AsyncMock(side_effect=_admin_then(
            _result_scalar(0),
            MagicMock(one=MagicMock(return_value=stats_row)),
        ))

        r = await _get(app, "/v1/admin/onboarding/funnel", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["total_requesters"] == 0
        assert data["completion_rate"] == 0.0


# =============================================================================
# 25. GET /v1/admin/worker-onboarding/funnel
# =============================================================================

class TestAdminWorkerOnboardingFunnel:

    @pytest.mark.asyncio
    async def test_worker_onboarding_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/worker-onboarding/funnel")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_worker_onboarding_happy_path(self, app, admin_headers):
        db = _make_mock_db()

        stats_row = MagicMock()
        stats_row.started = 30
        stats_row.step_profile = 25
        stats_row.step_explore = 20
        stats_row.step_first_task = 15
        stats_row.step_skills = 10
        stats_row.step_cert = 5
        stats_row.completed = 3
        stats_row.skipped = 2
        stats_row.bonus_claimed = 1

        db.execute = AsyncMock(side_effect=_admin_then(
            _result_scalar(50),  # total workers (scalar_one)
            MagicMock(one=MagicMock(return_value=stats_row)),  # onboarding stats
        ))

        r = await _get(app, "/v1/admin/worker-onboarding/funnel", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["total_workers"] == 50
        assert data["completion_rate"] == 0.06  # 3/50
        assert data["skipped"] == 2
        assert data["bonus_claimed"] == 1
        assert "funnel" in data
        assert "drop_off" in data

    @pytest.mark.asyncio
    async def test_worker_onboarding_zero_workers(self, app, admin_headers):
        db = _make_mock_db()

        stats_row = MagicMock()
        stats_row.started = 0
        stats_row.step_profile = 0
        stats_row.step_explore = 0
        stats_row.step_first_task = 0
        stats_row.step_skills = 0
        stats_row.step_cert = 0
        stats_row.completed = 0
        stats_row.skipped = 0
        stats_row.bonus_claimed = 0

        db.execute = AsyncMock(side_effect=_admin_then(
            _result_scalar(0),
            MagicMock(one=MagicMock(return_value=stats_row)),
        ))

        r = await _get(app, "/v1/admin/worker-onboarding/funnel", admin_headers, db)
        assert r.status_code == 200
        data = r.json()
        assert data["total_workers"] == 0
        assert data["completion_rate"] == 0.0


# =============================================================================
# 26. GET /v1/admin/config/status
# =============================================================================

class TestAdminConfigStatus:

    @pytest.mark.asyncio
    async def test_config_requires_auth(self, app):
        r = await _noauth(app, "GET", "/v1/admin/config/status")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_config_happy_path(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))

        mock_settings = MagicMock()
        mock_settings.database_url = "postgresql+asyncpg://user:pass@db:5432/test"
        mock_settings.jwt_secret = "real-secret"
        mock_settings.api_key_salt = "real-salt"
        mock_settings.rebasekit_api_key = "rk-key"
        mock_settings.rebasekit_base_url = "https://api.rebaselabs.online"
        mock_settings.email_enabled = False
        mock_settings.smtp_host = ""
        mock_settings.smtp_port = 587
        mock_settings.stripe_secret_key = ""
        mock_settings.google_client_id = ""
        mock_settings.task_result_cache_enabled = True
        mock_settings.jwt_expire_minutes = 30
        mock_settings.refresh_token_expire_days = 30

        with patch("core.config.get_settings", return_value=mock_settings):
            r = await _get(app, "/v1/admin/config/status", admin_headers, db)

        assert r.status_code == 200
        data = r.json()
        assert "ready" in data
        assert "checks" in data
        assert "summary" in data
        assert data["checks"]["database"]["configured"] is True
        assert data["checks"]["jwt_secret"]["configured"] is True

    @pytest.mark.asyncio
    async def test_config_detects_default_secrets(self, app, admin_headers):
        db = _make_mock_db()
        db.execute = AsyncMock(return_value=_scalar(_make_admin_user()))

        mock_settings = MagicMock()
        mock_settings.database_url = "postgresql+asyncpg://user:pass@db:5432/test"
        mock_settings.jwt_secret = "change-me-in-production"
        mock_settings.api_key_salt = "change-me-in-production"
        mock_settings.rebasekit_api_key = ""
        mock_settings.rebasekit_base_url = "https://api.rebaselabs.online"
        mock_settings.email_enabled = False
        mock_settings.smtp_host = ""
        mock_settings.smtp_port = 587
        mock_settings.stripe_secret_key = ""
        mock_settings.google_client_id = ""
        mock_settings.task_result_cache_enabled = False
        mock_settings.jwt_expire_minutes = 30
        mock_settings.refresh_token_expire_days = 30

        with patch("core.config.get_settings", return_value=mock_settings):
            r = await _get(app, "/v1/admin/config/status", admin_headers, db)

        assert r.status_code == 200
        data = r.json()
        assert data["checks"]["jwt_secret"]["configured"] is False
        assert data["checks"]["rebasekit"]["configured"] is False
        assert data["ready"] is False


# =============================================================================
# Cross-cutting: all GET endpoints return 401 without token
# =============================================================================

class TestAllEndpointsRequireAuth:
    """Verify every admin GET endpoint rejects unauthenticated requests."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", [
        "/v1/admin/stats",
        "/v1/admin/users",
        f"/v1/admin/users/{USER_ID}",
        "/v1/admin/tasks",
        "/v1/admin/analytics",
        "/v1/admin/sweeper/status",
        "/v1/admin/matching/stats",
        "/v1/admin/queue",
        "/v1/admin/billing/analytics",
        "/v1/admin/workers",
        "/v1/admin/health",
        "/v1/admin/audit-log",
        "/v1/admin/alerts",
        "/v1/admin/cache/stats",
        "/v1/admin/onboarding/funnel",
        "/v1/admin/worker-onboarding/funnel",
        "/v1/admin/config/status",
    ])
    async def test_get_endpoints_require_auth(self, app, path):
        r = await _noauth(app, "GET", path)
        assert r.status_code == 401, f"{path} should return 401, got {r.status_code}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", [
        "/v1/admin/sweep",
        f"/v1/admin/alerts/{ALERT_ID}/resolve",
    ])
    async def test_post_endpoints_require_auth(self, app, path):
        r = await _noauth(app, "POST", path)
        assert r.status_code in (401, 422), f"{path} should return 401 or 422, got {r.status_code}"

    @pytest.mark.asyncio
    async def test_delete_cache_flush_requires_auth(self, app):
        r = await _noauth(app, "DELETE", "/v1/admin/cache/flush")
        assert r.status_code == 401
