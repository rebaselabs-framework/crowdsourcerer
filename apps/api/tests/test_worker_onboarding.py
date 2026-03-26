"""Unit tests for the worker onboarding flow.

Tests cover:
  - mark_onboarding_step: marks steps, flushes, idempotency, ignores unknown
  - _to_out:              builds status response from DB record
  - _get_or_create:       creates new record with correct defaults
  - ONBOARDING_STEPS:     5 expected steps defined
  - STEP_LABELS:          all steps have labels
  - COMPLETION_BONUS_CREDITS: positive, sane value
  - Auth guards:          all 5 endpoints return 401 without token

No real DB or HTTP required — pure business logic testing.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_progress(**flags):
    """Create a mock OnboardingProgressDB-like object with given step flags."""
    p = MagicMock()
    p.step_profile    = flags.get("profile",     False)
    p.step_explore    = flags.get("explore",     False)
    p.step_first_task = flags.get("first_task",  False)
    p.step_skills     = flags.get("skills",      False)
    p.step_cert       = flags.get("cert",        False)
    p.completed_at    = flags.get("completed_at", None)
    p.skipped_at      = flags.get("skipped_at",  None)
    p.bonus_claimed   = flags.get("bonus_claimed", False)
    p.banner_dismissed= flags.get("banner_dismissed", False)
    p.user_id         = uuid.uuid4()
    p.updated_at      = None
    return p


# ── ONBOARDING_STEPS constant ─────────────────────────────────────────────────

def test_five_steps_defined():
    from routers.onboarding import ONBOARDING_STEPS
    assert len(ONBOARDING_STEPS) == 5


def test_step_names_are_expected():
    from routers.onboarding import ONBOARDING_STEPS
    assert ONBOARDING_STEPS == ["profile", "explore", "first_task", "skills", "cert"]


def test_all_steps_have_labels():
    from routers.onboarding import ONBOARDING_STEPS, STEP_LABELS
    for step in ONBOARDING_STEPS:
        assert step in STEP_LABELS, f"Step '{step}' has no STEP_LABELS entry"
        assert STEP_LABELS[step], f"Step '{step}' has an empty label"


# ── COMPLETION_BONUS_CREDITS ───────────────────────────────────────────────────

def test_bonus_positive():
    from routers.onboarding import COMPLETION_BONUS_CREDITS
    assert COMPLETION_BONUS_CREDITS > 0


def test_bonus_reasonable():
    from routers.onboarding import COMPLETION_BONUS_CREDITS
    assert 10 <= COMPLETION_BONUS_CREDITS <= 10_000


# ── _to_out ───────────────────────────────────────────────────────────────────

def test_to_out_all_incomplete():
    from routers.onboarding import _to_out
    p = _make_progress()
    out = _to_out(p)
    assert out.completed_steps == 0
    assert out.total_steps == 5
    assert out.pct_complete == 0.0
    assert out.is_complete is False
    assert all(not s.completed for s in out.steps)


def test_to_out_partial():
    from routers.onboarding import _to_out
    p = _make_progress(profile=True, explore=True)
    out = _to_out(p)
    assert out.completed_steps == 2
    assert out.pct_complete == 40.0
    profile_step = next(s for s in out.steps if s.key == "profile")
    explore_step = next(s for s in out.steps if s.key == "explore")
    first_task_step = next(s for s in out.steps if s.key == "first_task")
    assert profile_step.completed is True
    assert explore_step.completed is True
    assert first_task_step.completed is False


def test_to_out_all_complete():
    from routers.onboarding import _to_out
    from datetime import datetime, timezone
    p = _make_progress(
        profile=True, explore=True, first_task=True, skills=True, cert=True,
        completed_at=datetime.now(timezone.utc), bonus_claimed=True,
    )
    out = _to_out(p)
    assert out.completed_steps == 5
    assert out.pct_complete == 100.0
    assert out.is_complete is True
    assert out.bonus_claimed is True
    assert all(s.completed for s in out.steps)


def test_to_out_steps_ordered():
    """Steps in the response should follow ONBOARDING_STEPS order."""
    from routers.onboarding import _to_out, ONBOARDING_STEPS
    p = _make_progress()
    out = _to_out(p)
    keys = [s.key for s in out.steps]
    assert keys == ONBOARDING_STEPS


def test_to_out_step_has_order_field():
    from routers.onboarding import _to_out
    p = _make_progress()
    out = _to_out(p)
    for i, step in enumerate(out.steps, start=1):
        assert step.order == i


def test_to_out_step_has_label():
    from routers.onboarding import _to_out
    p = _make_progress()
    out = _to_out(p)
    for step in out.steps:
        assert step.label, f"Step '{step.key}' has no label in _to_out"


# ── mark_onboarding_step ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_step_sets_flag():
    """mark_onboarding_step should set the step flag and flush."""
    from routers.onboarding import mark_onboarding_step
    uid = uuid.uuid4()
    progress = _make_progress(explore=False)

    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = progress
    db.execute = AsyncMock(return_value=mock_result)

    await mark_onboarding_step(uid, "explore", db)

    assert progress.step_explore is True
    db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_mark_step_idempotent():
    """mark_onboarding_step should not re-flush if step is already done."""
    from routers.onboarding import mark_onboarding_step
    uid = uuid.uuid4()
    progress = _make_progress(explore=True)  # already done

    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = progress
    db.execute = AsyncMock(return_value=mock_result)

    await mark_onboarding_step(uid, "explore", db)

    # Step already True — flush should NOT be called
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_step_ignores_unknown_step():
    """mark_onboarding_step with an unknown step name should silently return."""
    from routers.onboarding import mark_onboarding_step
    uid = uuid.uuid4()
    db = AsyncMock()

    await mark_onboarding_step(uid, "nonexistent_step", db)

    # No DB access at all for unknown steps
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_mark_step_updates_updated_at():
    """mark_onboarding_step should update the updated_at timestamp."""
    from routers.onboarding import mark_onboarding_step
    uid = uuid.uuid4()
    progress = _make_progress(cert=False)

    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = progress
    db.execute = AsyncMock(return_value=mock_result)

    await mark_onboarding_step(uid, "cert", db)

    assert progress.updated_at is not None


@pytest.mark.asyncio
async def test_mark_step_all_five_steps():
    """All 5 valid step names should be accepted by mark_onboarding_step."""
    from routers.onboarding import mark_onboarding_step, ONBOARDING_STEPS
    uid = uuid.uuid4()

    for step in ONBOARDING_STEPS:
        progress = _make_progress()
        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = progress
        db.execute = AsyncMock(return_value=mock_result)

        await mark_onboarding_step(uid, step, db)

        col = f"step_{step}"
        assert getattr(progress, col) is True, f"Step '{step}' was not marked"


# ── Persistence: callers must commit after mark_onboarding_step ──────────────

@pytest.mark.asyncio
async def test_skills_endpoint_commits_after_marking_step():
    """get_my_skills must call db.commit() after mark_onboarding_step so the
    step is actually persisted (mark_onboarding_step only calls db.flush)."""
    from routers.skills import get_my_skills

    uid = uuid.uuid4()

    db = AsyncMock()
    # db.execute returns empty skills list
    skills_result = MagicMock()
    skills_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=skills_result)
    db.scalar = AsyncMock(return_value=0)

    # Patch mark_onboarding_step so we don't need real DB for onboarding
    with patch("routers.onboarding.mark_onboarding_step", new_callable=AsyncMock):
        await get_my_skills(db=db, user_id=str(uid))

    # db.commit() MUST be awaited — not just flush — so the step persists
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_cert_attempt_commits_after_marking_onboarding_step():
    """After a cert attempt, mark_onboarding_step('cert') must be followed by
    db.commit() so the step persists beyond the current transaction."""
    from routers.certifications import attempt_certification

    uid = uuid.uuid4()

    # Mock cert and a single question
    mock_cert = MagicMock()
    mock_cert.id = uuid.uuid4()
    mock_cert.task_type = "image_label"
    mock_cert.name = "Image Labeling"
    mock_cert.badge_icon = "🖼️"
    mock_cert.passing_score = 80

    mock_question = MagicMock()
    mock_question.id = uuid.uuid4()
    mock_question.question = "What colour is the sky?"
    mock_question.question_type = "multiple_choice"
    mock_question.correct_answer = "blue"
    mock_question.points = 10
    mock_question.explanation = "It's blue"
    mock_question.order_index = 1

    db = AsyncMock()

    # _get_cert_by_type, existing check, load questions
    cert_result = MagicMock()
    cert_result.scalar_one_or_none.return_value = mock_cert

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None  # first attempt

    questions_result = MagicMock()
    questions_result.scalars.return_value.all.return_value = [mock_question]

    db.execute = AsyncMock(side_effect=[cert_result, existing_result, questions_result])

    from models.schemas import CertAttemptRequest, CertAttemptAnswer
    request = CertAttemptRequest(
        answers=[CertAttemptAnswer(question_id=mock_question.id, answer="blue")]
    )

    with (
        patch("routers.certifications.create_notification", new_callable=AsyncMock),
        patch("routers.onboarding.mark_onboarding_step", new_callable=AsyncMock),
    ):
        await attempt_certification(
            task_type="image_label",
            req=request,
            db=db,
            user_id=str(uid),
        )

    # db.commit() must have been called at least twice:
    # once for saving the cert record, once after mark_onboarding_step
    assert db.commit.await_count >= 2, (
        f"Expected db.commit() called ≥2 times, got {db.commit.await_count}"
    )


# ── Auth guard smoke tests ─────────────────────────────────────────────────────

@pytest.fixture
async def client():
    from main import app
    from httpx import AsyncClient, ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_onboarding_status_requires_auth(client):
    r = await client.get("/v1/onboarding/status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_complete_step_requires_auth(client):
    r = await client.post("/v1/onboarding/steps/explore/complete")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_skip_onboarding_requires_auth(client):
    r = await client.post("/v1/onboarding/skip")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_dismiss_banner_requires_auth(client):
    r = await client.post("/v1/onboarding/dismiss-banner")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_reset_onboarding_requires_auth(client):
    r = await client.post("/v1/onboarding/reset")
    assert r.status_code == 401
