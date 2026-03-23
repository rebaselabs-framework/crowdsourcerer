"""Worker reputation scoring system.

Reputation score (0–100) is a weighted composite of:
  - Accuracy (35%): proportion of approved vs rejected submissions
  - Reliability (25%): proportion of tasks completed vs timed-out/abandoned
  - Volume (15%): logarithmic scaling based on total tasks completed
  - Level / XP (10%): worker gamification level (level 1–20)
  - Certifications (10%): earned certifications add bonus points
  - Streak (5%): active streak days add a small bonus

Modifiers:
  - Strikes subtract from the score (warning: -2, minor: -5, major: -15, critical: -30)
  - Banned workers have reputation 0 (enforced at query time)
"""
from __future__ import annotations

import math
from typing import Optional
from uuid import UUID

import structlog
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import UserDB, WorkerCertificationDB, WorkerStrikeDB

logger = structlog.get_logger()

STRIKE_PENALTIES = {
    "warning": 2,
    "minor": 5,
    "major": 15,
    "critical": 30,
}


async def compute_reputation(worker: UserDB, db: AsyncSession) -> float:
    """Compute and return the reputation score (0–100) for a worker.

    This does NOT save the value — call `refresh_worker_reputation()` for that.
    """
    if worker.is_banned:
        return 0.0

    # ── 1. Accuracy score (0–100, weight 35%) ───────────────────────────────
    accuracy_raw = worker.worker_accuracy or 0.5  # default to 0.5 when no data
    # Temper: penalise workers with very few tasks (no data)
    task_count = worker.worker_tasks_completed or 0
    confidence = min(task_count / 20.0, 1.0)  # full confidence after 20 tasks
    accuracy_score = (accuracy_raw * confidence + 0.5 * (1 - confidence)) * 100

    # ── 2. Reliability score (0–100, weight 25%) ────────────────────────────
    reliability_raw = worker.worker_reliability or 0.8  # default 80%
    reliability_score = reliability_raw * 100

    # ── 3. Volume score (0–100, weight 15%) — log scale ─────────────────────
    volume_score = min(math.log1p(task_count) / math.log1p(1000) * 100, 100)

    # ── 4. Level score (0–100, weight 10%) ──────────────────────────────────
    level = worker.worker_level or 1
    level_score = ((level - 1) / 19) * 100  # level 1 = 0, level 20 = 100

    # ── 5. Certification bonus (0–100, weight 10%) ──────────────────────────
    cert_count_result = await db.scalar(
        select(func.count()).where(
            WorkerCertificationDB.worker_id == worker.id,
            WorkerCertificationDB.passed == True,  # noqa: E712
        )
    ) or 0
    cert_score = min(cert_count_result * 25, 100)  # 4 certs = 100

    # ── 6. Streak bonus (0–100, weight 5%) ──────────────────────────────────
    streak = worker.worker_streak_days or 0
    streak_score = min(streak / 30 * 100, 100)  # 30 days = 100

    # ── Weighted composite ───────────────────────────────────────────────────
    raw = (
        accuracy_score * 0.35
        + reliability_score * 0.25
        + volume_score * 0.15
        + level_score * 0.10
        + cert_score * 0.10
        + streak_score * 0.05
    )

    # ── Strike penalties (subtract, floor at 0) ──────────────────────────────
    active_strikes_result = await db.execute(
        select(WorkerStrikeDB.severity).where(
            WorkerStrikeDB.worker_id == worker.id,
            WorkerStrikeDB.is_active == True,  # noqa: E712
        )
    )
    active_strikes = active_strikes_result.scalars().all()
    total_penalty = sum(STRIKE_PENALTIES.get(s, 5) for s in active_strikes)

    final = max(0.0, min(100.0, raw - total_penalty))
    return round(final, 2)


async def refresh_worker_reputation(worker_id: UUID, db: AsyncSession) -> float:
    """Recompute and persist reputation_score for a worker. Returns the new score."""
    result = await db.execute(select(UserDB).where(UserDB.id == worker_id))
    worker = result.scalar_one_or_none()
    if not worker:
        return 0.0

    score = await compute_reputation(worker, db)
    worker.reputation_score = score
    await db.flush()
    return score


def reputation_tier(score: float) -> str:
    """Return a human-readable tier label for a reputation score."""
    if score >= 90:
        return "Elite"
    elif score >= 75:
        return "Expert"
    elif score >= 60:
        return "Proficient"
    elif score >= 40:
        return "Developing"
    elif score >= 20:
        return "Novice"
    else:
        return "Untrusted"


def reputation_color(score: float) -> str:
    """Return a CSS color class for the score tier."""
    if score >= 90:
        return "text-yellow-400"
    elif score >= 75:
        return "text-green-400"
    elif score >= 60:
        return "text-blue-400"
    elif score >= 40:
        return "text-gray-300"
    elif score >= 20:
        return "text-orange-400"
    else:
        return "text-red-400"
