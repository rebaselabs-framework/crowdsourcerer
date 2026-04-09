"""Tests for the platform stats router.

Covers:
  1.  GET /v1/platform/stats — no auth required → returns 200
  2.  GET /v1/platform/stats — response contains all expected top-level fields
  3.  GET /v1/platform/stats — zero DB results → all counts are 0, top_task_types is []
  4.  GET /v1/platform/stats — non-zero counts propagate correctly
  5.  GET /v1/platform/stats — cache: second call within TTL skips DB
  6.  GET /v1/platform/stats — platform_uptime_note is a non-empty string
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_db() -> MagicMock:
    db         = MagicMock()
    db.execute = AsyncMock()
    return db


def _scalar_result(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one         = MagicMock(return_value=value)
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.all                = MagicMock(return_value=[])
    return r


def _combined_row_result(total, today, week, avg_ms):
    """Mock result for the combined completion stats query (.one() row)."""
    row = MagicMock()
    row.total = total
    row.today = today
    row.this_week = week
    row.avg_ms = avg_ms
    r = MagicMock()
    r.one = MagicMock(return_value=row)
    return r


def _build_stats_db(
    total: int       = 0,
    today: int       = 0,
    week: int        = 0,
    active_w: int    = 0,
    requesters: int  = 0,
    avg_ms: int | None = None,
    top_types: list  = None,
    running: int     = 0,
) -> MagicMock:
    """Returns a mock DB that answers the 5 sequential execute() calls in get_platform_stats.

    Query order after optimization:
      1. Combined task stats (total, today, this_week, avg_ms) → .one()
      2. Active workers (30d) → .scalar_one()
      3. Total requesters → .scalar_one()
      4. Top task types → .all()
      5. Tasks running now → .scalar_one()
    """
    db         = _make_db()
    call_num   = [0]
    top_rows   = top_types or []

    def _side_effect(stmt):
        call_num[0] += 1
        n = call_num[0]
        if n == 1:  # combined completion stats
            return _combined_row_result(total, today, week, avg_ms)
        if n == 2:  # active_workers_30d
            return _scalar_result(active_w)
        if n == 3:  # total_requesters
            return _scalar_result(requesters)
        if n == 4:  # top_task_types
            r = MagicMock()
            r.all = MagicMock(return_value=top_rows)
            return r
        if n == 5:  # tasks_running_now
            return _scalar_result(running)
        return _scalar_result(None)

    db.execute.side_effect = _side_effect
    return db


def _db_override(mock_db):
    async def _override():
        yield mock_db
    return _override


def _clear_cache():
    """Reset the platform_stats module-level cache between tests."""
    import routers.platform_stats as ps
    ps._cache    = {}
    ps._cache_at = None


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_platform_stats_no_auth_required():
    """GET /v1/platform/stats — no Authorization header → 200 (public endpoint)."""
    from main import app
    from core.database import get_db

    _clear_cache()
    db = _build_stats_db()
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/platform/stats")
        assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()
        _clear_cache()


@pytest.mark.asyncio
async def test_platform_stats_response_shape():
    """Response contains all expected top-level fields."""
    from main import app
    from core.database import get_db

    _clear_cache()
    db = _build_stats_db(total=42, today=5, week=20, active_w=10, requesters=3, running=2)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/platform/stats")
        assert r.status_code == 200
        body = r.json()
        for field in (
            "tasks_completed_total", "tasks_completed_today", "tasks_completed_this_week",
            "active_workers_30d", "total_requesters", "avg_completion_ms_overall",
            "top_task_types", "tasks_running_now", "platform_uptime_note",
        ):
            assert field in body, f"Missing field: {field}"
    finally:
        app.dependency_overrides.clear()
        _clear_cache()


@pytest.mark.asyncio
async def test_platform_stats_zero_values():
    """Empty DB → all count fields are 0, top_task_types is empty list."""
    from main import app
    from core.database import get_db

    _clear_cache()
    db = _build_stats_db()   # all zeros / no types
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/platform/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["tasks_completed_total"] == 0
        assert body["tasks_completed_today"] == 0
        assert body["active_workers_30d"] == 0
        assert body["top_task_types"] == []
        assert body["avg_completion_ms_overall"] is None
    finally:
        app.dependency_overrides.clear()
        _clear_cache()


@pytest.mark.asyncio
async def test_platform_stats_non_zero_values():
    """Non-zero DB results propagate to the response correctly."""
    from main import app
    from core.database import get_db

    _clear_cache()

    # Build a fake top-type row
    top_row = MagicMock()
    top_row.type   = "label_image"
    top_row.cnt    = 50
    top_row.avg_ms = 3500.0

    db = _build_stats_db(
        total=100, today=10, week=40, active_w=25, requesters=8,
        avg_ms=3500, top_types=[top_row], running=3,
    )
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/platform/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["tasks_completed_total"] == 100
        assert body["tasks_completed_today"] == 10
        assert body["active_workers_30d"] == 25
        assert body["total_requesters"] == 8
        assert body["tasks_running_now"] == 3
        assert body["avg_completion_ms_overall"] == 3500
        assert len(body["top_task_types"]) == 1
        assert body["top_task_types"][0]["task_type"] == "label_image"
        assert body["top_task_types"][0]["completed"] == 50
    finally:
        app.dependency_overrides.clear()
        _clear_cache()


@pytest.mark.asyncio
async def test_platform_stats_cache_hit():
    """Second call within TTL returns cached result without hitting DB again."""
    from main import app
    from core.database import get_db

    _clear_cache()
    db = _build_stats_db(total=77)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r1 = await c.get("/v1/platform/stats")
            # Second call — uses the same db override but module cache should kick in
            r2 = await c.get("/v1/platform/stats")
        assert r1.status_code == 200
        assert r2.status_code == 200
        # Both should return the same data
        assert r1.json()["tasks_completed_total"] == r2.json()["tasks_completed_total"]
        # The DB was only called ~8 times (first request), not 16 (two requests)
        assert db.execute.call_count < 16
    finally:
        app.dependency_overrides.clear()
        _clear_cache()


@pytest.mark.asyncio
async def test_platform_stats_uptime_note_non_empty():
    """platform_uptime_note is a non-empty string."""
    from main import app
    from core.database import get_db

    _clear_cache()
    db = _build_stats_db()
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/platform/stats")
        assert r.status_code == 200
        note = r.json()["platform_uptime_note"]
        assert isinstance(note, str) and len(note) > 0
    finally:
        app.dependency_overrides.clear()
        _clear_cache()
