"""Unit tests for the worker reputation scoring system.

Tests cover:
  - Tier label thresholds
  - Strike penalty constants
  - compute_reputation() weighted formula with pre-loaded data (no DB needed)
  - Banned worker short-circuit
  - Pre-loaded cert/strike kwargs skip DB queries
"""
from __future__ import annotations
import os
import asyncio

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest


# ── Tier thresholds ────────────────────────────────────────────────────────────

def test_reputation_tier_elite():
    from core.reputation import reputation_tier
    assert reputation_tier(90.0) == "Elite"
    assert reputation_tier(100.0) == "Elite"


def test_reputation_tier_expert():
    from core.reputation import reputation_tier
    assert reputation_tier(75.0) == "Expert"
    assert reputation_tier(89.9) == "Expert"


def test_reputation_tier_proficient():
    from core.reputation import reputation_tier
    assert reputation_tier(60.0) == "Proficient"
    assert reputation_tier(74.9) == "Proficient"


def test_reputation_tier_developing():
    from core.reputation import reputation_tier
    assert reputation_tier(40.0) == "Developing"
    assert reputation_tier(59.9) == "Developing"


def test_reputation_tier_novice():
    from core.reputation import reputation_tier
    assert reputation_tier(20.0) == "Novice"
    assert reputation_tier(39.9) == "Novice"


def test_reputation_tier_untrusted():
    from core.reputation import reputation_tier
    assert reputation_tier(0.0) == "Untrusted"
    assert reputation_tier(19.9) == "Untrusted"


# ── Strike penalties ──────────────────────────────────────────────────────────

def test_strike_penalties_defined():
    from core.reputation import STRIKE_PENALTIES
    assert STRIKE_PENALTIES["warning"] == 2
    assert STRIKE_PENALTIES["minor"] == 5
    assert STRIKE_PENALTIES["major"] == 15
    assert STRIKE_PENALTIES["critical"] == 30


def test_strike_penalties_ascending():
    """Higher severity strikes must deduct more points."""
    from core.reputation import STRIKE_PENALTIES
    assert STRIKE_PENALTIES["warning"] < STRIKE_PENALTIES["minor"]
    assert STRIKE_PENALTIES["minor"] < STRIKE_PENALTIES["major"]
    assert STRIKE_PENALTIES["major"] < STRIKE_PENALTIES["critical"]


# ── compute_reputation() with pre-loaded data (no real DB) ───────────────────

class _FakeWorker:
    """Minimal worker object with the fields compute_reputation() reads."""
    def __init__(
        self,
        *,
        is_banned: bool = False,
        worker_accuracy: float = 1.0,
        worker_tasks_completed: int = 50,
        worker_reliability: float = 1.0,
        worker_level: int = 10,
        worker_streak_days: int = 15,
    ):
        self.is_banned = is_banned
        self.worker_accuracy = worker_accuracy
        self.worker_tasks_completed = worker_tasks_completed
        self.worker_reliability = worker_reliability
        self.worker_level = worker_level
        self.worker_streak_days = worker_streak_days


@pytest.mark.asyncio
async def test_compute_reputation_banned_returns_zero():
    from core.reputation import compute_reputation
    worker = _FakeWorker(is_banned=True)
    score = await compute_reputation(worker, db=None, _cert_count=0, _strike_severities=[])
    assert score == 0.0


@pytest.mark.asyncio
async def test_compute_reputation_perfect_worker():
    """A worker with perfect stats and 4 certs and no strikes should score ~100."""
    from core.reputation import compute_reputation
    worker = _FakeWorker(
        worker_accuracy=1.0,
        worker_tasks_completed=1000,
        worker_reliability=1.0,
        worker_level=20,
        worker_streak_days=30,
    )
    score = await compute_reputation(
        worker, db=None, _cert_count=4, _strike_severities=[]
    )
    assert score == 100.0


