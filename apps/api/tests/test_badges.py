"""Tests for the badges router.

Covers:
  1–6.  compute_eligible_badge_ids — task milestone thresholds (exact boundaries)
  7–9.  compute_eligible_badge_ids — streak badges (3/7/30 days)
  10–13. compute_eligible_badge_ids — level badges (5/10/15/20)
  14–16. compute_eligible_badge_ids — accuracy badges (90/95/100%)
  17–18. compute_eligible_badge_ids — reliability badges (90/100%)
  19–20. compute_eligible_badge_ids — challenge badges (daily + streak 7)
  21.   compute_eligible_badge_ids — all None optional params → no accuracy/reliability badges
  22.   compute_eligible_badge_ids — zero stats → empty set
  23.   compute_eligible_badge_ids — all stats maxed → full eligible set (spot-check)
  24.   ALL_BADGES — no duplicate badge_id values
  25.   ALL_BADGES — _BADGE_MAP covers every badge in ALL_BADGES
  26.   award_new_badges — only inserts badges not already earned
  27.   award_new_badges — no new badges → returns empty list, no DB inserts
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(
    worker_xp: int = 0,
    worker_tasks_completed: int = 0,
    worker_streak_days: int = 0,
    worker_accuracy: float | None = None,
    worker_reliability: float | None = None,
) -> MagicMock:
    u = MagicMock()
    u.id                      = uuid.uuid4()
    u.worker_xp               = worker_xp
    u.worker_tasks_completed  = worker_tasks_completed
    u.worker_streak_days      = worker_streak_days
    u.worker_accuracy         = worker_accuracy
    u.worker_reliability      = worker_reliability
    return u


# ── Pure-function unit tests — compute_eligible_badge_ids ─────────────────────

def test_no_stats_empty_set():
    """Zero stats → no badges earned."""
    from routers.badges import compute_eligible_badge_ids
    result = compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0,
        accuracy=None, reliability=None,
    )
    assert result == set()


def test_first_task_boundary():
    """Exactly 1 task → first_task; 0 tasks → nothing."""
    from routers.badges import compute_eligible_badge_ids
    assert "first_task" in compute_eligible_badge_ids(
        tasks_completed=1, streak_days=0, level=0, accuracy=None, reliability=None
    )
    assert "first_task" not in compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0, accuracy=None, reliability=None
    )


def test_tasks_10_boundary():
    """tasks_10 awarded at exactly 10, not at 9."""
    from routers.badges import compute_eligible_badge_ids
    assert "tasks_10" in compute_eligible_badge_ids(
        tasks_completed=10, streak_days=0, level=0, accuracy=None, reliability=None
    )
    assert "tasks_10" not in compute_eligible_badge_ids(
        tasks_completed=9, streak_days=0, level=0, accuracy=None, reliability=None
    )


def test_tasks_milestone_accumulation():
    """At 500 tasks, earns first_task, tasks_10, tasks_50, tasks_100, tasks_500 (not tasks_1000)."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=500, streak_days=0, level=0, accuracy=None, reliability=None
    )
    assert earned >= {"first_task", "tasks_10", "tasks_50", "tasks_100", "tasks_500"}
    assert "tasks_1000" not in earned


def test_tasks_1000_badge():
    """tasks_1000 awarded at exactly 1000 tasks."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=1000, streak_days=0, level=0, accuracy=None, reliability=None
    )
    assert "tasks_1000" in earned


def test_tasks_99_no_tasks_100():
    """99 tasks does NOT earn tasks_100."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=99, streak_days=0, level=0, accuracy=None, reliability=None
    )
    assert "tasks_100" not in earned
    assert "tasks_50" in earned


# ── Streak badges ─────────────────────────────────────────────────────────────

def test_streak_3_boundary():
    """streak_3 at exactly 3 days, not at 2."""
    from routers.badges import compute_eligible_badge_ids
    assert "streak_3" in compute_eligible_badge_ids(
        tasks_completed=0, streak_days=3, level=0, accuracy=None, reliability=None
    )
    assert "streak_3" not in compute_eligible_badge_ids(
        tasks_completed=0, streak_days=2, level=0, accuracy=None, reliability=None
    )


def test_streak_7_not_at_6():
    """streak_7 requires exactly 7 days; 6 days earns streak_3 but not streak_7."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=0, streak_days=6, level=0, accuracy=None, reliability=None
    )
    assert "streak_3" in earned
    assert "streak_7" not in earned


def test_streak_30_accumulates():
    """30-day streak earns all three streak badges."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=0, streak_days=30, level=0, accuracy=None, reliability=None
    )
    assert earned >= {"streak_3", "streak_7", "streak_30"}


