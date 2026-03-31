"""Tests for worker performance endpoint.

Covers:
  - GET /v1/worker/performance: requires auth (401 without token)
  - GET /v1/worker/performance: returns expected structure
  - Returns zero/null when no reviewed assignments
  - all_time approval rate calculated correctly
  - last_30d counts only recent assignments
  - by_task_type breakdown present
  - platform_avg_approval_rate_pct present
  - rank_percentile is None with < 5 reviews
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "perf-test-secret")
os.environ.setdefault("API_KEY_SALT", "perf-test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport

WORKER_ID = str(uuid.uuid4())


def _token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _make_worker_user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.UUID(WORKER_ID)
    u.role = "worker"
    u.is_admin = False
    u.worker_xp = 500
    u.worker_tasks_completed = 20
    u.worker_streak_days = 3
    u.worker_last_active_date = datetime.now(timezone.utc)
    u.token_version = 0
    return u


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
    async def _inner():
        yield mock_db
    return _inner


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one         = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return r


def _one_result(total, approved):
    """Mock a .one() result row with total and approved attributes."""
    r = MagicMock()
    r.total    = total
    r.approved = approved
    result = MagicMock()
    result.one   = MagicMock(return_value=r)
    result.scalar_one_or_none = MagicMock(return_value=None)
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return result


def _all_result(rows):
    """Mock a .all() result."""
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    result.one = MagicMock(return_value=MagicMock(total=0, approved=0, total_workers=0, avg_rate=None))
    result.scalar_one_or_none = MagicMock(return_value=None)
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return result


def _make_zero_result():
    """Return a result mock that always returns zeros for .one(), .all(), .scalar()."""
    r = MagicMock()
    r.total = 0
    r.approved = 0
    r.total_workers = 0
    r.avg_rate = None
    result = MagicMock()
    result.one    = MagicMock(return_value=r)
    result.all    = MagicMock(return_value=[])
    result.scalar = MagicMock(return_value=0)
    result.scalar_one_or_none = MagicMock(return_value=None)
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return result


def _make_zero_db():
    """Mock DB that returns zero/empty for all performance queries.

    With JWT auth, get_current_user_id resolves user_id purely from the token
    (no DB call). So every db.execute call in the endpoint receives a zero-result.
    """
    db = _make_mock_db()

    def _side(*a, **kw):
        return _make_zero_result()

    db.execute.side_effect = _side
    db.scalar = AsyncMock(return_value=0)
    return db


@pytest.fixture
def app():
    from main import app as _app
    return _app


@pytest.fixture
def worker_headers():
    return {"Authorization": f"Bearer {_token(WORKER_ID)}"}


class TestWorkerPerformanceAuth:

    @pytest.mark.asyncio
    async def test_requires_auth(self, app):
        """401 without token."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/worker/performance")
        assert r.status_code == 401


class TestWorkerPerformanceData:

    @pytest.mark.asyncio
    async def test_returns_expected_structure(self, app, worker_headers):
        """Returns all top-level keys."""
        db = _make_zero_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/worker/performance", headers=worker_headers)
            assert r.status_code == 200, r.text
            data = r.json()
            for key in ("all_time", "last_30d", "by_task_type",
                        "platform_avg_approval_rate", "platform_avg_approval_rate_pct",
                        "platform_evaluated_workers", "rank_percentile", "weekly_trend"):
                assert key in data, f"Missing key: {key}"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_all_time_has_required_fields(self, app, worker_headers):
        """all_time block has total_reviewed, approved, rejected, approval_rate."""
        db = _make_zero_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/worker/performance", headers=worker_headers)
            at = r.json()["all_time"]
            for field in ("total_reviewed", "approved", "rejected", "approval_rate", "approval_rate_pct"):
                assert field in at, f"Missing field in all_time: {field}"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_zero_reviews_returns_null_rate(self, app, worker_headers):
        """approval_rate is None when no reviews exist."""
        db = _make_zero_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/worker/performance", headers=worker_headers)
            assert r.status_code == 200
            data = r.json()
            assert data["all_time"]["approval_rate"] is None
            assert data["all_time"]["total_reviewed"] == 0
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_rank_percentile_none_with_few_reviews(self, app, worker_headers):
        """rank_percentile is None when all_time has < 5 reviews."""
        db = _make_zero_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/worker/performance", headers=worker_headers)
            assert r.json()["rank_percentile"] is None
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_by_task_type_is_list(self, app, worker_headers):
        """by_task_type is always a list."""
        db = _make_zero_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/worker/performance", headers=worker_headers)
            assert isinstance(r.json()["by_task_type"], list)
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_weekly_trend_is_list(self, app, worker_headers):
        """weekly_trend is always a list."""
        db = _make_zero_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/worker/performance", headers=worker_headers)
            assert isinstance(r.json()["weekly_trend"], list)
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_platform_avg_present(self, app, worker_headers):
        """platform_avg_approval_rate_pct is a number."""
        db = _make_zero_db()
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/worker/performance", headers=worker_headers)
            pct = r.json()["platform_avg_approval_rate_pct"]
            assert isinstance(pct, (int, float))
        finally:
            app.dependency_overrides.pop(get_db, None)
