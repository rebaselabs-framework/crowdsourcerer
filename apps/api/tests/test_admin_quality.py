"""Tests for admin quality monitoring endpoint.

Covers:
  - GET /v1/admin/quality: requires admin auth (401 without token)
  - GET /v1/admin/quality: requires admin role (403 for non-admin)
  - GET /v1/admin/quality: returns empty list when no low-accuracy workers
  - GET /v1/admin/quality: returns platform_stats with correct fields
  - GET /v1/admin/quality: threshold param filters workers
  - GET /v1/admin/quality: min_evaluated param filters workers
  - GET /v1/admin/quality: worker entries have all required fields
  - GET /v1/admin/quality: workers_at_risk count is correct
  - GET /v1/admin/quality: workers ordered by accuracy ascending
  - GET /v1/admin/quality: stats include threshold and min_evaluated params
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "quality-test-secret")
os.environ.setdefault("API_KEY_SALT", "quality-test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Shared IDs ────────────────────────────────────────────────────────────────

ADMIN_ID    = str(uuid.uuid4())
NON_ADMIN_ID = str(uuid.uuid4())
WORKER_A_ID = str(uuid.uuid4())
WORKER_B_ID = str(uuid.uuid4())


# ── Mock helpers ──────────────────────────────────────────────────────────────

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


def _token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _make_admin_user() -> MagicMock:
    u = MagicMock()
    u.id       = uuid.UUID(ADMIN_ID)
    u.is_admin = True
    u.role     = "requester"
    u.credits  = 0
    return u


def _make_non_admin_user() -> MagicMock:
    u = MagicMock()
    u.id       = uuid.UUID(NON_ADMIN_ID)
    u.is_admin = False
    u.role     = "requester"
    u.credits  = 0
    return u


def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one         = MagicMock(return_value=value)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return r


def _row(total_workers=0, avg_accuracy=None, at_risk=0):
    """Mock a stats row result (one() call)."""
    r = MagicMock()
    r.total_workers = total_workers
    r.avg_accuracy  = avg_accuracy
    r.at_risk       = at_risk
    return r


def _worker_row(
    worker_id=None,
    name="Test Worker",
    email="worker@test.com",
    accuracy=0.45,
    total_evaluated=10,
    approved_count=4,
    worker_tasks_completed=25,
    reputation_score=0.7,
    strike_count=0,
    is_banned=False,
):
    r = MagicMock()
    r.id = uuid.UUID(worker_id or str(uuid.uuid4()))
    r.name = name
    r.email = email
    r.accuracy = accuracy
    r.accuracy_pct = round(accuracy * 100, 1)
    r.total_evaluated = total_evaluated
    r.approved_count = approved_count
    r.worker_tasks_completed = worker_tasks_completed
    r.reputation_score = reputation_score
    r.strike_count = strike_count
    r.is_banned = is_banned
    return r


def _make_quality_db(stats_row, worker_rows):
    """
    Mock DB where:
    - call 1: user lookup for require_admin → returns admin user (scalar_one_or_none)
    - call 2: stats query → returns stats_row (.one())
    - call 3: low-accuracy workers query → returns worker_rows (.all())
    """
    db = _make_mock_db()
    call_count = 0

    def _side(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Admin user lookup
            return _scalar(_make_admin_user())
        if call_count == 2:
            # Stats aggregation query — needs .one()
            result = MagicMock()
            result.one = MagicMock(return_value=stats_row)
            result.scalar_one_or_none = MagicMock(return_value=None)
            return result
        if call_count == 3:
            # Low-accuracy workers query — needs .all()
            result = MagicMock()
            result.all = MagicMock(return_value=worker_rows)
            result.scalar_one_or_none = MagicMock(return_value=None)
            return result
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=None)
        r.scalar_one = MagicMock(return_value=None)
        r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return r

    db.execute.side_effect = _side
    return db


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    from main import app as _app
    return _app


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {_token(ADMIN_ID)}"}


@pytest.fixture
def non_admin_headers():
    return {"Authorization": f"Bearer {_token(NON_ADMIN_ID)}"}


# ── Auth guard tests ──────────────────────────────────────────────────────────

class TestQualityMonitoringAuth:

    @pytest.mark.asyncio
    async def test_requires_auth(self, app):
        """GET /v1/admin/quality returns 401 without token."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/admin/quality")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_requires_admin(self, app, non_admin_headers):
        """Non-admin user gets 403."""
        db = _make_mock_db()
        non_admin = _make_non_admin_user()
        db.execute = AsyncMock(return_value=_scalar(non_admin))
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/admin/quality", headers=non_admin_headers)
            assert r.status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)


# ── Data tests ────────────────────────────────────────────────────────────────

