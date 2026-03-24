"""Unit tests for worker streak logic.

Tests the streak_at_risk calculation and related date handling
without requiring a real database connection.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")


# ── streak_at_risk logic ───────────────────────────────────────────────────────
# We replicate and test the exact logic used in the /v1/worker/stats endpoint.

def _compute_streak_at_risk(
    streak_days: int,
    last_active_date: date | None,
    today_utc: date | None = None,
) -> bool:
    """Mirror of the streak_at_risk computation in routers/worker.py."""
    if today_utc is None:
        today_utc = datetime.now(timezone.utc).date()
    if last_active_date is None:
        return False
    return streak_days > 0 and last_active_date < today_utc


def test_streak_at_risk_active_today():
    """Worker who completed a task today should NOT be at risk."""
    today = datetime.now(timezone.utc).date()
    assert _compute_streak_at_risk(5, today) is False


def test_streak_at_risk_not_active_today():
    """Worker with streak who did NOT complete a task today IS at risk."""
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    assert _compute_streak_at_risk(5, yesterday) is True


def test_streak_at_risk_zero_streak_never_at_risk():
    """A worker with no streak cannot be at risk."""
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    assert _compute_streak_at_risk(0, yesterday) is False


def test_streak_at_risk_no_active_date_never_at_risk():
    """Never at risk if there's no recorded activity at all."""
    assert _compute_streak_at_risk(0, None) is False
    assert _compute_streak_at_risk(1, None) is False


def test_streak_at_risk_new_day_boundary():
    """Exactly at midnight UTC: yesterday's activity means at-risk today."""
    today = date(2026, 3, 24)
    yesterday = date(2026, 3, 23)
    assert _compute_streak_at_risk(3, yesterday, today_utc=today) is True
    assert _compute_streak_at_risk(3, today, today_utc=today) is False


def test_streak_at_risk_old_activity():
    """Activity 7 days ago with a streak means at risk."""
    week_ago = datetime.now(timezone.utc).date() - timedelta(days=7)
    assert _compute_streak_at_risk(1, week_ago) is True


def test_streak_at_risk_streak_1_active_today():
    """Brand-new streak of 1 day that was earned today should NOT be at risk."""
    today = datetime.now(timezone.utc).date()
    assert _compute_streak_at_risk(1, today) is False


# ── formatDuration logic (Python mirror of the Astro TS helper) ───────────────

def format_duration(secs: int) -> str:
    """Mirror of the formatDuration TypeScript helper in task detail page."""
    if secs < 60:
        return f"{secs}s"
    m = secs // 60
    s = secs % 60
    if m < 60:
        return f"{m}m {s}s" if s > 0 else f"{m}m"
    h = m // 60
    mm = m % 60
    return f"{h}h {mm}m" if mm > 0 else f"{h}h"


def test_format_duration_seconds_only():
    assert format_duration(0) == "0s"
    assert format_duration(1) == "1s"
    assert format_duration(59) == "59s"


def test_format_duration_minutes_and_seconds():
    assert format_duration(60) == "1m"
    assert format_duration(61) == "1m 1s"
    assert format_duration(90) == "1m 30s"
    assert format_duration(119) == "1m 59s"
    assert format_duration(120) == "2m"


def test_format_duration_hours():
    assert format_duration(3600) == "1h"
    assert format_duration(3660) == "1h 1m"
    assert format_duration(7200) == "2h"
    assert format_duration(7260) == "2h 1m"
    assert format_duration(3599) == "59m 59s"


def test_format_duration_boundaries():
    """Boundary conditions between unit categories."""
    assert format_duration(59) == "59s"   # last second-only value
    assert format_duration(60) == "1m"    # first minute value
    assert format_duration(3599) == "59m 59s"  # last minute+second value
    assert format_duration(3600) == "1h"  # first hour value
