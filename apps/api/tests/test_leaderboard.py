"""Tests for the leaderboard router.

Covers:
  1.  _entry — pure function: correct rank, fields from UserDB
  2.  GET /v1/leaderboard — unauthenticated: returns 200, caller_id is None
  3.  GET /v1/leaderboard — authenticated: caller_id is set in response
  4.  GET /v1/leaderboard — default (xp, all_time): entries sorted by XP
  5.  GET /v1/leaderboard?category=tasks — all-time tasks ordering
  6.  GET /v1/leaderboard?category=earnings — all-time earnings falls back to tasks ordering
  7.  GET /v1/leaderboard — empty DB → entries list is empty
  8.  GET /v1/leaderboard?period=weekly&category=tasks — weekly tasks path
  9.  GET /v1/leaderboard?period=weekly&category=earnings — weekly earnings path
  10. GET /v1/leaderboard — invalid category → 422 validation error
  11. GET /v1/leaderboard — multiple entries have sequential ranks 1, 2, 3…
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── IDs ───────────────────────────────────────────────────────────────────────

CALLER_ID = str(uuid.uuid4())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(
    user_id: str | None = None,
    name: str = "Alice",
    worker_xp: int = 100,
    worker_tasks_completed: int = 10,
    worker_level: int = 3,
    worker_accuracy: float | None = 0.95,
    worker_reliability: float | None = 0.90,
    worker_streak_days: int = 5,
    profile_public: bool = True,
    role: str = "worker",
) -> MagicMock:
    u = MagicMock()
    u.id                      = uuid.UUID(user_id) if user_id else uuid.uuid4()
    u.name                    = name
    u.worker_xp               = worker_xp
    u.worker_tasks_completed  = worker_tasks_completed
    u.worker_level            = worker_level
    u.worker_accuracy         = worker_accuracy
    u.worker_reliability      = worker_reliability
    u.worker_streak_days      = worker_streak_days
    u.profile_public          = profile_public
    u.role                    = role
    return u


def _make_db() -> MagicMock:
    db         = MagicMock()
    db.execute = AsyncMock()
    return db


def _scalars_result(users: list) -> MagicMock:
    """Mock db.execute return value that supports .scalars().all()."""
    r = MagicMock()
    r.scalars = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=users))
    )
    r.all = MagicMock(return_value=[(u, 0) for u in users])
    return r


def _rows_result(rows: list) -> MagicMock:
    """Mock db.execute return value for weekly queries: .all() → [(UserDB, value), ...]."""
    r = MagicMock()
    r.all = MagicMock(return_value=rows)
    r.scalars = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=[row[0] for row in rows]))
    )
    return r


def _db_override(mock_db):
    async def _override():
        yield mock_db
    return _override


def _real_token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


# ── Unit tests — _entry ────────────────────────────────────────────────────────

def test_entry_rank_and_fields():
    """_entry(0, user) → rank=1, correct fields copied from user."""
    from routers.leaderboard import _entry
    u = _make_user(name="Bob", worker_xp=500, worker_level=7)
    entry = _entry(0, u)
    assert entry.rank == 1
    assert entry.name == "Bob"
    assert entry.worker_xp == 500
    assert entry.worker_level == 7


def test_entry_rank_sequential():
    """_entry uses i+1 as rank (0-indexed input → 1-indexed rank)."""
    from routers.leaderboard import _entry
    u = _make_user()
    assert _entry(0, u).rank == 1
    assert _entry(4, u).rank == 5
    assert _entry(49, u).rank == 50


# ── HTTP endpoint tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_leaderboard_unauthenticated_200():
    """No token → still returns 200 (public endpoint), caller_id is None."""
    from main import app
    from core.database import get_db

    users = [_make_user(name="Worker1", worker_xp=200)]
    db = _make_db()
    db.execute.return_value = _scalars_result(users)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/leaderboard")
        assert r.status_code == 200
        body = r.json()
        assert body["caller_id"] is None
        assert body["period"] == "all_time"
        assert body["category"] == "xp"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_leaderboard_authenticated_caller_id():
    """With token → caller_id is set to the authenticated user's ID string."""
    from main import app
    from core.database import get_db

    users = [_make_user(name="Me", worker_xp=1000)]
    db = _make_db()
    db.execute.return_value = _scalars_result(users)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/leaderboard",
                headers={"Authorization": f"Bearer {_real_token(CALLER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["caller_id"] == CALLER_ID
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_leaderboard_default_xp_all_time():
    """Default params → category=xp, period=all_time, entries in response."""
    from main import app
    from core.database import get_db

    users = [
        _make_user(name="Alpha", worker_xp=500),
        _make_user(name="Beta",  worker_xp=300),
    ]
    db = _make_db()
    db.execute.return_value = _scalars_result(users)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/leaderboard")
        assert r.status_code == 200
        body = r.json()
        assert len(body["entries"]) == 2
        assert body["entries"][0]["rank"] == 1
        assert body["entries"][0]["name"] == "Alpha"
        assert body["entries"][1]["rank"] == 2
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_leaderboard_tasks_all_time():
    """category=tasks, all_time → returns entries."""
    from main import app
    from core.database import get_db

    users = [_make_user(name="Taskmaster", worker_tasks_completed=99)]
    db = _make_db()
    db.execute.return_value = _scalars_result(users)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/leaderboard?category=tasks")
        assert r.status_code == 200
        body = r.json()
        assert body["category"] == "tasks"
        assert len(body["entries"]) == 1
        assert body["entries"][0]["worker_tasks_completed"] == 99
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_leaderboard_earnings_all_time_fallback():
    """category=earnings, all_time falls through to tasks ordering — still returns 200."""
    from main import app
    from core.database import get_db

    users = [_make_user(name="Earner", worker_tasks_completed=50)]
    db = _make_db()
    db.execute.return_value = _scalars_result(users)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/leaderboard?category=earnings")
        assert r.status_code == 200
        body = r.json()
        assert body["category"] == "earnings"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_leaderboard_empty_db():
    """No workers in DB → entries is an empty list, still 200."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalars_result([])
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/leaderboard")
        assert r.status_code == 200
        assert r.json()["entries"] == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_leaderboard_weekly_tasks():
    """period=weekly, category=tasks uses subquery join path."""
    from main import app
    from core.database import get_db

    user = _make_user(name="WeeklyWorker", worker_tasks_completed=7)
    db   = _make_db()
    # Weekly path: result.all() returns [(user, count)] tuples
    db.execute.return_value = _rows_result([(user, 7)])
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/leaderboard?period=weekly&category=tasks")
        assert r.status_code == 200
        body = r.json()
        assert body["period"] == "weekly"
        assert body["category"] == "tasks"
        assert len(body["entries"]) == 1
        assert body["entries"][0]["name"] == "WeeklyWorker"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_leaderboard_weekly_earnings():
    """period=weekly, category=earnings uses earnings subquery path."""
    from main import app
    from core.database import get_db

    user = _make_user(name="TopEarner", worker_tasks_completed=3)
    db   = _make_db()
    db.execute.return_value = _rows_result([(user, 250)])
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/leaderboard?period=weekly&category=earnings")
        assert r.status_code == 200
        body = r.json()
        assert body["period"] == "weekly"
        assert body["category"] == "earnings"
        assert body["entries"][0]["name"] == "TopEarner"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_leaderboard_invalid_category_422():
    """Entirely unknown category value → 422 Unprocessable Entity."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/leaderboard?category=invalid_xyz")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_leaderboard_invalid_period_422():
    """Unknown period value → 422."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/leaderboard?period=monthly")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_leaderboard_entries_have_required_fields():
    """Each entry in the response contains all required fields."""
    from main import app
    from core.database import get_db

    users = [_make_user(name="FieldChecker", worker_xp=42)]
    db = _make_db()
    db.execute.return_value = _scalars_result(users)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/leaderboard")
        assert r.status_code == 200
        entry = r.json()["entries"][0]
        for field in ("rank", "user_id", "name", "worker_xp", "worker_tasks_completed",
                      "worker_level", "worker_streak_days", "profile_public"):
            assert field in entry, f"Missing field: {field}"
    finally:
        app.dependency_overrides.clear()
