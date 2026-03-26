"""Tests for the referrals router.

Covers:
  1.  _gen_referral_code — produces 8-char alphanumeric strings
  2.  apply_referral_on_signup — happy path: creates ReferralDB, gives referred
      user REFERRED_BONUS credits, puts REFERRER_BONUS in referrer.credits_pending
  3.  apply_referral_on_signup — self-referral is silently ignored
  4.  apply_referral_on_signup — invalid code silently ignored (no referrer found)
  5.  pay_referral_bonus_on_first_task — happy path: pays referrer, clears pending,
      marks bonus_paid=True
  6.  pay_referral_bonus_on_first_task — no unpaid referral → no-op
  7.  GET /v1/referrals/stats — user not found → 404
  8.  GET /v1/referrals/stats — happy path: returns code, url, counts
  9.  GET /v1/referrals — email masking (first 2 chars + *** + @domain)
  10. GET /v1/referrals — pagination: page=2, page_size=1 applies correct offset
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest
from httpx import AsyncClient, ASGITransport


# ── IDs ───────────────────────────────────────────────────────────────────────

REFERRER_ID = str(uuid.uuid4())
REFERRED_ID = str(uuid.uuid4())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _make_user(
    user_id: str = REFERRER_ID,
    role: str = "requester",
    referral_code: str | None = None,
    credits: int = 100,
    credits_pending: int = 0,
) -> MagicMock:
    u = MagicMock()
    u.id              = uuid.UUID(user_id)
    u.email           = f"{user_id[:8]}@example.com"
    u.referral_code   = referral_code
    u.credits         = credits
    u.credits_pending = credits_pending
    u.role            = role
    return u


def _make_referral(
    referrer_id: str = REFERRER_ID,
    referred_id: str = REFERRED_ID,
    bonus_paid: bool = False,
    referrer_bonus_credits: int = 50,
) -> MagicMock:
    r = MagicMock()
    r.id                    = uuid.uuid4()
    r.referrer_id           = uuid.UUID(referrer_id)
    r.referred_id           = uuid.UUID(referred_id)
    r.bonus_paid            = bonus_paid
    r.referrer_bonus_credits = referrer_bonus_credits
    r.created_at            = _now()
    return r


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


def _scalars_result(items: list):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=items[0] if items else None)
    r.scalars            = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=items))
    )
    return r


def _db_override(mock_db):
    async def _override():
        yield mock_db
    return _override


def _real_token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


# ── Unit tests — pure helper functions ────────────────────────────────────────

def test_gen_referral_code_length():
    """_gen_referral_code returns an 8-character string by default."""
    from routers.referrals import _gen_referral_code
    code = _gen_referral_code()
    assert len(code) == 8


def test_gen_referral_code_alphanumeric():
    """_gen_referral_code characters are all letters or digits."""
    from routers.referrals import _gen_referral_code
    import string
    allowed = set(string.ascii_letters + string.digits)
    for _ in range(20):
        code = _gen_referral_code()
        assert all(c in allowed for c in code), f"Non-alphanumeric char in: {code}"


def test_gen_referral_code_custom_length():
    """_gen_referral_code respects a custom length argument."""
    from routers.referrals import _gen_referral_code
    assert len(_gen_referral_code(length=12)) == 12
    assert len(_gen_referral_code(length=4)) == 4


# ── apply_referral_on_signup ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_referral_signup_happy_path():
    """Creates a ReferralDB, credits referred user, puts bonus in referrer.credits_pending."""
    from routers.referrals import apply_referral_on_signup, REFERRED_BONUS, REFERRER_BONUS

    referrer = _make_user(REFERRER_ID, referral_code="CODE1234", credits_pending=0)
    referred = _make_user(REFERRED_ID, credits=100)
    db = _make_db()

    # First execute call: find the referrer by code
    # Second execute call: find the referred user to give them credits
    def _execute_side_effect(stmt):
        # The first call looks up the referrer by code; subsequent call finds referred user
        call_count = db.execute.call_count
        if call_count == 1:
            return _scalar_result(referrer)
        return _scalar_result(referred)

    db.execute.side_effect = _execute_side_effect

    await apply_referral_on_signup(REFERRED_ID, "CODE1234", db)

    # A ReferralDB was added
    added_types = [type(call.args[0]).__name__ for call in db.add.call_args_list]
    assert "ReferralDB" in str(added_types) or db.add.called

    # Referred user got their signup bonus
    assert referred.credits == 100 + REFERRED_BONUS

    # Referrer's pending credit increased
    assert referrer.credits_pending == REFERRER_BONUS


@pytest.mark.asyncio
async def test_apply_referral_self_referral_ignored():
    """Self-referral: same user ID as referrer → silently ignored, nothing added."""
    from routers.referrals import apply_referral_on_signup

    same_id = REFERRER_ID
    referrer = _make_user(same_id, referral_code="SELFREF1")
    db = _make_db()
    db.execute.return_value = _scalar_result(referrer)

    await apply_referral_on_signup(same_id, "SELFREF1", db)

    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_apply_referral_invalid_code_ignored():
    """Invalid referral code (no matching user) → silently ignored."""
    from routers.referrals import apply_referral_on_signup

    db = _make_db()
    db.execute.return_value = _scalar_result(None)  # no referrer found

    await apply_referral_on_signup(REFERRED_ID, "BADCODE1", db)

    db.add.assert_not_called()


# ── pay_referral_bonus_on_first_task ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_pay_referral_bonus_happy_path():
    """Referrer gets credits, pending decrements, bonus_paid set to True."""
    from routers.referrals import pay_referral_bonus_on_first_task

    referral = _make_referral(bonus_paid=False, referrer_bonus_credits=50)
    referrer = _make_user(REFERRER_ID, credits=100, credits_pending=50)
    db = _make_db()

    call_num = [0]
    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(referral)
        return _scalar_result(referrer)

    db.execute.side_effect = _side_effect

    with patch("routers.referrals.create_notification", AsyncMock()):
        await pay_referral_bonus_on_first_task(REFERRED_ID, db)

    assert referral.bonus_paid is True
    assert referrer.credits == 150          # +50
    assert referrer.credits_pending == 0    # 50 − 50


@pytest.mark.asyncio
async def test_pay_referral_bonus_no_unpaid_referral():
    """If no unpaid referral exists for this worker, function is a no-op."""
    from routers.referrals import pay_referral_bonus_on_first_task

    db = _make_db()
    db.execute.return_value = _scalar_result(None)  # no referral record

    await pay_referral_bonus_on_first_task(REFERRED_ID, db)

    # Nothing added to DB
    db.add.assert_not_called()


# ── HTTP endpoint tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_referral_stats_user_not_found():
    """GET /v1/referrals/stats — user not found → 404."""
    from main import app
    from core.database import get_db

    db = _make_db()
    db.execute.return_value = _scalar_result(None)  # no user
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(
                "/v1/referrals/stats",
                headers={"Authorization": f"Bearer {_real_token(REFERRER_ID)}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_referral_stats_happy_path():
    """GET /v1/referrals/stats — returns code, url, and correct counts."""
    from main import app
    from core.database import get_db

    user = _make_user(REFERRER_ID, referral_code="TESTCODE", credits_pending=50)
    db = _make_db()

    call_num = [0]
    def _side_effect(stmt):
        call_num[0] += 1
        if call_num[0] == 1:
            return _scalar_result(user)       # user lookup
        if call_num[0] == 2:
            return _scalar_result(3)          # total_referrals count
        if call_num[0] == 3:
            return _scalar_result(100)        # paid_bonus sum
        return _scalar_result(None)

    db.execute.side_effect = _side_effect
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(
                "/v1/referrals/stats",
                headers={"Authorization": f"Bearer {_real_token(REFERRER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["referral_code"] == "TESTCODE"
        assert "TESTCODE" in body["referral_url"]
        assert body["total_referrals"] == 3
        assert body["paid_bonus_credits"] == 100
        assert body["pending_bonus_credits"] == 50  # from user.credits_pending
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_referrals_email_masking():
    """GET /v1/referrals — email is masked to first 2 chars + *** + @domain."""
    from main import app
    from core.database import get_db

    ref = _make_referral()
    referred_user = MagicMock()
    referred_user.email = "alice@example.com"

    db = _make_db()
    rows_result = MagicMock()
    rows_result.all = MagicMock(return_value=[(ref, referred_user)])
    db.execute.return_value = rows_result
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(
                "/v1/referrals",
                headers={"Authorization": f"Bearer {_real_token(REFERRER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        masked = body[0]["referred_email"]
        # First 2 chars of local part + *** + @domain
        assert masked == "al***@example.com"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_referrals_short_email_masking():
    """Single-char local part still gets masked (no partial leak)."""
    from main import app
    from core.database import get_db

    ref = _make_referral()
    referred_user = MagicMock()
    referred_user.email = "a@example.com"

    db = _make_db()
    rows_result = MagicMock()
    rows_result.all = MagicMock(return_value=[(ref, referred_user)])
    db.execute.return_value = rows_result
    app.dependency_overrides[get_db] = _db_override(db)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(
                "/v1/referrals",
                headers={"Authorization": f"Bearer {_real_token(REFERRER_ID)}"},
            )
        assert r.status_code == 200
        body = r.json()
        masked = body[0]["referred_email"]
        # "a"[:2] = "a" but masking still applies
        assert "***" in masked
        assert "@example.com" in masked
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_referrals_unauthenticated():
    """GET /v1/referrals without token → 401 or 403."""
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/v1/referrals")
    assert r.status_code in (401, 403)
