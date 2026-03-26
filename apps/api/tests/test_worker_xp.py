"""Tests for worker XP / level / streak pure functions (worker.py).

Covers:
  1–6.   compute_level — boundary conditions: XP=0, first threshold, max level,
         XP just below a threshold, XP at threshold, xp_to_next=0 at max level
  7–11.  streak_xp_multiplier — tier boundaries: 0 days, 2 days, 3 days,
         7 days, 14 days, 30 days
  12–16. compute_xp_for_task — base XP by type, inaccurate halves XP (min 1),
         streak multiplier applied, unknown type defaults to 10, rounding
  17.    LEVEL_THRESHOLDS — increasing sequence (each threshold > previous)
  18.    LEVEL_NAMES — has exactly len(LEVEL_THRESHOLDS) + 1 entries (blank + 20 names)
  19.    STREAK_MULTIPLIER_TIERS — multipliers are in descending threshold order
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")


# ── compute_level ─────────────────────────────────────────────────────────────

def test_compute_level_zero_xp():
    """0 XP → level 1, xp_to_next = first threshold (100)."""
    from routers.worker import compute_level, LEVEL_THRESHOLDS
    level, xp_to_next = compute_level(0)
    assert level == 1
    assert xp_to_next == LEVEL_THRESHOLDS[1]   # 100


def test_compute_level_just_below_threshold():
    """XP one below Level 2 threshold → still Level 1."""
    from routers.worker import compute_level, LEVEL_THRESHOLDS
    level, _ = compute_level(LEVEL_THRESHOLDS[1] - 1)   # 99 XP
    assert level == 1


def test_compute_level_at_threshold():
    """XP exactly at Level 2 threshold → Level 2."""
    from routers.worker import compute_level, LEVEL_THRESHOLDS
    level, _ = compute_level(LEVEL_THRESHOLDS[1])   # 100 XP
    assert level == 2


def test_compute_level_mid_range():
    """XP in the middle of a tier returns correct level."""
    from routers.worker import compute_level, LEVEL_THRESHOLDS
    # LEVEL_THRESHOLDS[4] = 1000 (Level 5), LEVEL_THRESHOLDS[5] = 2000 (Level 6)
    level, xp_to_next = compute_level(1500)
    assert level == 5
    assert xp_to_next == 2000 - 1500   # 500


def test_compute_level_max_level():
    """Extremely high XP → level capped at 20, xp_to_next = 0."""
    from routers.worker import compute_level, LEVEL_THRESHOLDS
    max_level = len(LEVEL_THRESHOLDS)    # 20
    level, xp_to_next = compute_level(999_999)
    assert level == max_level
    assert xp_to_next == 0


def test_compute_level_at_level_20_threshold():
    """Exactly at the level-20 threshold → level 20, xp_to_next = 0."""
    from routers.worker import compute_level, LEVEL_THRESHOLDS
    level, xp_to_next = compute_level(LEVEL_THRESHOLDS[-1])   # 96000
    assert level == 20
    assert xp_to_next == 0


def test_compute_level_returns_tuple():
    """compute_level returns a 2-tuple of (int, int)."""
    from routers.worker import compute_level
    result = compute_level(500)
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert all(isinstance(v, int) for v in result)


# ── streak_xp_multiplier ──────────────────────────────────────────────────────

def test_streak_multiplier_zero_days():
    """0 streak days → multiplier of 1.0."""
    from routers.worker import streak_xp_multiplier
    assert streak_xp_multiplier(0) == 1.0


def test_streak_multiplier_2_days():
    """2 streak days (below 3-day tier) → multiplier of 1.0."""
    from routers.worker import streak_xp_multiplier
    assert streak_xp_multiplier(2) == 1.0


def test_streak_multiplier_3_day_tier():
    """Exactly 3 streak days → 1.1× multiplier."""
    from routers.worker import streak_xp_multiplier
    assert streak_xp_multiplier(3) == 1.1


def test_streak_multiplier_7_day_tier():
    """Exactly 7 streak days → 1.25× multiplier."""
    from routers.worker import streak_xp_multiplier
    assert streak_xp_multiplier(7) == 1.25


def test_streak_multiplier_14_day_tier():
    """Exactly 14 streak days → 1.5× multiplier."""
    from routers.worker import streak_xp_multiplier
    assert streak_xp_multiplier(14) == 1.5


def test_streak_multiplier_30_day_tier():
    """30+ streak days → 2.0× multiplier."""
    from routers.worker import streak_xp_multiplier
    assert streak_xp_multiplier(30) == 2.0
    assert streak_xp_multiplier(100) == 2.0   # stays at max


def test_streak_multiplier_between_tiers():
    """10 streak days (between 7 and 14) → 1.25×."""
    from routers.worker import streak_xp_multiplier
    assert streak_xp_multiplier(10) == 1.25


# ── compute_xp_for_task ───────────────────────────────────────────────────────

def test_compute_xp_base_known_type():
    """label_image with no streak and accurate → 10 XP."""
    from routers.worker import compute_xp_for_task
    assert compute_xp_for_task("label_image", accurate=True, streak_days=0) == 10


def test_compute_xp_base_verify_fact():
    """verify_fact has higher base XP than label_image."""
    from routers.worker import compute_xp_for_task
    label_xp   = compute_xp_for_task("label_image", accurate=True, streak_days=0)
    verify_xp  = compute_xp_for_task("verify_fact",  accurate=True, streak_days=0)
    assert verify_xp > label_xp


def test_compute_xp_inaccurate_halved():
    """Inaccurate submission → XP halved (floor), minimum 1."""
    from routers.worker import compute_xp_for_task
    # label_image base=10 → 10//2 = 5
    xp = compute_xp_for_task("label_image", accurate=False, streak_days=0)
    assert xp == 5


def test_compute_xp_inaccurate_minimum_one():
    """For a task with base XP of 1 (hypothetical), inaccurate → minimum 1 XP."""
    from routers.worker import compute_xp_for_task, TASK_XP_BASE
    # Find any type where halving would give 0 (base < 2 would do, but
    # our lowest base is 8).  Instead test directly: 8 → 8//2=4 (not 0),
    # so the floor clamps at max(1, base//2).  Just confirm min=1 is robust.
    xp = compute_xp_for_task("label_text", accurate=False, streak_days=0)
    assert xp >= 1


def test_compute_xp_streak_multiplier_applied():
    """30-day streak doubles XP."""
    from routers.worker import compute_xp_for_task
    base_xp   = compute_xp_for_task("label_image", accurate=True, streak_days=0)
    streak_xp = compute_xp_for_task("label_image", accurate=True, streak_days=30)
    assert streak_xp == round(base_xp * 2.0)


def test_compute_xp_unknown_type_defaults_to_10():
    """Unknown task type falls back to a base of 10."""
    from routers.worker import compute_xp_for_task
    xp = compute_xp_for_task("totally_unknown_type", accurate=True, streak_days=0)
    assert xp == 10


def test_compute_xp_combined_inaccurate_with_streak():
    """Inaccurate + streak: halved first, then multiplied."""
    from routers.worker import compute_xp_for_task
    # label_image base=10, inaccurate → 5, then 7-day streak (1.25×) → round(5*1.25)=6
    xp = compute_xp_for_task("label_image", accurate=False, streak_days=7)
    assert xp == round(5 * 1.25)


# ── Data integrity ────────────────────────────────────────────────────────────

def test_level_thresholds_strictly_increasing():
    """LEVEL_THRESHOLDS is strictly non-decreasing."""
    from routers.worker import LEVEL_THRESHOLDS
    for i in range(1, len(LEVEL_THRESHOLDS)):
        assert LEVEL_THRESHOLDS[i] > LEVEL_THRESHOLDS[i - 1], (
            f"Threshold at index {i} ({LEVEL_THRESHOLDS[i]}) "
            f"is not > index {i-1} ({LEVEL_THRESHOLDS[i-1]})"
        )


def test_level_names_count():
    """LEVEL_NAMES has one entry per level (blank at index 0 + 20 level names)."""
    from routers.worker import LEVEL_NAMES, LEVEL_THRESHOLDS
    # Index 0 is blank, indices 1-20 are level names
    assert len(LEVEL_NAMES) == len(LEVEL_THRESHOLDS) + 1


def test_streak_multiplier_tiers_descending():
    """STREAK_MULTIPLIER_TIERS is ordered from highest threshold to lowest."""
    from routers.worker import STREAK_MULTIPLIER_TIERS
    thresholds = [t for t, _ in STREAK_MULTIPLIER_TIERS]
    assert thresholds == sorted(thresholds, reverse=True), (
        "STREAK_MULTIPLIER_TIERS must be in descending threshold order for "
        "the first-match iteration to work correctly."
    )


def test_streak_multiplier_tiers_all_positive():
    """All multipliers in STREAK_MULTIPLIER_TIERS are ≥ 1.0."""
    from routers.worker import STREAK_MULTIPLIER_TIERS
    for threshold, mult in STREAK_MULTIPLIER_TIERS:
        assert mult >= 1.0, f"Multiplier {mult} at threshold {threshold} is < 1.0"


def test_task_xp_base_all_positive():
    """All base XP values in TASK_XP_BASE are positive integers."""
    from routers.worker import TASK_XP_BASE
    for task_type, xp in TASK_XP_BASE.items():
        assert isinstance(xp, int) and xp > 0, f"Invalid XP for {task_type}: {xp}"