# ── Level badges ──────────────────────────────────────────────────────────────

def test_level_5_boundary():
    """level_5 at level 5; not at level 4."""
    from routers.badges import compute_eligible_badge_ids
    assert "level_5" in compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=5, accuracy=None, reliability=None
    )
    assert "level_5" not in compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=4, accuracy=None, reliability=None
    )


def test_level_20_max():
    """Level 20 earns all four level badges."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=20, accuracy=None, reliability=None
    )
    assert earned >= {"level_5", "level_10", "level_15", "level_20"}


def test_level_19_no_level_20():
    """Level 19 earns level_15 but NOT level_20."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=19, accuracy=None, reliability=None
    )
    assert "level_15" in earned
    assert "level_20" not in earned


# ── Accuracy badges ───────────────────────────────────────────────────────────

def test_accuracy_90_boundary():
    """accuracy_90 at 0.90; not at 0.89."""
    from routers.badges import compute_eligible_badge_ids
    assert "accuracy_90" in compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0, accuracy=0.90, reliability=None
    )
    assert "accuracy_90" not in compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0, accuracy=0.899, reliability=None
    )


def test_accuracy_100_all_tiers():
    """1.0 accuracy earns all three accuracy badges."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0, accuracy=1.0, reliability=None
    )
    assert earned >= {"accuracy_90", "accuracy_95", "accuracy_100"}


def test_accuracy_none_no_accuracy_badges():
    """None accuracy → no accuracy badges."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0, accuracy=None, reliability=None
    )
    for badge in ("accuracy_90", "accuracy_95", "accuracy_100"):
        assert badge not in earned


# ── Reliability badges ────────────────────────────────────────────────────────

def test_reliability_90_boundary():
    """reliability_90 at 0.90; not below."""
    from routers.badges import compute_eligible_badge_ids
    assert "reliability_90" in compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0, accuracy=None, reliability=0.90
    )
    assert "reliability_90" not in compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0, accuracy=None, reliability=0.89
    )


def test_reliability_100():
    """Reliability 1.0 earns both reliability badges."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0, accuracy=None, reliability=1.0
    )
    assert earned >= {"reliability_90", "reliability_100"}


# ── Challenge badges ──────────────────────────────────────────────────────────

def test_daily_challenge_one_completion():
    """One challenge completion → daily_challenge badge."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0, accuracy=None, reliability=None,
        challenge_completions=1,
    )
    assert "daily_challenge" in earned


