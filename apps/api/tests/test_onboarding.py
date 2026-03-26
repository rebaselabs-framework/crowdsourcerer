"""Unit tests for the requester onboarding flow.

Tests cover:
  - _build_status: converts DB records to response objects
  - _set_step:     marks steps complete and awards the completion bonus
  - complete_step_internal: idempotency guard (skip already-done steps)
  - REQUESTER_ONBOARDING_STEPS: all 5 expected steps defined
  - ONBOARDING_BONUS: positive, sane value

No real DB or HTTP required — pure business logic testing.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_rec(**flags):
    """Create a mock RequesterOnboardingDB-like object with given step flags."""
    rec = MagicMock()
    rec.step_welcome = flags.get("welcome", False)
    rec.step_create_task = flags.get("create_task", False)
    rec.step_view_results = flags.get("view_results", False)
    rec.step_set_webhook = flags.get("set_webhook", False)
    rec.step_invite_team = flags.get("invite_team", False)
    rec.bonus_claimed = flags.get("bonus_claimed", False)
    rec.completed_at = flags.get("completed_at", None)
    return rec


# ── REQUESTER_ONBOARDING_STEPS constant ──────────────────────────────────────

def test_five_steps_defined():
    from models.schemas import REQUESTER_ONBOARDING_STEPS
    assert len(REQUESTER_ONBOARDING_STEPS) == 5


def test_step_names_are_expected():
    from models.schemas import REQUESTER_ONBOARDING_STEPS
    assert list(REQUESTER_ONBOARDING_STEPS) == [
        "welcome", "create_task", "view_results", "set_webhook", "invite_team",
    ]


def test_all_steps_have_meta():
    from models.schemas import REQUESTER_ONBOARDING_STEPS, REQUESTER_STEP_META
    for step in REQUESTER_ONBOARDING_STEPS:
        assert step in REQUESTER_STEP_META, f"Step '{step}' has no REQUESTER_STEP_META entry"
        meta = REQUESTER_STEP_META[step]
        assert meta.get("title"), f"Step '{step}' has no title"
        assert meta.get("cta"), f"Step '{step}' has no cta"
        assert meta.get("cta_url"), f"Step '{step}' has no cta_url"


# ── ONBOARDING_BONUS constant ─────────────────────────────────────────────────

def test_onboarding_bonus_positive():
    from routers.requester_onboarding import ONBOARDING_BONUS
    assert ONBOARDING_BONUS > 0


def test_onboarding_bonus_reasonable():
    """Sanity-check: bonus should be meaningful but not absurdly large."""
    from routers.requester_onboarding import ONBOARDING_BONUS
    assert 10 <= ONBOARDING_BONUS <= 10_000


# ── _build_status ─────────────────────────────────────────────────────────────

def test_build_status_all_incomplete():
    from routers.requester_onboarding import _build_status
    rec = _make_rec()
    status = _build_status(rec)
    assert status.completed_count == 0
    assert status.total_steps == 5
    assert status.all_complete is False
    assert status.bonus_claimed is False
    assert all(not s.completed for s in status.steps)


def test_build_status_partial():
    from routers.requester_onboarding import _build_status
    rec = _make_rec(welcome=True, create_task=True)
    status = _build_status(rec)
    assert status.completed_count == 2
    assert status.all_complete is False
    welcome = next(s for s in status.steps if s.key == "welcome")
    assert welcome.completed is True
    create_task = next(s for s in status.steps if s.key == "create_task")
    assert create_task.completed is True
    view_results = next(s for s in status.steps if s.key == "view_results")
    assert view_results.completed is False


def test_build_status_all_complete():
    from routers.requester_onboarding import _build_status
    rec = _make_rec(
        welcome=True, create_task=True, view_results=True,
        set_webhook=True, invite_team=True, bonus_claimed=True,
    )
    status = _build_status(rec)
    assert status.completed_count == 5
    assert status.all_complete is True
    assert status.bonus_claimed is True
    assert all(s.completed for s in status.steps)


def test_build_status_steps_ordered():
    """Steps in the response should follow REQUESTER_ONBOARDING_STEPS order."""
    from routers.requester_onboarding import _build_status
    from models.schemas import REQUESTER_ONBOARDING_STEPS
    rec = _make_rec()
    status = _build_status(rec)
    keys = [s.key for s in status.steps]
    assert keys == list(REQUESTER_ONBOARDING_STEPS)


def test_build_status_step_has_required_fields():
    from routers.requester_onboarding import _build_status
    rec = _make_rec()
    status = _build_status(rec)
    for step in status.steps:
        assert step.key
        assert step.title
        assert step.cta
        assert step.cta_url


# ── _set_step ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_step_marks_flag():
    """_set_step should set the appropriate flag on the record."""
    from routers.requester_onboarding import _set_step
    rec = _make_rec()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))

    await _set_step(rec, "welcome", db, "user-123")
    assert rec.step_welcome is True


@pytest.mark.asyncio
async def test_set_step_no_bonus_when_incomplete():
    """Bonus should NOT be claimed if not all steps are done."""
    from routers.requester_onboarding import _set_step
    rec = _make_rec(welcome=True, create_task=False)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))

    await _set_step(rec, "welcome", db, "user-123")
    assert rec.bonus_claimed is False


@pytest.mark.asyncio
async def test_set_step_awards_bonus_when_last_step():
    """Completing the final step should set bonus_claimed=True and credit user."""
    from routers.requester_onboarding import _set_step, ONBOARDING_BONUS

    # All steps already done except invite_team
    rec = _make_rec(
        welcome=True, create_task=True, view_results=True,
        set_webhook=True, invite_team=False, bonus_claimed=False,
    )

    # Mock user with enough credits
    mock_user = MagicMock()
    mock_user.credits = 0
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user

    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_result)
    db.add = MagicMock()

    with patch("core.notify.create_notification", new_callable=AsyncMock):
        await _set_step(rec, "invite_team", db, "user-123")

    assert rec.step_invite_team is True
    assert rec.bonus_claimed is True
    assert rec.completed_at is not None
    assert mock_user.credits == ONBOARDING_BONUS


@pytest.mark.asyncio
async def test_set_step_bonus_not_awarded_twice():
    """If bonus_claimed is already True, the bonus should not be re-awarded."""
    from routers.requester_onboarding import _set_step

    rec = _make_rec(
        welcome=True, create_task=True, view_results=True,
        set_webhook=True, invite_team=False, bonus_claimed=True,  # already claimed
    )

    mock_user = MagicMock()
    mock_user.credits = 500
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user

    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_result)

    await _set_step(rec, "invite_team", db, "user-123")

    # Credits must be unchanged — bonus already given
    assert mock_user.credits == 500


# ── complete_step_internal ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_complete_step_internal_ignores_unknown_step():
    """Unknown step names should be silently ignored (no crash, no DB write)."""
    from routers.requester_onboarding import complete_step_internal
    db = AsyncMock()
    # Should not raise or call db.execute
    await complete_step_internal("user-123", "nonexistent_step", db)
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_complete_step_internal_skips_already_done():
    """If the step flag is already True, the helper should return early without
    calling _set_step (saves a write to DB)."""
    from routers.requester_onboarding import complete_step_internal

    # Record where welcome is already done
    rec = _make_rec(welcome=True)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = rec

    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_result)

    # Even though db.execute is called (to fetch the record), commit should NOT
    # be called because the step is already done.
    await complete_step_internal("user-123", "welcome", db)
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_complete_step_internal_advances_when_not_done():
    """If the step is not done, complete_step_internal should mark it and commit."""
    from routers.requester_onboarding import complete_step_internal

    rec = _make_rec(welcome=False)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = rec

    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_result)
    db.add = MagicMock()

    await complete_step_internal("user-123", "welcome", db)

    assert rec.step_welcome is True
    db.commit.assert_called_once()


# ── Onboarding auth-guard smoke tests ────────────────────────────────────────

@pytest.fixture
async def client():
    from main import app
    from httpx import AsyncClient, ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_onboarding_status_requires_auth(client):
    r = await client.get("/v1/requester-onboarding/status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_onboarding_complete_step_requires_auth(client):
    r = await client.post("/v1/requester-onboarding/steps/welcome/complete")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_onboarding_skip_step_requires_auth(client):
    r = await client.post("/v1/requester-onboarding/steps/set_webhook/skip")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_onboarding_reset_requires_auth(client):
    r = await client.post("/v1/requester-onboarding/reset")
    assert r.status_code == 401
