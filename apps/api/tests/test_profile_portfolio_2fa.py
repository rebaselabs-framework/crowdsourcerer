"""Tests for profiles, portfolio, and two-factor auth routers.

Covers:
  Profiles (routers/profiles.py):
    1.  GET /v1/workers/{id}/profile — happy path
    2.  GET /v1/workers/{id}/profile — worker not found → 404
    3.  GET /v1/workers/{id}/profile — requester role → 404
    4.  GET /v1/workers/{id}/profile — private profile → 404
    5.  GET /v1/workers/{id}/profile — role "both" visible
    6.  GET /v1/workers/{id}/task-stats — happy path
    7.  GET /v1/workers/{id}/task-stats — not found → 404
    8.  GET /v1/workers/{id}/recent-activity — happy path
    9.  GET /v1/workers/{id}/recent-activity — not found → 404
   10.  PATCH /v1/users/me/profile — update name
   11.  PATCH /v1/users/me/profile — invalid avatar_url → 400
   12.  PATCH /v1/users/me/profile — invalid website_url → 400
   13.  PATCH /v1/users/me/profile — unauthenticated → 401/403
   14.  PATCH /v1/users/me/profile — user not found → 404
   15.  GET /v1/users/me/profile-status — full profile → score 100
   16.  GET /v1/users/me/profile-status — empty profile → low score
   17.  GET /v1/users/me/profile-status — user not found → 404

  Portfolio (routers/portfolio.py):
   18.  POST /v1/worker/portfolio — happy path
   19.  POST /v1/worker/portfolio — task not found → 404
   20.  POST /v1/worker/portfolio — task not completed → 400
   21.  POST /v1/worker/portfolio — not assigned → 403
   22.  POST /v1/worker/portfolio — duplicate pin → 409
   23.  POST /v1/worker/portfolio — portfolio full → 400
   24.  GET  /v1/worker/portfolio — happy path (empty list)
   25.  PATCH /v1/worker/portfolio/{id} — happy path
   26.  PATCH /v1/worker/portfolio/{id} — not found → 404
   27.  DELETE /v1/worker/portfolio/{id} — happy path → 204
   28.  DELETE /v1/worker/portfolio/{id} — not found → 404
   29.  GET  /v1/workers/{id}/portfolio — happy path (public)
   30.  GET  /v1/workers/{id}/portfolio — private worker → 404

  Two-Factor Auth (routers/two_factor.py):
   31.  GET  /v1/auth/2fa/status — 2FA disabled
   32.  GET  /v1/auth/2fa/status — 2FA enabled with backup codes
   33.  GET  /v1/auth/2fa/status — user not found → 404
   34.  POST /v1/auth/2fa/setup — happy path
   35.  POST /v1/auth/2fa/setup — already enabled → 409
   36.  POST /v1/auth/2fa/setup — user not found → 404
   37.  POST /v1/auth/2fa/enable — happy path
   38.  POST /v1/auth/2fa/enable — invalid code → 400
   39.  POST /v1/auth/2fa/enable — already enabled → 409
   40.  POST /v1/auth/2fa/enable — no secret set → 400
   41.  POST /v1/auth/2fa/enable — user not found → 404
   42.  POST /v1/auth/2fa/disable — happy path with TOTP code
   43.  POST /v1/auth/2fa/disable — happy path with backup code
   44.  POST /v1/auth/2fa/disable — invalid code → 400
   45.  POST /v1/auth/2fa/disable — 2FA not enabled → 400
   46.  POST /v1/auth/2fa/disable — user not found → 404
   47.  POST /v1/auth/2fa/verify — happy path with TOTP
   48.  POST /v1/auth/2fa/verify — happy path with backup code
   49.  POST /v1/auth/2fa/verify — invalid pending token → 401
   50.  POST /v1/auth/2fa/verify — invalid TOTP code → 401
   51.  POST /v1/auth/2fa/verify — 2FA not enabled → 400
   52.  POST /v1/auth/2fa/verify — user inactive → 401
   53.  POST /v1/auth/2fa/verify — user not found → 401
   54.  POST /v1/auth/2fa/verify — refresh token error handled gracefully
   55.  POST /v1/auth/2fa/enable — returns 8 backup codes
"""

