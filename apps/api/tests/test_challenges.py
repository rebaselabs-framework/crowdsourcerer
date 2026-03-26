"""Tests for the daily challenges router.

Covers:
  1.  _template_for_date — deterministic: same date always gives same template
  2.  _template_for_date — cycles through all templates (no index out of range)
  3.  _template_for_date — different dates give different templates
  4.  GET /v1/challenges/today — non-worker role → 403
  5.  GET /v1/challenges/today — unauthenticated → 401/403
  6.  GET /v1/challenges/today — happy path: returns challenge + progress
  7.  POST /v1/challenges/today/claim — challenge not complete → 400
  8.  POST /v1/challenges/today/claim — already claimed → 409
  9.  POST /v1/challenges/today/claim — happy path: credits + XP awarded,
      bonus_claimed=True, CreditTransactionDB created
  10. CHALLENGE_TEMPLATES — all templates have required keys
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── IDs ───────────────────────────────────────────────────────────────────────

WORKER_ID = str(uuid.uuid4())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _make_user(
    user_id: str = WORKER_ID,
    role: str = "worker",
    credits: int = 100,
    worker_xp: int = 0,
) -> MagicMock:
    u             = MagicMock()
    u.id          = uuid.UUID(user_id)
    u.role        = role
    u.credits     = credits
    u.worker_xp   = worker_xp
    return u


def _make_challenge(
    challenge_id: str | None = None,
    task_type: str = "label_image",
    title: str = "Test Challenge",
    bonus_credits: int = 8,
    bonus_xp: int = 30,
    target_count: int = 3,
    challenge_date: date | None = None,
) -> MagicMock:
    c = MagicMock()
    c.id             = uuid.UUID(challenge_id) if challenge_id else uuid.uuid4()
    c.task_type      = task_type
    c.title          = title
    c.description    = "A test challenge"
    c.bonus_credits  = bonus_credits
    c.bonus_xp       = bonus_xp
    c.target_count   = target_count
    c.challenge_date = challenge_date or date.today()
    return c


def _make_progress(
    progress_id: str | None = None,
    tasks_completed: int = 0,
    bonus_claimed: bool = False,
    bonus_claimed_at: datetime | None = None,
) -> MagicMock:
    p = MagicMock()
    p.id               = uuid.UUID(progress_id) if progress_id else uuid.uuid4()
    p.tasks_completed  = tasks_completed
    p.bonus_claimed    = bonus_claimed
    p.bonus_claimed_at = bonus_claimed_at
    return p


def _make_db() -> MagicMock:
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


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one         = MagicMock(return_value=value)
    r.scalars            = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=[]))
    )
    return r


def _db_override(mock_db):
    async def _override():
        yield mock_db
    return _override


def _real_token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


# ── Unit tests — _template_for_date ──────────────────────────────────────────

def test_template_for_date_deterministic():
    """Same date always returns the same template."""
    from routers.challenges import _template_for_date
    d = date(2026, 3, 15)
    t1 = _template_for_date(d)
    t2 = _template_for_date(d)
    assert t1["task_type"] == t2["task_type"]
    assert t1["title"] == t2["title"]


def test_template_for_date_covers_all_templates():
    """Every template in CHALLENGE_TEMPLATES is reachable by some date."""
    from routers.challenges import _template_for_date, CHALLENGE_TEMPLATES
    n = len(CHALLENGE_TEMPLATES)
    seen_types = set()
    # Cycle through enough dates to hit every template index
    start = date(2026, 1, 1)
    for i in range(n * 2):
        d = start + timedelta(days=i)
        t = _template_for_date(d)
        seen_types.add(t["task_type"])
    all_types = {tmpl["task_type"] for tmpl in CHALLENGE_TEMPLATES}
    assert seen_types == all_types


def test_template_for_date_different_dates_differ():
    """Consecutive dates (typically) give different templates due to cycling."""
    from routers.challenges import _template_for_date, CHALLENGE_TEMPLATES
    # Cycle through a full set of dates; at least 2 different templates seen
    types_seen = set()
    for i in range(len(CHALLENGE_TEMPLATES)):
        d = date(2026, 1, 1) + timedelta(days=i)
        types_seen.add(_template_for_date(d)["task_type"])
    assert len(types_seen) > 1  # not all the same


def test_all_templates_have_required_keys():
    """Every CHALLENGE_TEMPLATES entry has all required fields."""
    from routers.challenges import CHALLENGE_TEMPLATES
    required = {"task_type", "title", "description", "bonus_xp", "bonus_credits", "target_count"}
    for tmpl in CHALLENGE_TEMPLATES:
        missing = required - set(tmpl.keys())
        assert not missing, f"Template missing keys: {missing} — {tmpl.get('title')}"


def test_all_template_bonuses_positive():
    """All challenge bonuses are positive integers."""
    from routers.challenges import CHALLENGE_TEMPLATES
    for tmpl in CHALLENGE_TEMPLATES:
        assert tmpl["bonus_xp"] > 0, f"Non-positive bonus_xp in {tmpl['title']}"
        assert tmpl["bonus_credits"] > 0, f"Non-positive bonus_credits in {tmpl['title']}"
        assert tmpl["target_count"] > 0, f"Non-positive target_count in {tmpl['title']}"


# ── HTTP endpoint tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_today_challenge_non_worker():
    """GET /v1/challenges/today — requester role → 403."""
    from main import app
    from core.database import get_db

    user = _make_user(WORKER_ID, role="requester")
    db = _make_db()
    db.execute.return_value = _scalar_result(user)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(
                "/v1/challenges/today",
                headers={"Authorization": f"Bearer {_real_token(WORKER_ID)}"},
            )
        assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_today_challenge_unauthenticated():
    """GET /v1/challenges/today without token → 401 or 403."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/v1/challenges/today")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_today_challenge_happy_path():
    """GET /v1/challenges/today — returns challenge and progress for a worker."""
    from main import app
    from core.database import get_db

    user       = _make_user(WORKER_ID, role="worker")
    challenge  = _make_challenge(target_count=3)
    progress   = _make_progress(tasks_completed=1, bonus_claimed=False)

    db = _make_db()
    call_num = [0]
    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(user)
        if call_num[0] == 2:
            return _scalar_result(challenge)    # _get_or_create_challenge
        if call_num[0] == 3:
            return _scalar_result(progress)     # _get_or_create_progress
        return _scalar_result(None)

    db.execute.side_effect = _side_effect
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(
                "/v1/challenges/today",
                headers={"Authorization": f"Bearer {_real_token(WORKER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["tasks_completed"] == 1
        assert body["bonus_claimed"] is False
        assert body["is_complete"] is False
        assert body["tasks_remaining"] == 2
        assert "challenge" in body
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_claim_daily_bonus_challenge_incomplete():
    """POST /v1/challenges/today/claim — challenge not complete → 400."""
    from main import app
    from core.database import get_db

    user       = _make_user(WORKER_ID, role="worker")
    challenge  = _make_challenge(target_count=3)
    progress   = _make_progress(tasks_completed=1, bonus_claimed=False)  # only 1/3

    db = _make_db()
    call_num = [0]
    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(user)
        if call_num[0] == 2:
            return _scalar_result(challenge)
        if call_num[0] == 3:
            return _scalar_result(progress)
        return _scalar_result(None)

    db.execute.side_effect = _side_effect
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/v1/challenges/today/claim",
                headers={"Authorization": f"Bearer {_real_token(WORKER_ID)}"},
            )
        assert r.status_code == 400
        assert "2 more tasks" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_claim_daily_bonus_already_claimed():
    """POST /v1/challenges/today/claim — already claimed → 409."""
    from main import app
    from core.database import get_db

    user       = _make_user(WORKER_ID, role="worker")
    challenge  = _make_challenge(target_count=3)
    progress   = _make_progress(tasks_completed=3, bonus_claimed=True)

    db = _make_db()
    call_num = [0]
    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(user)
        if call_num[0] == 2:
            return _scalar_result(challenge)
        if call_num[0] == 3:
            return _scalar_result(progress)
        return _scalar_result(None)

    db.execute.side_effect = _side_effect
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/v1/challenges/today/claim",
                headers={"Authorization": f"Bearer {_real_token(WORKER_ID)}"},
            )
        assert r.status_code == 409
        assert "already claimed" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_claim_daily_bonus_happy_path():
    """POST /v1/challenges/today/claim — awards credits+XP, marks bonus_claimed=True."""
    from main import app
    from core.database import get_db

    user       = _make_user(WORKER_ID, role="worker", credits=100, worker_xp=50)
    challenge  = _make_challenge(target_count=3, bonus_credits=8, bonus_xp=30)
    progress   = _make_progress(tasks_completed=3, bonus_claimed=False)

    db = _make_db()
    call_num = [0]
    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(user)
        if call_num[0] == 2:
            return _scalar_result(challenge)
        if call_num[0] == 3:
            return _scalar_result(progress)
        return _scalar_result(None)

    db.execute.side_effect = _side_effect
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/v1/challenges/today/claim",
                headers={"Authorization": f"Bearer {_real_token(WORKER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["bonus_claimed"] is True
        assert body["is_complete"] is True
        assert body["tasks_remaining"] == 0
    finally:
        app.dependency_overrides.clear()

    # Credits and XP were added
    assert user.credits == 108    # 100 + 8
    assert user.worker_xp == 80   # 50 + 30

    # bonus_claimed was set
    assert progress.bonus_claimed is True

    # A CreditTransactionDB was added
    assert db.add.called
    assert db.commit.called


@pytest.mark.asyncio
async def test_claim_daily_bonus_non_worker():
    """POST /v1/challenges/today/claim — requester role → 403."""
    from main import app
    from core.database import get_db

    user = _make_user(WORKER_ID, role="requester")
    db   = _make_db()
    db.execute.return_value = _scalar_result(user)
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/v1/challenges/today/claim",
                headers={"Authorization": f"Bearer {_real_token(WORKER_ID)}"},
            )
        assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()
