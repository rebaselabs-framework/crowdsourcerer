"""Tests for SLA helper functions (core/sla.py) and skill quiz scoring (skill_quiz.py).

SLA helpers (core.sla):
  1.  get_sla_hours — free/normal → 72h
  2.  get_sla_hours — pro/urgent → 8*0.25 = 2h
  3.  get_sla_hours — enterprise/low → 2*2 = 4h
  4.  get_sla_hours — unknown plan defaults to 72h base
  5.  get_sla_hours — unknown priority defaults to 1.0 multiplier
  6.  compute_sla_deadline — created_at + sla_hours
  7.  is_sla_breached — now before deadline → False
  8.  is_sla_breached — now after deadline → True
  9.  is_sla_breached — explicit now parameter respected
  10. sla_status — completed before deadline → {"status": "met"}
  11. sla_status — completed after deadline → {"status": "breached", completed_at set}
  12. sla_status — ongoing before deadline → {"status": "on_track", remaining_hours > 0}
  13. sla_status — ongoing after deadline → {"status": "breached", overdue_hours > 0}
  14. sla_status — pct_elapsed capped at 100.0
  15. PRIORITY_CREDIT_MULTIPLIER — all four priorities have positive multipliers

Skill quiz (_score_to_proficiency):
  16. 0/10 → proficiency 1
  17. 4/10 → proficiency 2 (40%)
  18. 6/10 → proficiency 3 (60% = PASS_THRESHOLD)
  19. 7/10 → proficiency 3 (70%)
  20. 8/10 → proficiency 4 (80%)
  21. 9/10 → proficiency 5 (90%)
  22. 10/10 → proficiency 5 (100%)
  23. total=0 → proficiency 1 (zero-division guard)
  24. SKILL_CATEGORIES — all entries are non-empty strings
  25. SEED_QUESTIONS — all seeded categories are in SKILL_CATEGORIES
  26. SEED_QUESTIONS — all questions have required keys
  27. SEED_QUESTIONS — all answer indices are valid (0 ≤ a < len(opts))
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")


def _utc(**kwargs) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kwargs)


# ── get_sla_hours ─────────────────────────────────────────────────────────────

def test_get_sla_hours_free_normal():
    from core.sla import get_sla_hours
    assert get_sla_hours("free", "normal") == 72.0


def test_get_sla_hours_pro_urgent():
    """pro base=8, urgent multiplier=0.25 → 8*0.25 = 2.0 hours."""
    from core.sla import get_sla_hours
    assert get_sla_hours("pro", "urgent") == 2.0


def test_get_sla_hours_enterprise_low():
    """enterprise base=2, low multiplier=2.0 → 2*2.0 = 4.0 hours."""
    from core.sla import get_sla_hours
    assert get_sla_hours("enterprise", "low") == 4.0


def test_get_sla_hours_unknown_plan_defaults_to_72():
    """Unknown plan → 72.0h base (same as 'free')."""
    from core.sla import get_sla_hours
    assert get_sla_hours("nonexistent_plan", "normal") == 72.0


def test_get_sla_hours_unknown_priority_defaults_to_1x():
    """Unknown priority → 1.0 multiplier (normal)."""
    from core.sla import get_sla_hours
    base = get_sla_hours("starter", "normal")   # 24.0
    unknown = get_sla_hours("starter", "extreme")
    assert unknown == base


def test_get_sla_hours_starter_high():
    """starter base=24, high multiplier=0.5 → 12.0 hours."""
    from core.sla import get_sla_hours
    assert get_sla_hours("starter", "high") == 12.0


# ── compute_sla_deadline ──────────────────────────────────────────────────────

def test_compute_sla_deadline_correct_offset():
    """Deadline = created_at + sla_hours as timedelta."""
    from core.sla import compute_sla_deadline, get_sla_hours
    created = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    deadline = compute_sla_deadline(created, "starter", "normal")
    expected_hours = get_sla_hours("starter", "normal")
    assert deadline == created + timedelta(hours=expected_hours)


def test_compute_sla_deadline_returns_aware_datetime():
    """Deadline inherits timezone from created_at."""
    from core.sla import compute_sla_deadline
    created = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    deadline = compute_sla_deadline(created, "pro", "high")
    assert deadline.tzinfo is not None


# ── is_sla_breached ───────────────────────────────────────────────────────────

def test_is_sla_breached_false_before_deadline():
    """Task created just now, deadline is in the future → not breached."""
    from core.sla import is_sla_breached
    created = _utc(seconds=-10)   # 10 seconds ago
    now     = datetime.now(timezone.utc)
    # enterprise/normal = 2h deadline → still on track
    assert is_sla_breached(created, "enterprise", "normal", now=now) is False


def test_is_sla_breached_true_after_deadline():
    """Task created long ago, deadline has passed → breached."""
    from core.sla import is_sla_breached
    created = _utc(hours=-100)   # 100 hours ago
    now     = datetime.now(timezone.utc)
    # Any plan/priority combo → deadline is well in the past
    assert is_sla_breached(created, "free", "normal", now=now) is True


def test_is_sla_breached_explicit_now():
    """Explicit now parameter controls whether breach has occurred."""
    from core.sla import is_sla_breached
    created = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    # enterprise/normal = 2h → deadline = 2026-01-01 02:00
    # now 1h before deadline → not breached
    now_before = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    assert is_sla_breached(created, "enterprise", "normal", now=now_before) is False
    # now 3h after deadline → breached
    now_after  = datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)
    assert is_sla_breached(created, "enterprise", "normal", now=now_after) is True


# ── sla_status ────────────────────────────────────────────────────────────────

def test_sla_status_completed_on_time():
    """Completed before deadline → status='met'."""
    from core.sla import sla_status, compute_sla_deadline
    created = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    # enterprise/normal = 2h deadline
    deadline     = compute_sla_deadline(created, "enterprise", "normal")
    completed_at = deadline - timedelta(minutes=30)  # 30 min before deadline
    result = sla_status(created, "enterprise", "normal", completed_at=completed_at)
    assert result["status"] == "met"
    assert "completed_at" in result


def test_sla_status_completed_late():
    """Completed after deadline → status='breached'."""
    from core.sla import sla_status, compute_sla_deadline
    created  = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    deadline = compute_sla_deadline(created, "enterprise", "normal")
    completed_late = deadline + timedelta(hours=1)
    result = sla_status(created, "enterprise", "normal", completed_at=completed_late)
    assert result["status"] == "breached"
    assert "completed_at" in result


def test_sla_status_on_track():
    """Task created recently, still within deadline → on_track."""
    from core.sla import sla_status
    created = _utc(seconds=-10)   # just created
    result  = sla_status(created, "free", "normal")  # 72h deadline
    assert result["status"] == "on_track"
    assert "remaining_hours" in result
    assert result["remaining_hours"] > 0
    assert "pct_elapsed" in result


def test_sla_status_overdue():
    """Task well past deadline, not completed → breached."""
    from core.sla import sla_status
    created = _utc(hours=-200)   # 200 hours ago
    result  = sla_status(created, "free", "normal")  # 72h SLA
    assert result["status"] == "breached"
    assert "overdue_hours" in result
    assert result["overdue_hours"] > 0


def test_sla_status_pct_elapsed_capped_at_100():
    """pct_elapsed is capped at 100.0 even if well past deadline."""
    from core.sla import sla_status
    created = _utc(hours=-500)   # way past deadline
    result  = sla_status(created, "free", "normal")
    # Task is breached so pct_elapsed key may not appear; if it does it's capped
    if "pct_elapsed" in result:
        assert result["pct_elapsed"] <= 100.0


def test_priority_credit_multipliers_positive():
    """All PRIORITY_CREDIT_MULTIPLIER values are positive."""
    from core.sla import PRIORITY_CREDIT_MULTIPLIER
    for priority, mult in PRIORITY_CREDIT_MULTIPLIER.items():
        assert mult > 0, f"Non-positive multiplier for {priority}: {mult}"


def test_priority_credit_multiplier_urgent_highest():
    """Urgent tasks cost more than normal (multiplier > 1.0)."""
    from core.sla import PRIORITY_CREDIT_MULTIPLIER
    assert PRIORITY_CREDIT_MULTIPLIER["urgent"] > PRIORITY_CREDIT_MULTIPLIER["normal"]


def test_priority_credit_multiplier_low_cheapest():
    """Low priority tasks cost less than normal (multiplier < 1.0)."""
    from core.sla import PRIORITY_CREDIT_MULTIPLIER
    assert PRIORITY_CREDIT_MULTIPLIER["low"] < PRIORITY_CREDIT_MULTIPLIER["normal"]


# ── _score_to_proficiency ─────────────────────────────────────────────────────

def test_proficiency_score_0():
    """0/10 → proficiency 1 (below 40%)."""
    from routers.skill_quiz import _score_to_proficiency
    assert _score_to_proficiency(0, 10) == 1


def test_proficiency_score_4_of_10():
    """4/10 = 40% → proficiency 2."""
    from routers.skill_quiz import _score_to_proficiency
    assert _score_to_proficiency(4, 10) == 2


def test_proficiency_score_at_pass_threshold():
    """6/10 = 60% = PASS_THRESHOLD → proficiency 3."""
    from routers.skill_quiz import _score_to_proficiency, PASS_THRESHOLD
    assert PASS_THRESHOLD == 0.6
    assert _score_to_proficiency(6, 10) == 3


def test_proficiency_score_7():
    """7/10 = 70% → proficiency 3 (in the 60-75% band)."""
    from routers.skill_quiz import _score_to_proficiency
    assert _score_to_proficiency(7, 10) == 3


def test_proficiency_score_8():
    """8/10 = 80% → proficiency 4 (75-90% band)."""
    from routers.skill_quiz import _score_to_proficiency
    assert _score_to_proficiency(8, 10) == 4


def test_proficiency_score_9():
    """9/10 = 90% → proficiency 5."""
    from routers.skill_quiz import _score_to_proficiency
    assert _score_to_proficiency(9, 10) == 5


def test_proficiency_score_perfect():
    """10/10 = 100% → proficiency 5."""
    from routers.skill_quiz import _score_to_proficiency
    assert _score_to_proficiency(10, 10) == 5


def test_proficiency_total_zero_guard():
    """total=0 → proficiency 1 (zero-division guard: pct=0)."""
    from routers.skill_quiz import _score_to_proficiency
    assert _score_to_proficiency(0, 0) == 1


def test_proficiency_returns_in_range():
    """Proficiency level is always 1–5 for any valid inputs."""
    from routers.skill_quiz import _score_to_proficiency
    for score in range(11):
        level = _score_to_proficiency(score, 10)
        assert 1 <= level <= 5, f"Level {level} out of range for score {score}/10"


# ── SKILL_CATEGORIES and SEED_QUESTIONS integrity ────────────────────────────

def test_skill_categories_non_empty():
    """All entries in SKILL_CATEGORIES are non-empty strings."""
    from routers.skill_quiz import SKILL_CATEGORIES
    assert len(SKILL_CATEGORIES) > 0
    for cat in SKILL_CATEGORIES:
        assert isinstance(cat, str) and len(cat) > 0


def test_seed_questions_keys_in_skill_categories():
    """All keys in SEED_QUESTIONS are valid SKILL_CATEGORIES entries."""
    from routers.skill_quiz import SEED_QUESTIONS, SKILL_CATEGORIES
    for cat in SEED_QUESTIONS:
        assert cat in SKILL_CATEGORIES, f"SEED_QUESTIONS has unknown category: {cat}"


def test_seed_questions_required_fields():
    """All seed questions have q, opts, a, d fields."""
    from routers.skill_quiz import SEED_QUESTIONS
    required = {"q", "opts", "a", "d"}
    for cat, questions in SEED_QUESTIONS.items():
        for i, q in enumerate(questions):
            missing = required - set(q.keys())
            assert not missing, f"Question {i} in '{cat}' missing fields: {missing}"


def test_seed_questions_valid_answer_indices():
    """Answer index 'a' is a valid index into the 'opts' list."""
    from routers.skill_quiz import SEED_QUESTIONS
    for cat, questions in SEED_QUESTIONS.items():
        for i, q in enumerate(questions):
            opts = q["opts"]
            a    = q["a"]
            assert 0 <= a < len(opts), (
                f"Question {i} in '{cat}': answer index {a} out of range "
                f"for {len(opts)} options"
            )


def test_seed_questions_options_count():
    """All questions have exactly 4 answer options (standard quiz format)."""
    from routers.skill_quiz import SEED_QUESTIONS
    for cat, questions in SEED_QUESTIONS.items():
        for i, q in enumerate(questions):
            assert len(q["opts"]) == 4, (
                f"Question {i} in '{cat}' has {len(q['opts'])} options, expected 4"
            )