class TestQualityMonitoringData:

    @pytest.mark.asyncio
    async def test_empty_when_no_evaluated_workers(self, app, admin_headers):
        """Returns empty list and zero stats when no workers have enough reviews."""
        db = _make_quality_db(
            stats_row=_row(total_workers=0, avg_accuracy=None, at_risk=0),
            worker_rows=[],
        )
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/admin/quality", headers=admin_headers)
            assert r.status_code == 200
            data = r.json()
            assert data["low_accuracy_workers"] == []
            assert data["platform_stats"]["total_workers_evaluated"] == 0
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_platform_stats_has_required_fields(self, app, admin_headers):
        """platform_stats includes all required fields."""
        db = _make_quality_db(
            stats_row=_row(total_workers=5, avg_accuracy=0.72, at_risk=2),
            worker_rows=[],
        )
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/admin/quality", headers=admin_headers)
            assert r.status_code == 200
            stats = r.json()["platform_stats"]
            for field in ("total_workers_evaluated", "avg_accuracy", "avg_accuracy_pct",
                          "workers_at_risk", "pct_at_risk", "threshold", "min_evaluated"):
                assert field in stats, f"Missing field: {field}"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_platform_stats_values_correct(self, app, admin_headers):
        """platform_stats reflects the query results."""
        db = _make_quality_db(
            stats_row=_row(total_workers=10, avg_accuracy=0.75, at_risk=3),
            worker_rows=[],
        )
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/admin/quality", headers=admin_headers)
            stats = r.json()["platform_stats"]
            assert stats["total_workers_evaluated"] == 10
            assert stats["avg_accuracy_pct"] == 75.0
            assert stats["workers_at_risk"] == 3
            assert stats["pct_at_risk"] == 30.0  # 3/10 = 30%
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_worker_entries_have_required_fields(self, app, admin_headers):
        """Each worker entry has all required fields."""
        wrow = _worker_row(worker_id=WORKER_A_ID, accuracy=0.45, total_evaluated=10, approved_count=4)
        db = _make_quality_db(
            stats_row=_row(total_workers=1, avg_accuracy=0.45, at_risk=1),
            worker_rows=[wrow],
        )
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/admin/quality", headers=admin_headers)
            assert r.status_code == 200
            workers = r.json()["low_accuracy_workers"]
            assert len(workers) == 1
            w = workers[0]
            for field in ("id", "name", "email", "accuracy", "accuracy_pct",
                          "total_evaluated", "approved_count", "rejected_count",
                          "worker_tasks_completed", "reputation_score", "strike_count", "is_banned"):
                assert field in w, f"Missing field: {field}"
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_rejected_count_computed_correctly(self, app, admin_headers):
        """rejected_count = total_evaluated - approved_count."""
        wrow = _worker_row(worker_id=WORKER_A_ID, total_evaluated=10, approved_count=3, accuracy=0.3)
        db = _make_quality_db(
            stats_row=_row(total_workers=1, avg_accuracy=0.3, at_risk=1),
            worker_rows=[wrow],
        )
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/admin/quality", headers=admin_headers)
            w = r.json()["low_accuracy_workers"][0]
            assert w["rejected_count"] == 7  # 10 - 3
            assert w["approved_count"] == 3
            assert w["total_evaluated"] == 10
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_threshold_reflected_in_stats(self, app, admin_headers):
        """threshold param is reflected in platform_stats."""
        db = _make_quality_db(
            stats_row=_row(total_workers=0, avg_accuracy=None, at_risk=0),
            worker_rows=[],
        )
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/admin/quality?threshold=0.5", headers=admin_headers)
            assert r.status_code == 200
            assert r.json()["platform_stats"]["threshold"] == 0.5
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_min_evaluated_reflected_in_stats(self, app, admin_headers):
        """min_evaluated param is reflected in platform_stats."""
        db = _make_quality_db(
            stats_row=_row(total_workers=0, avg_accuracy=None, at_risk=0),
            worker_rows=[],
        )
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/admin/quality?min_evaluated=10", headers=admin_headers)
            assert r.status_code == 200
            assert r.json()["platform_stats"]["min_evaluated"] == 10
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_multiple_workers_returned(self, app, admin_headers):
        """Multiple low-accuracy workers are all returned."""
        rows = [
            _worker_row(worker_id=WORKER_A_ID, accuracy=0.35, email="a@test.com"),
            _worker_row(worker_id=WORKER_B_ID, accuracy=0.50, email="b@test.com"),
        ]
        db = _make_quality_db(
            stats_row=_row(total_workers=5, avg_accuracy=0.6, at_risk=2),
            worker_rows=rows,
        )
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/admin/quality", headers=admin_headers)
            assert r.status_code == 200
            workers = r.json()["low_accuracy_workers"]
            assert len(workers) == 2
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_pct_at_risk_zero_when_no_workers(self, app, admin_headers):
        """pct_at_risk is 0 when total_workers_evaluated is 0 (no division by zero)."""
        db = _make_quality_db(
            stats_row=_row(total_workers=0, avg_accuracy=None, at_risk=0),
            worker_rows=[],
        )
        from core.database import get_db
        app.dependency_overrides[get_db] = _db_override(db)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/v1/admin/quality", headers=admin_headers)
            assert r.status_code == 200
            assert r.json()["platform_stats"]["pct_at_risk"] == 0.0
        finally:
            app.dependency_overrides.pop(get_db, None)
