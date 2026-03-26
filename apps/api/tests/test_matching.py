"""Tests for core/matching.py — compute_match_score pure function.

Covers:
  1.  Hard constraint: proficiency < min_skill_level → None
  2.  Hard constraint: proficiency == min_skill_level → allowed (not None)
  3.  Hard constraint: reputation < min_reputation_score → None
  4.  Hard constraint: reputation is None treated as 0.0 for constraint check
  5.  No constraints → returns a float in [0.0, 1.0]
  6.  Accuracy None → defaults to 0.5 (neutral)
  7.  Accuracy 1.0 → higher score than accuracy 0.0
  8.  Reputation None → uses 50.0 as default
  9.  Freshness: last_task_at=None → 0.0 freshness
  10. Freshness: last_task_at=now → maximum freshness
  11. Freshness: last_task_at=31 days ago → 0.0 freshness
  12. match_weight=2.0 → double the raw score (capped at 1.0)
  13. match_weight=0.0 → score is 0.0
  14. match_weight negative → clamped to 0.0
  15. match_weight > 2.0 → clamped to 2.0
  16. max proficiency (5) → highest proficiency component
  17. min proficiency (1) → lowest proficiency component
  18. Score is always in [0.0, 1.0] range

Also covers scope data integrity (core.scopes):
  19. ALL_SCOPES — no duplicate entries
  20. SCOPE_DESCRIPTIONS — covers every scope in ALL_SCOPES
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(n: float) -> datetime:
    return _now() - timedelta(days=n)


# ── compute_match_score ───────────────────────────────────────────────────────

def test_hard_constraint_proficiency_below_min_returns_none():
    """proficiency_level < min_skill_level → None (ineligible)."""
    from core.matching import compute_match_score
    result = compute_match_score(
        proficiency_level=2,
        accuracy=0.9,
        reputation_score=80.0,
        last_task_at=_now(),
        min_skill_level=3,
    )
    assert result is None


def test_hard_constraint_proficiency_at_min_is_allowed():
    """proficiency_level == min_skill_level → not None."""
    from core.matching import compute_match_score
    result = compute_match_score(
        proficiency_level=3,
        accuracy=0.9,
        reputation_score=80.0,
        last_task_at=_now(),
        min_skill_level=3,
    )
    assert result is not None
    assert isinstance(result, float)


def test_hard_constraint_reputation_below_min_returns_none():
    """reputation_score < min_reputation_score → None."""
    from core.matching import compute_match_score
    result = compute_match_score(
        proficiency_level=5,
        accuracy=1.0,
        reputation_score=40.0,
        last_task_at=_now(),
        min_reputation_score=50.0,
    )
    assert result is None


def test_hard_constraint_reputation_none_treated_as_zero():
    """reputation_score=None with min_reputation_score=10 → None (0.0 < 10)."""
    from core.matching import compute_match_score
    result = compute_match_score(
        proficiency_level=5,
        accuracy=1.0,
        reputation_score=None,
        last_task_at=_now(),
        min_reputation_score=10.0,
    )
    assert result is None


def test_no_constraints_returns_float():
    """No hard constraints → returns a float."""
    from core.matching import compute_match_score
    result = compute_match_score(
        proficiency_level=3,
        accuracy=0.8,
        reputation_score=60.0,
        last_task_at=_now(),
    )
    assert result is not None
    assert 0.0 <= result <= 1.0


def test_accuracy_none_uses_neutral_default():
    """accuracy=None → uses 0.5 (neutral). Higher accuracy gives a higher score."""
    from core.matching import compute_match_score
    score_none = compute_match_score(
        proficiency_level=3, accuracy=None, reputation_score=50.0, last_task_at=_now()
    )
    score_high = compute_match_score(
        proficiency_level=3, accuracy=1.0, reputation_score=50.0, last_task_at=_now()
    )
    assert score_none is not None
    assert score_high > score_none   # 1.0 accuracy beats 0.5 default


def test_accuracy_1_higher_than_accuracy_0():
    """Accuracy 1.0 gives a higher score than accuracy 0.0."""
    from core.matching import compute_match_score
    base = dict(proficiency_level=3, reputation_score=50.0, last_task_at=_now())
    s1   = compute_match_score(**base, accuracy=1.0)
    s0   = compute_match_score(**base, accuracy=0.0)
    assert s1 > s0


def test_reputation_none_uses_50_default():
    """reputation_score=None → treated as 50.0 (neutral). Score is between extremes."""
    from core.matching import compute_match_score
    base = dict(proficiency_level=3, accuracy=0.8, last_task_at=_now())
    s_none = compute_match_score(**base, reputation_score=None)
    s_high = compute_match_score(**base, reputation_score=100.0)
    s_low  = compute_match_score(**base, reputation_score=0.0)
    assert s_low < s_none < s_high


def test_freshness_none_last_task_is_zero():
    """last_task_at=None → freshness component is 0.0."""
    from core.matching import compute_match_score, _W_FRESHNESS
    # With freshness=0, score equals the weighted sum without freshness
    s_no_history  = compute_match_score(
        proficiency_level=3, accuracy=0.8, reputation_score=50.0, last_task_at=None
    )
    s_just_active = compute_match_score(
        proficiency_level=3, accuracy=0.8, reputation_score=50.0, last_task_at=_now()
    )
    # Active worker scores higher because freshness > 0
    assert s_just_active > s_no_history


def test_freshness_now_is_maximum():
    """last_task_at=now → freshness = 1.0 (maximum)."""
    from core.matching import compute_match_score
    s_now = compute_match_score(
        proficiency_level=5, accuracy=1.0, reputation_score=100.0,
        last_task_at=_now()
    )
    # At max settings, score should approach 1.0
    assert s_now is not None
    assert s_now > 0.9


def test_freshness_31_days_ago_is_zero():
    """last_task_at = 31 days ago → freshness decays to 0.0."""
    from core.matching import compute_match_score
    s_old    = compute_match_score(
        proficiency_level=3, accuracy=0.8, reputation_score=50.0,
        last_task_at=_days_ago(31)
    )
    s_no_history = compute_match_score(
        proficiency_level=3, accuracy=0.8, reputation_score=50.0,
        last_task_at=None
    )
    # Both should give the same result (freshness=0 in both cases)
    assert s_old == s_no_history


def test_match_weight_doubles_score():
    """match_weight=2.0 doubles raw score (capped at 1.0)."""
    from core.matching import compute_match_score
    base = dict(proficiency_level=2, accuracy=0.5, reputation_score=30.0, last_task_at=None)
    s1 = compute_match_score(**base, match_weight=1.0)
    s2 = compute_match_score(**base, match_weight=2.0)
    assert s2 is not None and s1 is not None
    # With weight=2, score should be higher (or 1.0 if already near max)
    assert s2 >= s1


def test_match_weight_zero_gives_zero():
    """match_weight=0.0 → score is 0.0."""
    from core.matching import compute_match_score
    result = compute_match_score(
        proficiency_level=5, accuracy=1.0, reputation_score=100.0,
        last_task_at=_now(), match_weight=0.0
    )
    assert result == 0.0


def test_match_weight_negative_clamped_to_zero():
    """match_weight < 0 → clamped to 0.0 → score is 0.0."""
    from core.matching import compute_match_score
    result = compute_match_score(
        proficiency_level=5, accuracy=1.0, reputation_score=100.0,
        last_task_at=_now(), match_weight=-1.0
    )
    assert result == 0.0


def test_match_weight_above_2_clamped():
    """match_weight > 2.0 → clamped to 2.0 (same result as weight=2.0)."""
    from core.matching import compute_match_score
    base = dict(proficiency_level=2, accuracy=0.5, reputation_score=30.0, last_task_at=None)
    s_2   = compute_match_score(**base, match_weight=2.0)
    s_100 = compute_match_score(**base, match_weight=100.0)
    assert s_2 == s_100


def test_max_proficiency_higher_score():
    """proficiency_level=5 → higher score than proficiency_level=1."""
    from core.matching import compute_match_score
    base = dict(accuracy=0.8, reputation_score=50.0, last_task_at=_now())
    s5 = compute_match_score(**base, proficiency_level=5)
    s1 = compute_match_score(**base, proficiency_level=1)
    assert s5 > s1


def test_score_always_in_0_1_range():
    """compute_match_score always returns None or a float in [0.0, 1.0]."""
    from core.matching import compute_match_score
    test_cases = [
        dict(proficiency_level=1, accuracy=0.0, reputation_score=0.0, last_task_at=None),
        dict(proficiency_level=5, accuracy=1.0, reputation_score=100.0, last_task_at=_now()),
        dict(proficiency_level=3, accuracy=None, reputation_score=None, last_task_at=_days_ago(20)),
        dict(proficiency_level=2, accuracy=0.7, reputation_score=50.0, last_task_at=_now(), match_weight=1.5),
    ]
    for kwargs in test_cases:
        result = compute_match_score(**kwargs)
        if result is not None:
            assert 0.0 <= result <= 1.0, f"Score out of range: {result} for {kwargs}"


# ── Scope data integrity ──────────────────────────────────────────────────────

def test_all_scopes_no_duplicates():
    """ALL_SCOPES list has no duplicate entries."""
    from core.scopes import ALL_SCOPES
    assert len(ALL_SCOPES) == len(set(ALL_SCOPES)), "Duplicate scope found in ALL_SCOPES"


def test_scope_descriptions_cover_all_scopes():
    """SCOPE_DESCRIPTIONS has an entry for every scope in ALL_SCOPES."""
    from core.scopes import ALL_SCOPES, SCOPE_DESCRIPTIONS
    for scope in ALL_SCOPES:
        assert scope in SCOPE_DESCRIPTIONS, f"Missing description for scope: {scope}"


def test_all_scope_constants_in_all_scopes_list():
    """Each SCOPE_xxx constant is included in ALL_SCOPES."""
    from core.scopes import (
        ALL_SCOPES,
        SCOPE_TASKS_READ, SCOPE_TASKS_WRITE,
        SCOPE_PIPELINES_READ, SCOPE_PIPELINES_WRITE,
        SCOPE_CREDITS_READ, SCOPE_CREDITS_WRITE,
        SCOPE_WORKERS_READ, SCOPE_ANALYTICS_READ,
        SCOPE_WEBHOOKS_READ, SCOPE_WEBHOOKS_WRITE,
        SCOPE_MARKETPLACE_READ, SCOPE_MARKETPLACE_WRITE,
    )
    for scope in (
        SCOPE_TASKS_READ, SCOPE_TASKS_WRITE,
        SCOPE_PIPELINES_READ, SCOPE_PIPELINES_WRITE,
        SCOPE_CREDITS_READ, SCOPE_CREDITS_WRITE,
        SCOPE_WORKERS_READ, SCOPE_ANALYTICS_READ,
        SCOPE_WEBHOOKS_READ, SCOPE_WEBHOOKS_WRITE,
        SCOPE_MARKETPLACE_READ, SCOPE_MARKETPLACE_WRITE,
    ):
        assert scope in ALL_SCOPES, f"Scope constant not in ALL_SCOPES: {scope}"
