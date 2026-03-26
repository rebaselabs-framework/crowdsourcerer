"""
core/matching.py — Skill-based worker ↔ task matching.

Computes a match score (0.0–1.0) for a worker against an open task based on:
  - Worker proficiency level in the task type       (40% weight)
  - Worker accuracy in the task type                (30% weight)
  - Worker reputation score                         (20% weight)
  - Freshness bonus (recently active workers)       (10% weight)

Also enforces hard constraints:
  - task.min_skill_level: worker proficiency must be >= this (if set)
  - task.min_reputation_score: worker reputation must be >= this (if set)
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_

from models.db import TaskDB, UserDB, WorkerSkillDB


# ── Weights ──────────────────────────────────────────────────────────────────
_W_PROFICIENCY = 0.40
_W_ACCURACY = 0.30
_W_REPUTATION = 0.20
_W_FRESHNESS = 0.10

# Max proficiency level (for normalisation)
_MAX_PROF = 5.0


def compute_match_score(
    *,
    proficiency_level: int,         # 1–5
    accuracy: Optional[float],      # 0.0–1.0, or None if no data
    reputation_score: Optional[float],  # 0.0–100.0, or None
    last_task_at: Optional[datetime],
    match_weight: float = 1.0,
    min_skill_level: Optional[int] = None,
    min_reputation_score: Optional[float] = None,
) -> Optional[float]:
    """
    Return a 0.0–1.0 match score, or None if hard constraints are not met.

    None means this worker is ineligible for the task.
    """
    # Hard constraints
    if min_skill_level is not None and proficiency_level < min_skill_level:
        return None
    if min_reputation_score is not None:
        rep = reputation_score or 0.0
        if rep < min_reputation_score:
            return None

    # Soft scores
    prof_score = (proficiency_level - 1) / (_MAX_PROF - 1)  # 0.0–1.0

    acc_score = accuracy if accuracy is not None else 0.5  # neutral if no data

    rep_score = (reputation_score or 50.0) / 100.0  # normalise 0–100 → 0–1

    # Freshness: decays with days since last task; 0 days = 1.0, 30+ days = 0.0
    if last_task_at:
        now = datetime.now(timezone.utc)
        days_since = max(0, (now - last_task_at).total_seconds() / 86400)
        freshness = max(0.0, 1.0 - days_since / 30.0)
    else:
        freshness = 0.0

    raw = (
        _W_PROFICIENCY * prof_score
        + _W_ACCURACY * acc_score
        + _W_REPUTATION * rep_score
        + _W_FRESHNESS * freshness
    )

    # Apply per-skill match_weight modifier (clamped to 0–2)
    # Use explicit None check — `match_weight or 1.0` would incorrectly
    # replace 0.0 (falsy) with 1.0.
    weight = max(0.0, min(2.0, match_weight if match_weight is not None else 1.0))
    return min(1.0, raw * weight)


async def score_worker_for_task(
    db: AsyncSession,
    *,
    worker: UserDB,
    task: TaskDB,
) -> Optional[float]:
    """
    Fetch the worker's skill profile for the task type and return a match score.
    Returns None if the worker fails hard constraints.
    """
    skill_result = await db.execute(
        select(WorkerSkillDB).where(
            WorkerSkillDB.worker_id == worker.id,
            WorkerSkillDB.task_type == task.type,
        )
    )
    skill = skill_result.scalar_one_or_none()

    proficiency = skill.proficiency_level if skill else 1
    accuracy = skill.accuracy if skill else None
    last_task_at = skill.last_task_at if skill else None
    match_weight = skill.match_weight if skill else 1.0

    return compute_match_score(
        proficiency_level=proficiency,
        accuracy=accuracy,
        reputation_score=worker.reputation_score if hasattr(worker, "reputation_score") else None,
        last_task_at=last_task_at,
        match_weight=match_weight,
        min_skill_level=task.min_skill_level,
        min_reputation_score=task.min_reputation_score,
    )


async def rank_tasks_for_worker(
    db: AsyncSession,
    *,
    worker: UserDB,
    tasks: list[TaskDB],
) -> list[tuple[TaskDB, float]]:
    """
    Score all given tasks for the worker and return them sorted by match score
    descending. Tasks where the worker is ineligible are excluded.

    For workers who have declared skill interests but no earned proficiency in a
    task type, the interest acts as a 1.5× match_weight boost — enough to surface
    those tasks in the feed without over-riding earned-proficiency signals.
    """
    scored: list[tuple[TaskDB, float]] = []

    # Preload all skill rows for this worker in one query
    task_types = list({t.type for t in tasks})
    skills_result = await db.execute(
        select(WorkerSkillDB).where(
            WorkerSkillDB.worker_id == worker.id,
            WorkerSkillDB.task_type.in_(task_types),
        )
    )
    skill_map = {s.task_type: s for s in skills_result.scalars().all()}

    rep_score = getattr(worker, "reputation_score", None)

    # Build a set of declared interests for O(1) lookup
    interests: set[str] = set(getattr(worker, "worker_skill_interests", None) or [])

    for task in tasks:
        skill = skill_map.get(task.type)
        proficiency = skill.proficiency_level if skill else 1
        accuracy = skill.accuracy if skill else None
        last_task_at = skill.last_task_at if skill else None

        # Base match weight from earned proficiency record, or 1.0 if no record
        base_weight = (skill.match_weight if skill else 1.0) or 1.0

        # Interest boost: if the worker declared interest in this type and has no
        # earned proficiency yet, apply a 1.5× boost to seed their feed.
        if not skill and task.type in interests:
            base_weight = 1.5

        score = compute_match_score(
            proficiency_level=proficiency,
            accuracy=accuracy,
            reputation_score=rep_score,
            last_task_at=last_task_at,
            match_weight=base_weight,
            min_skill_level=task.min_skill_level,
            min_reputation_score=task.min_reputation_score,
        )
        if score is not None:
            scored.append((task, score))

    # Sort by score descending, then by reward descending as tiebreaker
    scored.sort(key=lambda x: (x[1], x[0].worker_reward_credits or 0), reverse=True)
    return scored
