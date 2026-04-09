"""Tests for api_key_usage and ratings routers.

API Key Usage:
  1.  GET /v1/api-keys/usage/overview — no auth → 401
  2.  GET /v1/api-keys/usage/overview — no keys → zeros/empty
  3.  GET /v1/api-keys/usage/overview — keys with usage data → totals + per-key
  4.  GET /v1/api-keys/usage/overview — custom days param respected
  5.  GET /v1/api-keys/usage/overview — invalid days (0) → 422
  6.  GET /v1/api-keys/usage/overview — invalid days (999) → 422
  7.  GET /v1/api-keys/{key_id}/usage — no auth → 401
  8.  GET /v1/api-keys/{key_id}/usage — key not found → 404
  9.  GET /v1/api-keys/{key_id}/usage — happy path → detail with daily + endpoints
  10. GET /v1/api-keys/{key_id}/usage — invalid days (0) → 422
  11. log_api_key_usage() — happy path: creates log + updates key counters
  12. log_api_key_usage() — key not found: still commits the log row
  13. log_api_key_usage() — exception rolls back

Ratings:
  14. POST /v1/tasks/{task_id}/rate — no auth → 401
  15. POST /v1/tasks/{task_id}/rate — task not found → 404
  16. POST /v1/tasks/{task_id}/rate — non-owner → 403
  17. POST /v1/tasks/{task_id}/rate — task not completed → 400
  18. POST /v1/tasks/{task_id}/rate — duplicate rating → 409
  19. POST /v1/tasks/{task_id}/rate — no worker assignment → 400
  20. POST /v1/tasks/{task_id}/rate — happy path → 201 + RatingOut
  21. POST /v1/tasks/{task_id}/rate — score below 1 → 422
  22. POST /v1/tasks/{task_id}/rate — score above 5 → 422
  23. GET /v1/tasks/{task_id}/rating — no auth → 401
  24. GET /v1/tasks/{task_id}/rating — task not found → 404
  25. GET /v1/tasks/{task_id}/rating — non-owner → 403
  26. GET /v1/tasks/{task_id}/rating — no rating exists → returns null
  27. GET /v1/tasks/{task_id}/rating — happy path → RatingOut
  28. GET /v1/workers/me/ratings — no auth → 401
  29. GET /v1/workers/me/ratings — happy path empty → zeros + empty recent
  30. GET /v1/workers/me/ratings — happy path with ratings → distribution + recent
  31. GET /v1/workers/{worker_id}/ratings — worker not found → 404
  32. GET /v1/workers/{worker_id}/ratings — private profile → 404
  33. GET /v1/workers/{worker_id}/ratings — happy path → summary
  34. GET /v1/workers/{worker_id}/ratings — empty ratings → zeros
"""

import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport

# ── Fixed IDs ───────────────────────────────────────────────────────────────

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WORKER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TASK_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
KEY_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
OTHER_USER = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
NOW = datetime.now(timezone.utc)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _token(user_id: str = USER_ID) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _auth(user_id: str = USER_ID) -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


def _make_db():
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
        if getattr(obj, "created_at", None) is None:
            obj.created_at = NOW
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
    db.refresh = _refresh
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value if value is not None else 0)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.one = MagicMock(return_value=value)
    r.one_or_none = MagicMock(return_value=value)
    r.all = MagicMock(return_value=[])
    r.fetchall = MagicMock(return_value=[])
    return r


def _scalars_result(items):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    r.all = MagicMock(return_value=items)
    r.fetchall = MagicMock(return_value=[(i,) for i in items])
    return r


# ── Mock factories ──────────────────────────────────────────────────────────

def _make_api_key(user_id=USER_ID, key_id=KEY_ID):
    k = MagicMock()
    k.id = uuid.UUID(key_id)
    k.user_id = uuid.UUID(user_id)
    k.name = "Test Key"
    k.key_prefix = "csk_test"
    k.last_used_at = NOW
    k.request_count = 0
    k.total_credits_used = 0
    return k


def _make_task(owner_id=USER_ID, status="completed"):
    t = MagicMock()
    t.id = uuid.UUID(TASK_ID)
    t.user_id = uuid.UUID(owner_id)
    t.status = status
    return t


def _make_assignment(worker_id=WORKER_ID):
    a = MagicMock()
    a.id = uuid.uuid4()
    a.task_id = uuid.UUID(TASK_ID)
    a.worker_id = uuid.UUID(worker_id)
    a.status = "approved"
    a.submitted_at = NOW
    return a