import hashlib
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WORKER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PIN_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TASK_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value)
    r.scalar = MagicMock(return_value=value if not isinstance(value, MagicMock) else 0)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all = MagicMock(return_value=[])
    return r


def _make_worker(
    worker_id: str = WORKER_ID,
    role: str = "worker",
    profile_public: bool = True,
    totp_enabled: bool = False,
    totp_secret: str = None,
    totp_backup_codes: list = None,
    bio: str = "Test bio",
    avatar_url: str = "https://example.com/avatar.png",
    location: str = "NYC",
    website_url: str = "https://example.com",
    name: str = "Test Worker",
    is_active: bool = True,
):
    u = MagicMock()
    u.id = uuid.UUID(worker_id)
    u.name = name
    u.email = "worker@example.com"
    u.role = role
    u.bio = bio
    u.avatar_url = avatar_url
    u.location = location
    u.website_url = website_url
    u.profile_public = profile_public
    u.worker_level = 5
    u.worker_xp = 1200
    u.worker_tasks_completed = 50
    u.worker_accuracy = 0.95
    u.worker_reliability = 0.9
    u.reputation_score = 85.5
    u.worker_streak_days = 10
    u.avg_feedback_score = 4.5
    u.total_ratings_received = 20
    u.created_at = NOW
    u.is_active = is_active
    u.totp_enabled = totp_enabled
    u.totp_secret = totp_secret
    u.totp_backup_codes = totp_backup_codes
    u.token_version = 0
    # Fields required by UserOut response model
    u.plan = "free"
    u.credits = 100
    u.availability_status = "available"
    u.email_verified = True
    return u


def _make_pin(
    pin_id: str = PIN_ID,
    worker_id: str = USER_ID,
    task_id: str = TASK_ID,
    caption: str = "My best work",
    display_order: int = 0,
):
    pin = MagicMock()
    pin.id = uuid.UUID(pin_id)
    pin.worker_id = uuid.UUID(worker_id) if isinstance(worker_id, str) else worker_id
    pin.task_id = uuid.UUID(task_id)
    pin.caption = caption
    pin.display_order = display_order
    pin.pinned_at = NOW
    return pin


def _make_task(
    task_id: str = TASK_ID,
    status: str = "completed",
    task_type: str = "web_research",
    user_id: str = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
    execution_mode: str = "human",
):
    task = MagicMock()
    task.id = uuid.UUID(task_id)
    task.status = status
    task.type = task_type
    task.user_id = uuid.UUID(user_id)
    task.execution_mode = execution_mode
    task.input = {"title": "Research Task"}
    task.output = {"summary": "Task results here"}
    return task


def _make_assignment(worker_id: str = USER_ID, task_id: str = TASK_ID):
    a = MagicMock()
    a.worker_id = uuid.UUID(worker_id)
    a.task_id = uuid.UUID(task_id)
    a.status = "approved"
    a.submitted_at = NOW
    a.earnings_credits = 10
    a.xp_earned = 50
    return a


def _make_skill(task_type: str = "web_research"):
    s = MagicMock()
    s.task_type = task_type
    s.proficiency_level = 3
    s.tasks_completed = 25
    s.avg_accuracy = 0.92
    return s


# ---------------------------------------------------------------------------
# App factory — includes all three routers
# ---------------------------------------------------------------------------

def _get_app():
    from main import app
    from routers.profiles import router as profiles_router
    from routers.portfolio import router as portfolio_router, public_router as portfolio_public_router
    from routers.two_factor import router as tfa_router
    # Routers are already included via main.py; just return the app.
    return app


# ===========================================================================
# PROFILES ROUTER TESTS
# ===========================================================================


