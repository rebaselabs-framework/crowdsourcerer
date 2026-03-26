"""Tests for core/reputation.py (pure functions) and core/quotas.py (pure functions + data integrity).

Reputation (core.reputation):
  1.  reputation_tier — score >= 90 → "Elite"
  2.  reputation_tier — score == 90 (boundary) → "Elite"
  3.  reputation_tier — score 89.9 → "Expert"
  4.  reputation_tier — score >= 75 → "Expert"
  5.  reputation_tier — score == 75 (boundary) → "Expert"
  6.  reputation_tier — score 74.9 → "Proficient"
  7.  reputation_tier — score >= 60 → "Proficient"
  8.  reputation_tier — score == 60 (boundary) → "Proficient"
  9.  reputation_tier — score 59.9 → "Developing"
  10. reputation_tier — score >= 40 → "Developing"
  11. reputation_tier — score == 40 (boundary) → "Developing"
  12. reputation_tier — score 39.9 → "Novice"
  13. reputation_tier — score >= 20 → "Novice"
  14. reputation_tier — score == 20 (boundary) → "Novice"
  15. reputation_tier — score 19.9 → "Untrusted"
  16. reputation_tier — score 0 → "Untrusted"
  17. reputation_tier — score 100 → "Elite"

  18. reputation_color — >= 90 → "text-yellow-400"
  19. reputation_color — >= 75 (but < 90) → "text-green-400"
  20. reputation_color — >= 60 (but < 75) → "text-blue-400"
  21. reputation_color — >= 40 (but < 60) → "text-gray-300"
  22. reputation_color — >= 20 (but < 40) → "text-orange-400"
  23. reputation_color — < 20 → "text-red-400"
  24. reputation_color — 0 → "text-red-400"

  25. STRIKE_PENALTIES — all values are positive integers
  26. STRIKE_PENALTIES — all four severity keys present

Quotas (core.quotas):
  27. get_plan_quota — free/tasks_per_day → 10
  28. get_plan_quota — starter/tasks_per_day → 100
  29. get_plan_quota — pro/tasks_per_day → 500
  30. get_plan_quota — enterprise/tasks_per_day → None (unlimited)
  31. get_plan_quota — pro/pipelines_total → None (unlimited)
  32. get_plan_quota — enterprise/pipeline_runs_per_day → None (unlimited)
  33. get_plan_quota — unknown plan falls back to "free" limits
  34. get_plan_quota — unknown key → None

  35. enforce_batch_size — count within limit → no exception
  36. enforce_batch_size — count exactly at limit → no exception
  37. enforce_batch_size — count one over limit → HTTP 400
  38. enforce_batch_size — count well over limit → HTTP 400
  39. enforce_batch_size — free plan limit is 10
  40. enforce_batch_size — enterprise plan limit is 50

  41. PLAN_QUOTAS — all four plans present
  42. PLAN_QUOTAS — every plan has all expected quota keys
  43. PLAN_QUOTAS — enterprise tasks_per_day is None
  44. PLAN_QUOTAS — enterprise pipelines_total is None
  45. PLAN_QUOTAS — enterprise pipeline_runs_per_day is None
  46. PLAN_QUOTAS — pro pipelines_total is None
  47. PLAN_QUOTAS — numeric limits are positive where not None
  48. PLAN_QUOTAS — limits increase from free → starter → pro (for bounded plans)
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest
from fastapi import HTTPException


# ── reputation_tier ───────────────────────────────────────────────────────────

def test_reputation_tier_100_is_elite():
    from core.reputation import reputation_tier
    assert reputation_tier(100.0) == "Elite"


def test_reputation_tier_90_boundary_is_elite():
    from core.reputation import reputation_tier
    assert reputation_tier(90.0) == "Elite"


def test_reputation_tier_just_below_90_is_expert():
    from core.reputation import reputation_tier
    assert reputation_tier(89.99) == "Expert"


def test_reputation_tier_80_is_expert():
    from core.reputation import reputation_tier
    assert reputation_tier(80.0) == "Expert"


def test_reputation_tier_75_boundary_is_expert():
    from core.reputation import reputation_tier
    assert reputation_tier(75.0) == "Expert"


def test_reputation_tier_just_below_75_is_proficient():
    from core.reputation import reputation_tier
    assert reputation_tier(74.99) == "Proficient"


def test_reputation_tier_65_is_proficient():
    from core.reputation import reputation_tier
    assert reputation_tier(65.0) == "Proficient"


def test_reputation_tier_60_boundary_is_proficient():
    from core.reputation import reputation_tier
    assert reputation_tier(60.0) == "Proficient"


def test_reputation_tier_just_below_60_is_developing():
    from core.reputation import reputation_tier
    assert reputation_tier(59.99) == "Developing"


def test_reputation_tier_50_is_developing():
    from core.reputation import reputation_tier
    assert reputation_tier(50.0) == "Developing"


def test_reputation_tier_40_boundary_is_developing():
    from core.reputation import reputation_tier
    assert reputation_tier(40.0) == "Developing"


def test_reputation_tier_just_below_40_is_novice():
    from core.reputation import reputation_tier
    assert reputation_tier(39.99) == "Novice"


def test_reputation_tier_30_is_novice():
    from core.reputation import reputation_tier
    assert reputation_tier(30.0) == "Novice"


def test_reputation_tier_20_boundary_is_novice():
    from core.reputation import reputation_tier
    assert reputation_tier(20.0) == "Novice"


def test_reputation_tier_just_below_20_is_untrusted():
    from core.reputation import reputation_tier
    assert reputation_tier(19.99) == "Untrusted"


def test_reputation_tier_0_is_untrusted():
    from core.reputation import reputation_tier
    assert reputation_tier(0.0) == "Untrusted"


def test_reputation_tier_all_six_tiers_covered():
    """Every tier label is reachable."""
    from core.reputation import reputation_tier
    tiers = {reputation_tier(s) for s in [0, 10, 25, 45, 62, 80, 95]}
    assert tiers == {"Untrusted", "Novice", "Developing", "Proficient", "Expert", "Elite"}


# ── reputation_color ──────────────────────────────────────────────────────────

def test_reputation_color_elite():
    from core.reputation import reputation_color
    assert reputation_color(95.0) == "text-yellow-400"


def test_reputation_color_90_boundary():
    from core.reputation import reputation_color
    assert reputation_color(90.0) == "text-yellow-400"


def test_reputation_color_expert():
    from core.reputation import reputation_color
    assert reputation_color(80.0) == "text-green-400"


def test_reputation_color_75_boundary():
    from core.reputation import reputation_color
    assert reputation_color(75.0) == "text-green-400"


def test_reputation_color_proficient():
    from core.reputation import reputation_color
    assert reputation_color(65.0) == "text-blue-400"


def test_reputation_color_60_boundary():
    from core.reputation import reputation_color
    assert reputation_color(60.0) == "text-blue-400"


def test_reputation_color_developing():
    from core.reputation import reputation_color
    assert reputation_color(50.0) == "text-gray-300"


def test_reputation_color_40_boundary():
    from core.reputation import reputation_color
    assert reputation_color(40.0) == "text-gray-300"


def test_reputation_color_novice():
    from core.reputation import reputation_color
    assert reputation_color(25.0) == "text-orange-400"


def test_reputation_color_20_boundary():
    from core.reputation import reputation_color
    assert reputation_color(20.0) == "text-orange-400"


def test_reputation_color_untrusted():
    from core.reputation import reputation_color
    assert reputation_color(10.0) == "text-red-400"


def test_reputation_color_zero():
    from core.reputation import reputation_color
    assert reputation_color(0.0) == "text-red-400"


# ── STRIKE_PENALTIES integrity ────────────────────────────────────────────────

def test_strike_penalties_all_positive():
    """Every strike penalty is a positive number."""
    from core.reputation import STRIKE_PENALTIES
    for severity, penalty in STRIKE_PENALTIES.items():
        assert penalty > 0, f"Non-positive penalty for '{severity}': {penalty}"


def test_strike_penalties_has_all_severity_keys():
    """All four severity levels are represented."""
    from core.reputation import STRIKE_PENALTIES
    required = {"warning", "minor", "major", "critical"}
    assert required.issubset(set(STRIKE_PENALTIES.keys()))


def test_strike_penalties_severity_ordering():
    """Penalties increase with severity: warning < minor < major < critical."""
    from core.reputation import STRIKE_PENALTIES
    assert STRIKE_PENALTIES["warning"] < STRIKE_PENALTIES["minor"]
    assert STRIKE_PENALTIES["minor"] < STRIKE_PENALTIES["major"]
    assert STRIKE_PENALTIES["major"] < STRIKE_PENALTIES["critical"]


# ── get_plan_quota ────────────────────────────────────────────────────────────

def test_get_plan_quota_free_tasks_per_day():
    from core.quotas import get_plan_quota
    assert get_plan_quota("free", "tasks_per_day") == 10


def test_get_plan_quota_starter_tasks_per_day():
    from core.quotas import get_plan_quota
    assert get_plan_quota("starter", "tasks_per_day") == 100


def test_get_plan_quota_pro_tasks_per_day():
    from core.quotas import get_plan_quota
    assert get_plan_quota("pro", "tasks_per_day") == 500


def test_get_plan_quota_enterprise_tasks_per_day_unlimited():
    """enterprise/tasks_per_day is None (unlimited)."""
    from core.quotas import get_plan_quota
    assert get_plan_quota("enterprise", "tasks_per_day") is None


def test_get_plan_quota_pro_pipelines_total_unlimited():
    """pro/pipelines_total is None (unlimited)."""
    from core.quotas import get_plan_quota
    assert get_plan_quota("pro", "pipelines_total") is None


def test_get_plan_quota_enterprise_pipeline_runs_unlimited():
    """enterprise/pipeline_runs_per_day is None (unlimited)."""
    from core.quotas import get_plan_quota
    assert get_plan_quota("enterprise", "pipeline_runs_per_day") is None


def test_get_plan_quota_free_pipelines_total():
    from core.quotas import get_plan_quota
    assert get_plan_quota("free", "pipelines_total") == 2


def test_get_plan_quota_unknown_plan_falls_back_to_free():
    """Unknown plan falls back to free plan limits."""
    from core.quotas import get_plan_quota
    assert get_plan_quota("unknown_plan", "tasks_per_day") == get_plan_quota("free", "tasks_per_day")


def test_get_plan_quota_unknown_key_returns_none():
    """Unknown quota key returns None."""
    from core.quotas import get_plan_quota
    assert get_plan_quota("free", "nonexistent_key") is None


def test_get_plan_quota_batch_task_size_per_plan():
    """batch_task_size increases from free to starter to pro."""
    from core.quotas import get_plan_quota
    free_size    = get_plan_quota("free",    "batch_task_size")
    starter_size = get_plan_quota("starter", "batch_task_size")
    pro_size     = get_plan_quota("pro",     "batch_task_size")
    assert free_size < starter_size <= pro_size


# ── enforce_batch_size ────────────────────────────────────────────────────────

def test_enforce_batch_size_within_limit_no_exception():
    """Task count below limit raises no exception."""
    from core.quotas import enforce_batch_size
    enforce_batch_size("free", 5)   # free limit = 10


def test_enforce_batch_size_exactly_at_limit_no_exception():
    """Task count exactly at the plan limit is allowed."""
    from core.quotas import enforce_batch_size
    enforce_batch_size("free", 10)   # free limit = 10


def test_enforce_batch_size_one_over_limit_raises_400():
    """Task count one over limit raises HTTP 400."""
    from core.quotas import enforce_batch_size
    with pytest.raises(HTTPException) as exc_info:
        enforce_batch_size("free", 11)   # free limit = 10
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "batch_size_exceeded"


def test_enforce_batch_size_well_over_limit_raises_400():
    """Large batch count raises HTTP 400 with informative detail."""
    from core.quotas import enforce_batch_size
    with pytest.raises(HTTPException) as exc_info:
        enforce_batch_size("starter", 100)   # starter limit = 25
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["limit"] == 25
    assert "upgrade_url" in detail


def test_enforce_batch_size_free_plan_limit_10():
    """Free plan limit is exactly 10 — 10 passes, 11 fails."""
    from core.quotas import enforce_batch_size
    enforce_batch_size("free", 10)
    with pytest.raises(HTTPException):
        enforce_batch_size("free", 11)


def test_enforce_batch_size_enterprise_limit_50():
    """Enterprise batch limit is 50."""
    from core.quotas import enforce_batch_size
    enforce_batch_size("enterprise", 50)
    with pytest.raises(HTTPException):
        enforce_batch_size("enterprise", 51)


def test_enforce_batch_size_error_detail_contains_plan_name():
    """Error detail message includes the plan display name."""
    from core.quotas import enforce_batch_size
    with pytest.raises(HTTPException) as exc_info:
        enforce_batch_size("starter", 99)
    assert "Starter" in exc_info.value.detail["message"]


# ── PLAN_QUOTAS structural integrity ─────────────────────────────────────────

def test_plan_quotas_has_all_four_plans():
    from core.quotas import PLAN_QUOTAS
    assert set(PLAN_QUOTAS.keys()) >= {"free", "starter", "pro", "enterprise"}


def test_plan_quotas_all_plans_have_required_keys():
    """Every plan entry contains all expected quota keys."""
    from core.quotas import PLAN_QUOTAS
    required_keys = {
        "tasks_per_day",
        "tasks_per_minute",
        "pipelines_total",
        "pipeline_runs_per_day",
        "batch_task_size",
        "max_worker_assignments",
    }
    for plan, quotas in PLAN_QUOTAS.items():
        missing = required_keys - set(quotas.keys())
        assert not missing, f"Plan '{plan}' missing quota keys: {missing}"


def test_plan_quotas_enterprise_unlimited_fields_are_none():
    """Enterprise plan has None for the three unlimited fields."""
    from core.quotas import PLAN_QUOTAS
    ent = PLAN_QUOTAS["enterprise"]
    assert ent["tasks_per_day"] is None
    assert ent["pipelines_total"] is None
    assert ent["pipeline_runs_per_day"] is None


def test_plan_quotas_pro_pipelines_total_is_none():
    """Pro plan has unlimited pipelines (None)."""
    from core.quotas import PLAN_QUOTAS
    assert PLAN_QUOTAS["pro"]["pipelines_total"] is None


def test_plan_quotas_free_has_no_none_limits():
    """Free plan has concrete (non-None) limits for all quota keys."""
    from core.quotas import PLAN_QUOTAS
    for key, value in PLAN_QUOTAS["free"].items():
        assert value is not None, f"Free plan quota '{key}' should not be None"


def test_plan_quotas_numeric_limits_are_positive():
    """All non-None quota values are positive integers."""
    from core.quotas import PLAN_QUOTAS
    for plan, quotas in PLAN_QUOTAS.items():
        for key, value in quotas.items():
            if value is not None:
                assert value > 0, f"Plan '{plan}' quota '{key}' is non-positive: {value}"


def test_plan_quotas_tasks_per_day_increases_free_to_pro():
    """tasks_per_day limit strictly increases: free < starter < pro."""
    from core.quotas import PLAN_QUOTAS
    free_limit    = PLAN_QUOTAS["free"]["tasks_per_day"]
    starter_limit = PLAN_QUOTAS["starter"]["tasks_per_day"]
    pro_limit     = PLAN_QUOTAS["pro"]["tasks_per_day"]
    assert free_limit < starter_limit < pro_limit


def test_plan_quotas_pipeline_runs_increases_free_to_pro():
    """pipeline_runs_per_day increases: free < starter < pro."""
    from core.quotas import PLAN_QUOTAS
    assert (
        PLAN_QUOTAS["free"]["pipeline_runs_per_day"]
        < PLAN_QUOTAS["starter"]["pipeline_runs_per_day"]
        < PLAN_QUOTAS["pro"]["pipeline_runs_per_day"]
    )


def test_plan_quotas_max_worker_assignments_increases():
    """max_worker_assignments increases across tiers."""
    from core.quotas import PLAN_QUOTAS
    free_w    = PLAN_QUOTAS["free"]["max_worker_assignments"]
    starter_w = PLAN_QUOTAS["starter"]["max_worker_assignments"]
    pro_w     = PLAN_QUOTAS["pro"]["max_worker_assignments"]
    ent_w     = PLAN_QUOTAS["enterprise"]["max_worker_assignments"]
    assert free_w < starter_w < pro_w < ent_w