def test_challenge_7_streak():
    """challenge_streak=7 → challenge_7 badge."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0, accuracy=None, reliability=None,
        challenge_completions=7, challenge_streak=7,
    )
    assert "challenge_7" in earned
    assert "daily_challenge" in earned


def test_challenge_6_streak_no_challenge_7():
    """challenge_streak=6 does NOT earn challenge_7."""
    from routers.badges import compute_eligible_badge_ids
    earned = compute_eligible_badge_ids(
        tasks_completed=0, streak_days=0, level=0, accuracy=None, reliability=None,
        challenge_completions=6, challenge_streak=6,
    )
    assert "challenge_7" not in earned


# ── Badge catalogue integrity ─────────────────────────────────────────────────

def test_no_duplicate_badge_ids():
    """ALL_BADGES has no duplicate badge_id values."""
    from routers.badges import ALL_BADGES
    ids = [b.badge_id for b in ALL_BADGES]
    assert len(ids) == len(set(ids))


def test_badge_map_covers_all_badges():
    """_BADGE_MAP contains every badge in ALL_BADGES."""
    from routers.badges import ALL_BADGES, _BADGE_MAP
    for b in ALL_BADGES:
        assert b.badge_id in _BADGE_MAP, f"Missing from _BADGE_MAP: {b.badge_id}"


def test_all_badges_have_non_empty_strings():
    """Every badge has non-empty name, description, and icon."""
    from routers.badges import ALL_BADGES
    for b in ALL_BADGES:
        assert b.name, f"Empty name for badge_id={b.badge_id}"
        assert b.description, f"Empty description for badge_id={b.badge_id}"
        assert b.icon, f"Empty icon for badge_id={b.badge_id}"


# ── award_new_badges unit tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_award_new_badges_only_new():
    """award_new_badges only inserts badges not already in the DB."""
    from routers.badges import award_new_badges

    user = _make_user(
        worker_tasks_completed=1,   # qualifies for first_task only
        worker_xp=0,                # level 1
        worker_streak_days=0,
    )

    db = MagicMock()
    db.add = MagicMock()
    db.execute = AsyncMock()

    # Simulate first_task already earned
    already_earned_result = MagicMock()
    already_earned_result.all = MagicMock(return_value=[("first_task",)])
    db.execute.return_value = already_earned_result

    new_badges = await award_new_badges(user, db)

    # first_task is already earned — should not be re-added
    assert "first_task" not in new_badges
    # db.add should not have been called (nothing new to add)
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_award_new_badges_inserts_new_badge():
    """award_new_badges inserts a WorkerBadgeDB for each newly earned badge."""
    from routers.badges import award_new_badges

    user = _make_user(
        worker_tasks_completed=1,   # qualifies for first_task
        worker_xp=0,                # level 1
        worker_streak_days=0,
    )

    db = MagicMock()
    db.add = MagicMock()
    db.execute = AsyncMock()

    # No badges already earned
    no_badges_result = MagicMock()
    no_badges_result.all = MagicMock(return_value=[])
    db.execute.return_value = no_badges_result

    new_badges = await award_new_badges(user, db)

    assert "first_task" in new_badges
    assert db.add.called


@pytest.mark.asyncio
async def test_award_new_badges_no_eligible():
    """If worker has zero stats, award_new_badges returns [] and calls no db.add."""
    from routers.badges import award_new_badges

    user = _make_user(
        worker_tasks_completed=0,
        worker_xp=0,
        worker_streak_days=0,
        worker_accuracy=None,
        worker_reliability=None,
    )

    db = MagicMock()
    db.add = MagicMock()
    db.execute = AsyncMock()

    no_badges_result = MagicMock()
    no_badges_result.all = MagicMock(return_value=[])
    db.execute.return_value = no_badges_result

    new_badges = await award_new_badges(user, db)

    assert new_badges == []
    db.add.assert_not_called()


# ── League badge tests ──────────────────────────────────────────────────────

def test_league_badge_definitions_exist():
    """All 6 league badges exist in ALL_BADGES."""
    from routers.badges import ALL_BADGES
    ids = {b.badge_id for b in ALL_BADGES}
    expected = {"league_silver", "league_gold", "league_platinum",
                "league_diamond", "league_obsidian", "league_champion"}
    assert expected.issubset(ids)


def test_tier_badge_map_covers_promotable_tiers():
    """_TIER_BADGE_MAP covers silver through obsidian (not bronze)."""
    from routers.badges import _TIER_BADGE_MAP
    assert "bronze" not in _TIER_BADGE_MAP
    assert set(_TIER_BADGE_MAP.keys()) == {"silver", "gold", "platinum", "diamond", "obsidian"}


@pytest.mark.asyncio
async def test_award_league_badges_promotion():
    """award_league_badges awards the tier badge for a promoted worker."""
    from routers.badges import award_league_badges

    user = _make_user()
    db = MagicMock()
    db.add = MagicMock()
    db.execute = AsyncMock()

    # No badges already earned
    no_badges_result = MagicMock()
    no_badges_result.all = MagicMock(return_value=[])
    db.execute.return_value = no_badges_result

    new_badges = await award_league_badges(user, db, new_tier="silver", final_rank=3)
    assert "league_silver" in new_badges
    assert "league_champion" not in new_badges  # rank 3 ≠ champion


@pytest.mark.asyncio
async def test_award_league_badges_champion():
    """award_league_badges awards champion badge for #1 finisher."""
    from routers.badges import award_league_badges

    user = _make_user()
    db = MagicMock()
    db.add = MagicMock()
    db.execute = AsyncMock()

    no_badges_result = MagicMock()
    no_badges_result.all = MagicMock(return_value=[])
    db.execute.return_value = no_badges_result

    new_badges = await award_league_badges(user, db, new_tier="gold", final_rank=1)
    assert "league_gold" in new_badges
    assert "league_champion" in new_badges


@pytest.mark.asyncio
async def test_award_league_badges_idempotent():
    """award_league_badges skips badges already earned."""
    from routers.badges import award_league_badges

    user = _make_user()
    db = MagicMock()
    db.add = MagicMock()
    db.execute = AsyncMock()

    # Already has league_silver
    already_result = MagicMock()
    already_result.all = MagicMock(return_value=[("league_silver",)])
    db.execute.return_value = already_result

    new_badges = await award_league_badges(user, db, new_tier="silver", final_rank=5)
    assert "league_silver" not in new_badges
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_award_league_badges_bronze_no_badge():
    """Bronze tier has no badge — no tier badge awarded."""
    from routers.badges import award_league_badges

    user = _make_user()
    db = MagicMock()
    db.add = MagicMock()
    db.execute = AsyncMock()

    no_badges_result = MagicMock()
    no_badges_result.all = MagicMock(return_value=[])
    db.execute.return_value = no_badges_result

    new_badges = await award_league_badges(user, db, new_tier="bronze", final_rank=5)
    assert len(new_badges) == 0