@pytest.mark.asyncio
async def test_public_profile_happy_path():
    """GET /v1/workers/{id}/profile — returns full profile for a public worker."""
    app = _get_app()
    from core.database import get_db

    worker = _make_worker()
    db = _make_mock_db()

    call_count = [0]
    def _exec_side(_stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            # UserDB lookup
            return _scalar(worker)
        elif call_count[0] == 2:
            # WorkerSkillDB
            return _scalar(None)
        elif call_count[0] == 3:
            # WorkerCertificationDB
            return _scalar(None)
        elif call_count[0] == 4:
            # WorkerBadgeDB
            return _scalar(None)
        return _scalar(None)

    db.execute.side_effect = _exec_side
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/profile")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Test Worker"
        assert body["bio"] == "Test bio"
        assert body["worker_level"] == 5
        assert body["skills"] == []
        assert body["badges"] == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_public_profile_not_found():
    """GET /v1/workers/{id}/profile — unknown user → 404."""
    app = _get_app()
    from core.database import get_db

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/profile")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_public_profile_requester_role():
    """GET /v1/workers/{id}/profile — requester role → 404."""
    app = _get_app()
    from core.database import get_db

    worker = _make_worker(role="requester")
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/profile")
        assert r.status_code == 404
        assert "Worker not found" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_public_profile_private():
    """GET /v1/workers/{id}/profile — private profile → 404."""
    app = _get_app()
    from core.database import get_db

    worker = _make_worker(profile_public=False)
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/profile")
        assert r.status_code == 404
        assert "private" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_public_profile_role_both():
    """GET /v1/workers/{id}/profile — role 'both' is visible."""
    app = _get_app()
    from core.database import get_db

    worker = _make_worker(role="both")
    db = _make_mock_db()

    call_count = [0]
    def _exec_side(_stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            return _scalar(worker)
        return _scalar(None)

    db.execute.side_effect = _exec_side
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/profile")
        assert r.status_code == 200
        assert r.json()["role"] == "both"
    finally:
        app.dependency_overrides.clear()


# ─── Task Stats ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_stats_happy_path():
    """GET /v1/workers/{id}/task-stats — returns per-type stats."""
    app = _get_app()
    from core.database import get_db

    worker = _make_worker()
    skill = _make_skill()
    db = _make_mock_db()

    call_count = [0]
    def _exec_side(_stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            return _scalar(worker)
        # skills query
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=None)
        r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[skill])))
        return r

    db.execute.side_effect = _exec_side
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/task-stats")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["task_type"] == "web_research"
        assert body[0]["tasks_completed"] == 25
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_task_stats_not_found():
    """GET /v1/workers/{id}/task-stats — unknown user → 404."""
    app = _get_app()
    from core.database import get_db

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/task-stats")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ─── Recent Activity ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recent_activity_happy_path():
    """GET /v1/workers/{id}/recent-activity — returns recent approved tasks."""
    app = _get_app()
    from core.database import get_db

    worker = _make_worker()
    assignment = _make_assignment(worker_id=WORKER_ID)
    task = _make_task()
    db = _make_mock_db()

    call_count = [0]
    def _exec_side(_stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            return _scalar(worker)
        # assignments + task join
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=None)
        r.all = MagicMock(return_value=[(assignment, task)])
        return r

    db.execute.side_effect = _exec_side
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/recent-activity")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["task_type"] == "web_research"
        assert body[0]["earnings_credits"] == 10
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_recent_activity_not_found():
    """GET /v1/workers/{id}/recent-activity — unknown user → 404."""
    app = _get_app()
    from core.database import get_db

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/recent-activity")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ─── Update Profile ──────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("core.background.safe_create_task")
async def test_update_profile_name(mock_bg):
    """PATCH /v1/users/me/profile — update name field."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    worker = _make_worker(worker_id=USER_ID)
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                "/v1/users/me/profile",
                json={"name": "Updated Name"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 200
        assert worker.name == "Updated Name"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("core.background.safe_create_task")
async def test_update_profile_invalid_avatar_url(mock_bg):
    """PATCH /v1/users/me/profile — non-http avatar_url → 400."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    worker = _make_worker(worker_id=USER_ID)
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                "/v1/users/me/profile",
                json={"avatar_url": "ftp://bad.com/img.png"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 400
        assert "avatar_url" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("core.background.safe_create_task")
async def test_update_profile_invalid_website_url(mock_bg):
    """PATCH /v1/users/me/profile — non-http website_url → 400."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    worker = _make_worker(worker_id=USER_ID)
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                "/v1/users/me/profile",
                json={"website_url": "not-a-url"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 400
        assert "website_url" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_profile_unauthenticated():
    """PATCH /v1/users/me/profile — no auth header → 401 or 403."""
    app = _get_app()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/users/me/profile", json={"name": "X"})
        assert r.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("core.background.safe_create_task")
async def test_update_profile_user_not_found(mock_bg):
    """PATCH /v1/users/me/profile — user missing from DB → 404."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                "/v1/users/me/profile",
                json={"name": "Ghost"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ─── Profile Status ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_profile_status_full_profile():
    """GET /v1/users/me/profile-status — fully filled profile → score 100."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    worker = _make_worker(worker_id=USER_ID)
    db = _make_mock_db()

    # First execute returns user, then scalar calls return skill_count and cert_count
    db.execute.return_value = _scalar(worker)
    # db.scalar is called for skill_count and cert_count
    db.scalar.side_effect = [3, 1]  # 3 skills, 1 cert

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/users/me/profile-status",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["completeness_pct"] == 100
        assert body["has_name"] is True
        assert body["has_bio"] is True
        assert body["has_avatar"] is True
        assert body["has_location"] is True
        assert body["has_website"] is True
        assert body["skill_count"] == 3
        assert body["cert_count"] == 1
        assert body["missing"] == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_profile_status_empty_profile():
    """GET /v1/users/me/profile-status — empty profile → low score with missing items."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    worker = _make_worker(
        worker_id=USER_ID, bio=None, avatar_url=None,
        location=None, website_url=None,
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)
    db.scalar.side_effect = [0, 0]  # no skills, no certs

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/users/me/profile-status",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        # Only name is present → 15
        assert body["completeness_pct"] == 15
        assert "bio" in body["missing"]
        assert "avatar" in body["missing"]
        assert "location" in body["missing"]
        assert "website" in body["missing"]
        assert "skills" in body["missing"]
        assert "certification" in body["missing"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_profile_status_user_not_found():
    """GET /v1/users/me/profile-status — user not found → 404."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/users/me/profile-status",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ===========================================================================
# PORTFOLIO ROUTER TESTS
# ===========================================================================


@pytest.mark.asyncio
async def test_pin_task_happy_path():
    """POST /v1/worker/portfolio — pin a completed task successfully."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    task = _make_task()
    assignment = _make_assignment()
    db = _make_mock_db()

    # db.get is used for TaskDB lookup
    db.get.return_value = task

    call_count = [0]
    def _exec_side(_stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            # assignment lookup
            return _scalar(assignment)
        elif call_count[0] == 2:
            # existing portfolio items (with_for_update) — empty
            r = MagicMock()
            r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            return r
        elif call_count[0] == 3:
            # _build_item: avg rating query
            row = MagicMock()
            row.avg = None
            r = MagicMock()
            r.one_or_none = MagicMock(return_value=row)
            return r
        return _scalar(None)

    db.execute.side_effect = _exec_side

    # After commit, pin_task calls db.refresh(pin). The real WorkerPortfolioItemDB
    # has id and pinned_at as None until DB defaults fire. Simulate that here.
    async def _refresh_pin(obj):
        if hasattr(obj, "pinned_at") and obj.pinned_at is None:
            obj.pinned_at = NOW
        if hasattr(obj, "id") and obj.id is None:
            obj.id = uuid.uuid4()
    db.refresh = _refresh_pin

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/worker/portfolio",
                json={"task_id": TASK_ID, "caption": "My best work"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 201
        body = r.json()
        assert body["task_type"] == "web_research"
        assert body["caption"] == "My best work"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_pin_task_not_found():
    """POST /v1/worker/portfolio — task doesn't exist → 404."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    db = _make_mock_db()
    db.get.return_value = None  # task not found

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/worker/portfolio",
                json={"task_id": TASK_ID},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 404
        assert "Task not found" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_pin_task_not_completed():
    """POST /v1/worker/portfolio — task not completed → 400."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    task = _make_task(status="pending")
    db = _make_mock_db()
    db.get.return_value = task

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/worker/portfolio",
                json={"task_id": TASK_ID},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 400
        assert "completed" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_pin_task_not_assigned():
    """POST /v1/worker/portfolio — worker not assigned → 403."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    task = _make_task()  # execution_mode=human, user_id != USER_ID
    db = _make_mock_db()
    db.get.return_value = task
    db.execute.return_value = _scalar(None)  # no assignment found

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/worker/portfolio",
                json={"task_id": TASK_ID},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 403
        assert "did not work" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_pin_task_duplicate():
    """POST /v1/worker/portfolio — task already pinned → 409."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    task = _make_task()
    assignment = _make_assignment()
    existing_pin = _make_pin(task_id=TASK_ID)
    db = _make_mock_db()
    db.get.return_value = task

    call_count = [0]
    def _exec_side(_stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            # assignment lookup
            return _scalar(assignment)
        elif call_count[0] == 2:
            # existing items — contains the same task
            r = MagicMock()
            r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[existing_pin])))
            return r
        return _scalar(None)

    db.execute.side_effect = _exec_side

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/worker/portfolio",
                json={"task_id": TASK_ID},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 409
        assert "already pinned" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_pin_task_portfolio_full():
    """POST /v1/worker/portfolio — 10 items already → 400."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    task = _make_task()
    assignment = _make_assignment()
    # 10 existing pins with different task_ids
    existing_pins = [
        _make_pin(task_id=str(uuid.uuid4()), pin_id=str(uuid.uuid4()))
        for _ in range(10)
    ]
    db = _make_mock_db()
    db.get.return_value = task

    call_count = [0]
    def _exec_side(_stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            return _scalar(assignment)
        elif call_count[0] == 2:
            r = MagicMock()
            r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=existing_pins)))
            return r
        return _scalar(None)

    db.execute.side_effect = _exec_side

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/worker/portfolio",
                json={"task_id": TASK_ID},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 400
        assert "full" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_my_portfolio_empty():
    """GET /v1/worker/portfolio — returns empty list when no pins."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    db = _make_mock_db()
    r_mock = MagicMock()
    r_mock.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db.execute.return_value = r_mock

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/worker/portfolio",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 200
        assert r.json() == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_pin_happy_path():
    """PATCH /v1/worker/portfolio/{id} — update caption."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    pin = _make_pin()
    task = _make_task()
    db = _make_mock_db()

    call_count = [0]
    def _exec_side(_stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            # pin lookup
            return _scalar(pin)
        elif call_count[0] == 2:
            # _build_item: avg rating query
            row = MagicMock()
            row.avg = 4.5
            r = MagicMock()
            r.one_or_none = MagicMock(return_value=row)
            return r
        return _scalar(None)

    db.execute.side_effect = _exec_side
    db.get.return_value = task  # _build_item calls db.get(TaskDB, ...)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                f"/v1/worker/portfolio/{PIN_ID}",
                json={"caption": "Updated caption"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 200
        assert pin.caption == "Updated caption"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_pin_not_found():
    """PATCH /v1/worker/portfolio/{id} — pin not found → 404."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                f"/v1/worker/portfolio/{PIN_ID}",
                json={"caption": "x"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_pin_happy_path():
    """DELETE /v1/worker/portfolio/{id} — success → 204."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    pin = _make_pin()
    db = _make_mock_db()
    db.execute.return_value = _scalar(pin)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(
                f"/v1/worker/portfolio/{PIN_ID}",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 204
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_pin_not_found():
    """DELETE /v1/worker/portfolio/{id} — not found → 404."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete(
                f"/v1/worker/portfolio/{PIN_ID}",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ─── Public Portfolio ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_public_portfolio_happy_path():
    """GET /v1/workers/{id}/portfolio — returns public portfolio."""
    app = _get_app()
    from core.database import get_db

    worker = _make_worker()
    pin = _make_pin(worker_id=WORKER_ID)
    task = _make_task()
    db = _make_mock_db()

    call_count = [0]
    def _exec_side(_stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            # worker lookup
            return _scalar(worker)
        elif call_count[0] == 2:
            # pins query
            r = MagicMock()
            r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[pin])))
            return r
        return _scalar(None)

    db.execute.side_effect = _exec_side
    db.get.return_value = task  # public_portfolio calls db.get(TaskDB, ...)

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/portfolio")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["task_type"] == "web_research"
        assert body["items"][0]["task_title"] == "Research Task"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_public_portfolio_private_worker():
    """GET /v1/workers/{id}/portfolio — private worker → 404."""
    app = _get_app()
    from core.database import get_db

    worker = _make_worker(profile_public=False)
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/v1/workers/{WORKER_ID}/portfolio")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ===========================================================================
# TWO-FACTOR AUTH ROUTER TESTS
# ===========================================================================

# Disable slowapi rate limiter for tests
from routers.two_factor import limiter
limiter.enabled = False


@pytest.mark.asyncio
async def test_2fa_status_disabled():
    """GET /v1/auth/2fa/status — 2FA not enabled."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    worker = _make_worker(worker_id=USER_ID, totp_enabled=False)
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/auth/2fa/status",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["backup_codes_remaining"] == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_status_enabled_with_backup_codes():
    """GET /v1/auth/2fa/status — 2FA enabled with 5 backup codes remaining."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    codes = ["hash1", "hash2", "hash3", "hash4", "hash5"]
    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=True,
        totp_secret="JBSWY3DPEHPK3PXP",
        totp_backup_codes=codes,
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/auth/2fa/status",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["backup_codes_remaining"] == 5
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_status_user_not_found():
    """GET /v1/auth/2fa/status — user not found → 404."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/v1/auth/2fa/status",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ─── 2FA Setup ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("routers.two_factor.pyotp")
async def test_2fa_setup_happy_path(mock_pyotp):
    """POST /v1/auth/2fa/setup — generates TOTP secret and URI."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    mock_pyotp.random_base32.return_value = "TESTBASE32SECRET"
    mock_totp_instance = MagicMock()
    mock_totp_instance.provisioning_uri.return_value = "otpauth://totp/CrowdSorcerer:worker@example.com?secret=TESTBASE32SECRET"
    mock_pyotp.TOTP.return_value = mock_totp_instance

    worker = _make_worker(worker_id=USER_ID, totp_enabled=False)
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/setup",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["secret"] == "TESTBASE32SECRET"
        assert "otpauth://" in body["totp_uri"]
        assert body["issuer"] == "CrowdSorcerer"
        assert worker.totp_secret == "TESTBASE32SECRET"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_setup_already_enabled():
    """POST /v1/auth/2fa/setup — already enabled → 409."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    worker = _make_worker(worker_id=USER_ID, totp_enabled=True)
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/setup",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 409
        assert "already enabled" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_setup_user_not_found():
    """POST /v1/auth/2fa/setup — user not found → 404."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/setup",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ─── 2FA Enable ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("routers.two_factor.pyotp")
async def test_2fa_enable_happy_path(mock_pyotp):
    """POST /v1/auth/2fa/enable — valid TOTP → enables 2FA, returns backup codes."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    mock_totp_instance = MagicMock()
    mock_totp_instance.verify.return_value = True
    mock_pyotp.TOTP.return_value = mock_totp_instance

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=False,
        totp_secret="TESTBASE32SECRET",
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/enable",
                json={"code": "123456"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["backup_codes"]) == 8
        assert worker.totp_enabled is True
        assert worker.totp_backup_codes is not None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("routers.two_factor.pyotp")
async def test_2fa_enable_returns_8_codes(mock_pyotp):
    """POST /v1/auth/2fa/enable — backup codes are 8-char hex strings."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    mock_totp_instance = MagicMock()
    mock_totp_instance.verify.return_value = True
    mock_pyotp.TOTP.return_value = mock_totp_instance

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=False,
        totp_secret="SECRET",
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/enable",
                json={"code": "654321"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 200
        codes = r.json()["backup_codes"]
        assert len(codes) == 8
        for code in codes:
            assert len(code) == 8  # token_hex(4) = 8 hex chars
            # Verify each code is valid uppercase hex
            int(code, 16)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("routers.two_factor.pyotp")
async def test_2fa_enable_invalid_code(mock_pyotp):
    """POST /v1/auth/2fa/enable — invalid TOTP code → 400."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    mock_totp_instance = MagicMock()
    mock_totp_instance.verify.return_value = False
    mock_pyotp.TOTP.return_value = mock_totp_instance

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=False,
        totp_secret="TESTBASE32SECRET",
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/enable",
                json={"code": "000000"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 400
        assert "invalid" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_enable_already_enabled():
    """POST /v1/auth/2fa/enable — already enabled → 409."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    worker = _make_worker(worker_id=USER_ID, totp_enabled=True, totp_secret="SECRET")
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/enable",
                json={"code": "123456"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_enable_no_secret():
    """POST /v1/auth/2fa/enable — no secret set (setup not called) → 400."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    worker = _make_worker(worker_id=USER_ID, totp_enabled=False, totp_secret=None)
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/enable",
                json={"code": "123456"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 400
        assert "setup" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_enable_user_not_found():
    """POST /v1/auth/2fa/enable — user not found → 404."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/enable",
                json={"code": "123456"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ─── 2FA Disable ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("routers.two_factor.pyotp")
async def test_2fa_disable_with_totp(mock_pyotp):
    """POST /v1/auth/2fa/disable — valid TOTP code → 204."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    mock_totp_instance = MagicMock()
    mock_totp_instance.verify.return_value = True
    mock_pyotp.TOTP.return_value = mock_totp_instance

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=True,
        totp_secret="TESTBASE32SECRET",
        totp_backup_codes=["hash1", "hash2"],
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/disable",
                json={"code": "123456"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 204
        assert worker.totp_enabled is False
        assert worker.totp_secret is None
        assert worker.totp_backup_codes is None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("routers.two_factor.pyotp")
async def test_2fa_disable_with_backup_code(mock_pyotp):
    """POST /v1/auth/2fa/disable — valid backup code → 204."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    # The backup code in plain: "AABBCCDD" (8-char hex, uppercase)
    backup_code = "AABBCCDD"
    backup_hash = hashlib.sha256(backup_code.encode()).hexdigest()

    # TOTP verify returns False so it falls through to backup code path
    mock_totp_instance = MagicMock()
    mock_totp_instance.verify.return_value = False
    mock_pyotp.TOTP.return_value = mock_totp_instance

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=True,
        totp_secret="TESTBASE32SECRET",
        totp_backup_codes=[backup_hash, "other_hash"],
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/disable",
                json={"code": backup_code},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 204
        assert worker.totp_enabled is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("routers.two_factor.pyotp")
async def test_2fa_disable_invalid_code(mock_pyotp):
    """POST /v1/auth/2fa/disable — invalid code → 400."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    mock_totp_instance = MagicMock()
    mock_totp_instance.verify.return_value = False
    mock_pyotp.TOTP.return_value = mock_totp_instance

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=True,
        totp_secret="TESTBASE32SECRET",
        totp_backup_codes=["somehash"],
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/disable",
                json={"code": "999999"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 400
        assert "invalid" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_disable_not_enabled():
    """POST /v1/auth/2fa/disable — 2FA not enabled → 400."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    worker = _make_worker(worker_id=USER_ID, totp_enabled=False)
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/disable",
                json={"code": "123456"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 400
        assert "not enabled" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_disable_user_not_found():
    """POST /v1/auth/2fa/disable — user not found → 404."""
    app = _get_app()
    from core.database import get_db
    from core.auth import get_current_user_id

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)

    app.dependency_overrides[get_db] = _db_override(db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/disable",
                json={"code": "123456"},
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ─── 2FA Verify (login flow) ─────────────────────────────────────────────

@pytest.mark.asyncio
@patch("routers.two_factor.pyotp")
@patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock)
async def test_2fa_verify_happy_path_totp(mock_refresh, mock_pyotp):
    """POST /v1/auth/2fa/verify — valid pending token + TOTP → access token."""
    app = _get_app()
    from core.database import get_db
    from routers.two_factor import _create_pending_token

    mock_totp_instance = MagicMock()
    mock_totp_instance.verify.return_value = True
    mock_pyotp.TOTP.return_value = mock_totp_instance
    mock_refresh.side_effect = Exception("skip refresh")

    pending = _create_pending_token(USER_ID)

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=True,
        totp_secret="TESTBASE32SECRET",
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/verify",
                json={"pending_token": pending, "code": "123456"},
            )
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("routers.two_factor.pyotp")
@patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock)
async def test_2fa_verify_happy_path_backup_code(mock_refresh, mock_pyotp):
    """POST /v1/auth/2fa/verify — valid pending token + backup code → access token."""
    app = _get_app()
    from core.database import get_db
    from routers.two_factor import _create_pending_token

    backup_code = "AABBCCDD"
    backup_hash = hashlib.sha256(backup_code.encode()).hexdigest()

    mock_totp_instance = MagicMock()
    mock_totp_instance.verify.return_value = False
    mock_pyotp.TOTP.return_value = mock_totp_instance
    mock_refresh.side_effect = Exception("skip refresh")

    pending = _create_pending_token(USER_ID)

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=True,
        totp_secret="TESTBASE32SECRET",
        totp_backup_codes=[backup_hash, "other_hash"],
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/verify",
                json={"pending_token": pending, "code": backup_code},
            )
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        # Backup code should be consumed
        assert backup_hash not in worker.totp_backup_codes
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_verify_invalid_pending_token():
    """POST /v1/auth/2fa/verify — bad pending token → 401."""
    app = _get_app()
    from core.database import get_db

    db = _make_mock_db()
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/verify",
                json={"pending_token": "invalid.token.here", "code": "123456"},
            )
        assert r.status_code == 401
        assert "invalid" in r.json()["detail"].lower() or "expired" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("routers.two_factor.pyotp")