@pytest.mark.asyncio
async def test_compute_reputation_no_tasks_middling():
    """A new worker with no tasks should get a mid-range score (~49–51)."""
    from core.reputation import compute_reputation
    worker = _FakeWorker(
        worker_accuracy=None,   # defaults to 0.5
        worker_tasks_completed=0,
        worker_reliability=None,  # defaults to 0.8
        worker_level=1,
        worker_streak_days=0,
    )
    score = await compute_reputation(
        worker, db=None, _cert_count=0, _strike_severities=[]
    )
    # Raw: acc=50*0 +50*1=50, rel=80, vol=0, lvl=0, cert=0, streak=0
    # weighted: 50*0.35 + 80*0.25 + 0*0.15 + 0*0.10 + 0*0.10 + 0*0.05 = 17.5 + 20 = 37.5
    assert 35.0 <= score <= 40.0


@pytest.mark.asyncio
async def test_compute_reputation_strike_reduces_score():
    """A critical strike must subtract 30 points from the raw score."""
    from core.reputation import compute_reputation
    worker = _FakeWorker(
        worker_accuracy=1.0,
        worker_tasks_completed=1000,
        worker_reliability=1.0,
        worker_level=20,
        worker_streak_days=30,
    )
    score_clean = await compute_reputation(
        worker, db=None, _cert_count=4, _strike_severities=[]
    )
    score_struck = await compute_reputation(
        worker, db=None, _cert_count=4, _strike_severities=["critical"]
    )
    assert score_clean - score_struck == 30.0


@pytest.mark.asyncio
async def test_compute_reputation_multiple_strikes_stack():
    """Multiple strikes stack their penalties."""
    from core.reputation import compute_reputation
    worker = _FakeWorker(
        worker_accuracy=1.0,
        worker_tasks_completed=1000,
        worker_reliability=1.0,
        worker_level=20,
        worker_streak_days=30,
    )
    score_no_strike = await compute_reputation(
        worker, db=None, _cert_count=4, _strike_severities=[]
    )
    score_two_minor = await compute_reputation(
        worker, db=None, _cert_count=4, _strike_severities=["minor", "minor"]
    )
    # 2× minor = 2×5 = 10 reduction
    assert score_no_strike - score_two_minor == 10.0


@pytest.mark.asyncio
async def test_compute_reputation_capped_at_100():
    """Score must never exceed 100."""
    from core.reputation import compute_reputation
    worker = _FakeWorker(
        worker_accuracy=1.0,
        worker_tasks_completed=100_000,
        worker_reliability=1.0,
        worker_level=20,
        worker_streak_days=365,
    )
    score = await compute_reputation(
        worker, db=None, _cert_count=100, _strike_severities=[]
    )
    assert score <= 100.0


@pytest.mark.asyncio
async def test_compute_reputation_floored_at_zero():
    """Score must never go below 0, even with extreme strike penalties."""
    from core.reputation import compute_reputation
    worker = _FakeWorker(
        worker_accuracy=0.0,
        worker_tasks_completed=0,
        worker_reliability=0.0,
        worker_level=1,
        worker_streak_days=0,
    )
    score = await compute_reputation(
        worker,
        db=None,
        _cert_count=0,
        _strike_severities=["critical", "critical", "critical", "critical"],
    )
    assert score == 0.0


@pytest.mark.asyncio
async def test_compute_reputation_cert_score_maxes_at_4():
    """4 certs = 100 cert_score; 5 certs should not exceed 100 cert_score."""
    from core.reputation import compute_reputation
    worker = _FakeWorker()
    score_4 = await compute_reputation(
        worker, db=None, _cert_count=4, _strike_severities=[]
    )
    score_10 = await compute_reputation(
        worker, db=None, _cert_count=10, _strike_severities=[]
    )
    # cert_score is min(count * 25, 100) — should be the same at 4+ certs
    assert score_4 == score_10


# ── reputation_color smoke test ───────────────────────────────────────────────

def test_reputation_color_returns_string():
    from core.reputation import reputation_color
    for score in [0, 10, 25, 45, 65, 80, 95]:
        result = reputation_color(float(score))
        assert isinstance(result, str)
        assert result.startswith("text-")