def _make_rating(score=5):
    r = MagicMock()
    r.id = uuid.uuid4()
    r.task_id = uuid.UUID(TASK_ID)
    r.requester_id = uuid.UUID(USER_ID)
    r.worker_id = uuid.UUID(WORKER_ID)
    r.submission_id = None
    r.score = score
    r.comment = "Great work"
    r.created_at = NOW
    return r


def _make_worker(worker_id=WORKER_ID, public=True):
    w = MagicMock()
    w.id = uuid.UUID(worker_id)
    w.profile_public = public
    w.avg_feedback_score = 4.5
    w.total_ratings_received = 10
    return w


# ═══════════════════════════════════════════════════════════════════════════
#  API KEY USAGE — GET /v1/api-keys/usage/overview
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_usage_overview_no_auth():
    """GET /v1/api-keys/usage/overview without auth → 401."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/api-keys/usage/overview")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_usage_overview_no_keys():
    """No API keys for user → returns zeros and empty keys list."""
    from main import app
    from core.database import get_db

    db = _make_db()
    # Query 1: select keys → empty list
    keys_result = _scalars_result([])
    db.execute = AsyncMock(return_value=keys_result)

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/api-keys/usage/overview", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["total_requests"] == 0
        assert body["total_errors"] == 0
        assert body["total_credits_used"] == 0
        assert body["keys"] == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_usage_overview_with_data():
    """Keys with usage data → returns totals and per-key breakdown."""
    from main import app
    from core.database import get_db

    db = _make_db()
    key1 = _make_api_key()
    key2_id = "dddddddd-dddd-dddd-dddd-ddddddddddde"
    key2 = _make_api_key(key_id=key2_id)
    key2.name = "Second Key"
    key2.key_prefix = "csk_sec"

    # Query 1: select keys
    keys_result = _scalars_result([key1, key2])

    # Query 2: total requests + credits
    totals_row = MagicMock()
    totals_row.total = 150
    totals_row.credits = 42
    totals_result = MagicMock()
    totals_result.one = MagicMock(return_value=totals_row)

    # Query 3: total errors
    errors_result = MagicMock()
    errors_result.scalar_one = MagicMock(return_value=5)

    # Query 4: per-key stats GROUP BY
    kstat1 = MagicMock()
    kstat1.api_key_id = key1.id
    kstat1.reqs = 100
    kstat1.credits = 30
    kstat1.errs = 3
    kstat2 = MagicMock()
    kstat2.api_key_id = key2.id
    kstat2.reqs = 50
    kstat2.credits = 12
    kstat2.errs = 2
    kstats_result = MagicMock()
    kstats_result.__iter__ = MagicMock(return_value=iter([kstat1, kstat2]))

    db.execute = AsyncMock(
        side_effect=[keys_result, totals_result, errors_result, kstats_result]
    )

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/api-keys/usage/overview", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["total_requests"] == 150
        assert body["total_errors"] == 5
        assert body["total_credits_used"] == 42
        assert len(body["keys"]) == 2
        assert body["keys"][0]["name"] == "Test Key"
        assert body["keys"][0]["requests"] == 100
        assert body["keys"][1]["name"] == "Second Key"
        assert body["keys"][1]["requests"] == 50
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_usage_overview_custom_days():
    """Custom days=7 parameter is accepted and produces 200."""
    from main import app
    from core.database import get_db

    db = _make_db()
    keys_result = _scalars_result([])
    db.execute = AsyncMock(return_value=keys_result)

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/api-keys/usage/overview?days=7", headers=_auth())
        assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_usage_overview_days_too_low():
    """days=0 violates ge=1 constraint → 422."""
    from main import app
    from core.database import get_db

    db = _make_db()
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/api-keys/usage/overview?days=0", headers=_auth())
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_usage_overview_days_too_high():
    """days=999 violates le=365 constraint → 422."""
    from main import app
    from core.database import get_db

    db = _make_db()
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/api-keys/usage/overview?days=999", headers=_auth())
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


# ═══════════════════════════════════════════════════════════════════════════
#  API KEY USAGE — GET /v1/api-keys/{key_id}/usage
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_key_usage_no_auth():
    """GET /v1/api-keys/{key_id}/usage without auth → 401."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/v1/api-keys/{KEY_ID}/usage")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_key_usage_not_found():
    """Key not found (or not owned by user) → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    # Query 1: ownership check → None
    db.execute = AsyncMock(return_value=_scalar(None))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/api-keys/{KEY_ID}/usage", headers=_auth())
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_key_usage_happy_path():
    """Per-key detail returns totals, daily breakdown, and top endpoints."""
    from main import app
    from core.database import get_db

    db = _make_db()
    key = _make_api_key()

    # Query 1: ownership check → key found
    key_result = _scalar(key)

    # Query 2: totals
    totals_row = MagicMock()
    totals_row.total = 200
    totals_row.credits = 50
    totals_row.avg_ms = 123.456
    totals_result = MagicMock()
    totals_result.one = MagicMock(return_value=totals_row)

    # Query 3: errors count
    errors_result = MagicMock()
    errors_result.scalar_one = MagicMock(return_value=8)

    # Query 4: daily breakdown
    day_row = MagicMock()
    day_row.day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    day_row.reqs = 50
    day_row.errs = 2
    day_row.credits = 10
    day_row.avg_ms = 99.5
    daily_result = MagicMock()
    daily_result.all = MagicMock(return_value=[day_row])

    # Query 5: top endpoints
    ep_row = MagicMock()
    ep_row.endpoint = "/v1/tasks"
    ep_row.method = "POST"
    ep_row.reqs = 30
    ep_row.avg_ms = 88.1
    endpoints_result = MagicMock()
    endpoints_result.all = MagicMock(return_value=[ep_row])

    db.execute = AsyncMock(
        side_effect=[key_result, totals_result, errors_result, daily_result, endpoints_result]
    )

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/api-keys/{KEY_ID}/usage", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["key_id"] == KEY_ID
        assert body["key_name"] == "Test Key"
        assert body["key_prefix"] == "csk_test"
        assert body["total_requests"] == 200
        assert body["total_errors"] == 8
        assert body["total_credits_used"] == 50
        assert len(body["daily"]) == 1
        assert body["daily"][0]["requests"] == 50
        assert len(body["top_endpoints"]) == 1
        assert body["top_endpoints"][0]["endpoint"] == "/v1/tasks"
        assert body["top_endpoints"][0]["method"] == "POST"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_key_usage_days_too_low():
    """days=0 for per-key endpoint violates ge=1 → 422."""
    from main import app
    from core.database import get_db

    db = _make_db()
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/api-keys/{KEY_ID}/usage?days=0", headers=_auth())
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


# ═══════════════════════════════════════════════════════════════════════════
#  API KEY USAGE — log_api_key_usage() internal helper
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_log_api_key_usage_happy():
    """log_api_key_usage creates a log row and updates key counters."""
    from routers.api_key_usage import log_api_key_usage

    db = _make_db()
    key = _make_api_key()
    key.request_count = 5
    key.total_credits_used = 10

    db.execute = AsyncMock(return_value=_scalar(key))

    await log_api_key_usage(
        db=db,
        api_key_id=uuid.UUID(KEY_ID),
        user_id=uuid.UUID(USER_ID),
        endpoint="/v1/tasks",
        method="POST",
        status_code=200,
        response_time_ms=50,
        credits_used=3,
    )

    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    assert key.request_count == 6
    assert key.total_credits_used == 13
    assert key.last_used_at is not None


@pytest.mark.asyncio
async def test_log_api_key_usage_key_missing():
    """When key is not found, the log row is still created and committed."""
    from routers.api_key_usage import log_api_key_usage

    db = _make_db()
    db.execute = AsyncMock(return_value=_scalar(None))

    await log_api_key_usage(
        db=db,
        api_key_id=uuid.UUID(KEY_ID),
        user_id=uuid.UUID(USER_ID),
        endpoint="/v1/tasks",
        method="GET",
        status_code=200,
        response_time_ms=30,
        credits_used=0,
    )

    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_log_api_key_usage_exception_rollback():
    """On exception the helper rolls back instead of crashing."""
    from routers.api_key_usage import log_api_key_usage

    db = _make_db()
    db.commit = AsyncMock(side_effect=RuntimeError("db down"))

    await log_api_key_usage(
        db=db,
        api_key_id=uuid.UUID(KEY_ID),
        user_id=uuid.UUID(USER_ID),
        endpoint="/v1/tasks",
        method="POST",
        status_code=200,
        response_time_ms=50,
        credits_used=0,
    )

    db.rollback.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
#  RATINGS — POST /v1/tasks/{task_id}/rate
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rate_task_no_auth():
    """POST /v1/tasks/{task_id}/rate without auth → 401."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(f"/v1/tasks/{TASK_ID}/rate", json={"score": 5})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_rate_task_not_found():
    """Task does not exist → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    # Query 1: find task → None
    db.execute = AsyncMock(return_value=_scalar(None))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/rate",
                json={"score": 5},
                headers=_auth(),
            )
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_rate_task_non_owner():
    """Non-owner user tries to rate → 403."""
    from main import app
    from core.database import get_db

    db = _make_db()
    task = _make_task(owner_id=OTHER_USER)
    db.execute = AsyncMock(return_value=_scalar(task))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/rate",
                json={"score": 5},
                headers=_auth(),
            )
        assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_rate_task_not_completed():
    """Task status != 'completed' → 400."""
    from main import app
    from core.database import get_db

    db = _make_db()
    task = _make_task(status="in_progress")
    db.execute = AsyncMock(return_value=_scalar(task))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/rate",
                json={"score": 4},
                headers=_auth(),
            )
        assert r.status_code == 400
        assert "completed" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_rate_task_duplicate():
    """Duplicate rating for same task → 409."""
    from main import app
    from core.database import get_db

    db = _make_db()
    task = _make_task()
    existing_rating = _make_rating()

    # Query 1: find task → exists
    # Query 2: check existing rating → found
    db.execute = AsyncMock(side_effect=[_scalar(task), _scalar(existing_rating)])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/rate",
                json={"score": 5},
                headers=_auth(),
            )
        assert r.status_code == 409
        assert "already rated" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_rate_task_no_worker():
    """No approved/submitted assignment → 400."""
    from main import app
    from core.database import get_db

    db = _make_db()
    task = _make_task()

    # Query 1: find task
    # Query 2: check existing rating → None
    # Query 3: find assignment → None
    db.execute = AsyncMock(
        side_effect=[_scalar(task), _scalar(None), _scalar(None)]
    )

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/rate",
                json={"score": 3},
                headers=_auth(),
            )
        assert r.status_code == 400
        assert "no worker" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("routers.ratings.create_notification", new_callable=AsyncMock)
async def test_rate_task_happy_path(mock_notify):
    """Happy path: creates rating, refreshes worker avg, returns 201."""
    from main import app
    from core.database import get_db

    db = _make_db()
    task = _make_task()
    assignment = _make_assignment()
    worker = _make_worker()
    worker.avg_feedback_score = 4.0
    worker.total_ratings_received = 5

    # Aggregate result for _refresh_worker_avg
    agg_row = MagicMock()
    agg_row.avg = 4.2
    agg_row.cnt = 6
    agg_result = MagicMock()
    agg_result.one_or_none = MagicMock(return_value=agg_row)

    # Query 1: find task
    # Query 2: check existing rating → None
    # Query 3: find assignment
    # Query 4: _refresh_worker_avg aggregate
    # Query 5: _refresh_worker_avg fetch worker
    db.execute = AsyncMock(
        side_effect=[
            _scalar(task),
            _scalar(None),
            _scalar(assignment),
            agg_result,
            _scalar(worker),
        ]
    )

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/rate",
                json={"score": 5, "comment": "Excellent"},
                headers=_auth(),
            )
        assert r.status_code == 201
        body = r.json()
        assert body["score"] == 5
        assert body["comment"] == "Excellent"
        assert body["task_id"] == TASK_ID
        assert body["worker_id"] == WORKER_ID
        assert body["created_at"] is not None
        db.add.assert_called_once()
        db.flush.assert_awaited_once()
        db.commit.assert_awaited_once()
        # Worker avg was updated
        assert worker.avg_feedback_score == 4.2
        assert worker.total_ratings_received == 6
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_rate_task_score_below_minimum():
    """score=0 violates ge=1 → 422."""
    from main import app
    from core.database import get_db

    db = _make_db()
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/rate",
                json={"score": 0},
                headers=_auth(),
            )
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_rate_task_score_above_maximum():
    """score=6 violates le=5 → 422."""
    from main import app
    from core.database import get_db

    db = _make_db()
    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                f"/v1/tasks/{TASK_ID}/rate",
                json={"score": 6},
                headers=_auth(),
            )
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


# ═══════════════════════════════════════════════════════════════════════════
#  RATINGS — GET /v1/tasks/{task_id}/rating
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_rating_no_auth():
    """GET /v1/tasks/{task_id}/rating without auth → 401."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/v1/tasks/{TASK_ID}/rating")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_rating_task_not_found():
    """Task not found → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute = AsyncMock(return_value=_scalar(None))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/tasks/{TASK_ID}/rating", headers=_auth())
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_rating_non_owner():
    """Non-owner tries to fetch rating → 403."""
    from main import app
    from core.database import get_db

    db = _make_db()
    task = _make_task(owner_id=OTHER_USER)
    db.execute = AsyncMock(return_value=_scalar(task))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/tasks/{TASK_ID}/rating", headers=_auth())
        assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_rating_none_exists():
    """No rating for this task → returns null body with 200."""
    from main import app
    from core.database import get_db

    db = _make_db()
    task = _make_task()

    # Query 1: find task
    # Query 2: find rating → None
    db.execute = AsyncMock(side_effect=[_scalar(task), _scalar(None)])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/tasks/{TASK_ID}/rating", headers=_auth())
        assert r.status_code == 200
        assert r.json() is None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_rating_happy_path():
    """Rating exists → returns RatingOut with correct fields."""
    from main import app
    from core.database import get_db

    db = _make_db()
    task = _make_task()
    rating = _make_rating(score=4)

    # Query 1: find task
    # Query 2: find rating
    db.execute = AsyncMock(side_effect=[_scalar(task), _scalar(rating)])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/tasks/{TASK_ID}/rating", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["score"] == 4
        assert body["comment"] == "Great work"
        assert body["task_id"] == TASK_ID
        assert body["worker_id"] == WORKER_ID
    finally:
        app.dependency_overrides.clear()


# ═══════════════════════════════════════════════════════════════════════════
#  RATINGS — GET /v1/workers/me/ratings
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_my_ratings_no_auth():
    """GET /v1/workers/me/ratings without auth → 401."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/workers/me/ratings")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_my_ratings_empty():
    """No ratings for worker → returns zeros and empty recent list."""
    from main import app
    from core.database import get_db

    db = _make_db()
    me = _make_worker(worker_id=USER_ID)
    me.avg_feedback_score = None
    me.total_ratings_received = 0

    # Query 1: fetch ratings → empty
    # Query 2: fetch user
    db.execute = AsyncMock(side_effect=[_scalars_result([]), _scalar(me)])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/workers/me/ratings", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["avg_score"] is None
        assert body["total_ratings"] == 0
        assert body["recent"] == []
        assert body["distribution"] == {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_my_ratings_with_data():
    """Worker has ratings → returns distribution and recent list."""
    from main import app
    from core.database import get_db

    db = _make_db()
    r1 = _make_rating(score=5)
    r2 = _make_rating(score=4)
    r2.comment = "Good job"

    me = _make_worker(worker_id=USER_ID)
    me.avg_feedback_score = 4.5
    me.total_ratings_received = 2

    # Query 1: fetch ratings
    # Query 2: fetch user
    db.execute = AsyncMock(side_effect=[_scalars_result([r1, r2]), _scalar(me)])

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/workers/me/ratings", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["avg_score"] == 4.5
        assert body["total_ratings"] == 2
        assert len(body["recent"]) == 2
        assert body["recent"][0]["score"] == 5
        assert body["recent"][1]["score"] == 4
        # Distribution should count the scores
        assert body["distribution"]["5"] == 1
        assert body["distribution"]["4"] == 1
    finally:
        app.dependency_overrides.clear()


# ═══════════════════════════════════════════════════════════════════════════
#  RATINGS — GET /v1/workers/{worker_id}/ratings (public)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_public_ratings_worker_not_found():
    """Worker does not exist → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute = AsyncMock(return_value=_scalar(None))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/ratings")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_public_ratings_private_profile():
    """Worker with profile_public=False → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    worker = _make_worker(public=False)
    db.execute = AsyncMock(return_value=_scalar(worker))

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/ratings")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_public_ratings_happy_path():
    """Public worker with ratings → returns summary with distribution."""
    from main import app
    from core.database import get_db

    db = _make_db()
    worker = _make_worker()
    r1 = _make_rating(score=5)
    r2 = _make_rating(score=3)
    r2.comment = "Decent"

    # Query 1: fetch worker
    # Query 2: fetch ratings
    db.execute = AsyncMock(
        side_effect=[_scalar(worker), _scalars_result([r1, r2])]
    )

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/ratings")
        assert r.status_code == 200
        body = r.json()
        assert body["avg_score"] == 4.5
        assert body["total_ratings"] == 10
        assert len(body["recent"]) == 2
        assert body["distribution"]["5"] == 1
        assert body["distribution"]["3"] == 1
        assert body["distribution"]["1"] == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_public_ratings_empty():
    """Public worker with no ratings → returns zeros and empty recent."""
    from main import app
    from core.database import get_db

    db = _make_db()
    worker = _make_worker()
    worker.avg_feedback_score = None
    worker.total_ratings_received = 0

    # Query 1: fetch worker
    # Query 2: fetch ratings → empty
    db.execute = AsyncMock(
        side_effect=[_scalar(worker), _scalars_result([])]
    )

    app.dependency_overrides[get_db] = _db_override(db)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/ratings")
        assert r.status_code == 200
        body = r.json()
        assert body["avg_score"] is None
        assert body["total_ratings"] == 0
        assert body["recent"] == []
        assert body["distribution"] == {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    finally:
        app.dependency_overrides.clear()