async def test_2fa_verify_invalid_totp_code(mock_pyotp):
    """POST /v1/auth/2fa/verify — valid pending token but wrong code → 401."""
    app = _get_app()
    from core.database import get_db
    from routers.two_factor import _create_pending_token

    mock_totp_instance = MagicMock()
    mock_totp_instance.verify.return_value = False
    mock_pyotp.TOTP.return_value = mock_totp_instance

    pending = _create_pending_token(USER_ID)

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=True,
        totp_secret="TESTBASE32SECRET",
        totp_backup_codes=[],
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/verify",
                json={"pending_token": pending, "code": "000000"},
            )
        assert r.status_code == 401
        assert "invalid" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_verify_not_enabled():
    """POST /v1/auth/2fa/verify — 2FA not enabled on account → 400."""
    app = _get_app()
    from core.database import get_db
    from routers.two_factor import _create_pending_token

    pending = _create_pending_token(USER_ID)

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=False,
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/verify",
                json={"pending_token": pending, "code": "123456"},
            )
        assert r.status_code == 400
        assert "not enabled" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_verify_user_inactive():
    """POST /v1/auth/2fa/verify — user inactive → 401."""
    app = _get_app()
    from core.database import get_db
    from routers.two_factor import _create_pending_token

    pending = _create_pending_token(USER_ID)

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=True, is_active=False,
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/verify",
                json={"pending_token": pending, "code": "123456"},
            )
        assert r.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_2fa_verify_user_not_found():
    """POST /v1/auth/2fa/verify — user not in DB → 401."""
    app = _get_app()
    from core.database import get_db
    from routers.two_factor import _create_pending_token

    pending = _create_pending_token(USER_ID)

    db = _make_mock_db()
    db.execute.return_value = _scalar(None)

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/verify",
                json={"pending_token": pending, "code": "123456"},
            )
        assert r.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("routers.two_factor.pyotp")
@patch("core.refresh_tokens.create_refresh_token", new_callable=AsyncMock)
async def test_2fa_verify_refresh_token_error_graceful(mock_refresh, mock_pyotp):
    """POST /v1/auth/2fa/verify — refresh token creation fails but login still succeeds."""
    app = _get_app()
    from core.database import get_db
    from routers.two_factor import _create_pending_token

    mock_totp_instance = MagicMock()
    mock_totp_instance.verify.return_value = True
    mock_pyotp.TOTP.return_value = mock_totp_instance
    # Refresh token creation raises
    mock_refresh.side_effect = RuntimeError("DB unavailable")

    pending = _create_pending_token(USER_ID)

    worker = _make_worker(
        worker_id=USER_ID, totp_enabled=True,
        totp_secret="TESTBASE32SECRET",
    )
    db = _make_mock_db()
    db.execute.return_value = _scalar(worker)

    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/v1/auth/2fa/verify",
                json={"pending_token": pending, "code": "123456"},
            )
        # Should still succeed — refresh token is best-effort
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        # refresh_token should be None since creation failed
        assert body["refresh_token"] is None
    finally:
        app.dependency_overrides.clear()
