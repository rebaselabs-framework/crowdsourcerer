"""Leaderboard — top workers by XP, tasks completed, or earnings."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from core.auth import get_current_user_id
from core.database import get_db
from models.db import UserDB, TaskAssignmentDB
from models.schemas import LeaderboardOut, LeaderboardEntryOut

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/leaderboard", tags=["leaderboard"])

_PAGE_SIZE = 50


@router.get("", response_model=LeaderboardOut)
async def get_leaderboard(
    category: Literal["xp", "tasks", "earnings"] = Query(
        "xp", description="Sort by: xp | tasks | earnings"
    ),
    period: Literal["all_time", "weekly"] = Query(
        "all_time", description="Time window: all_time | weekly"
    ),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return the top 50 workers for a given category and period."""
    now = datetime.now(timezone.utc)

    if period == "weekly":
        # Weekly: earnings based on credits earned in last 7 days
        week_ago = now - timedelta(days=7)

        if category == "earnings":
            # Sum earnings from approved/submitted assignments in the last 7 days
            subq = (
                select(
                    TaskAssignmentDB.worker_id,
                    func.sum(TaskAssignmentDB.earnings_credits).label("total_earnings"),
                )
                .where(
                    TaskAssignmentDB.status.in_(["submitted", "approved"]),
                    TaskAssignmentDB.submitted_at >= week_ago,
                )
                .group_by(TaskAssignmentDB.worker_id)
                .order_by(desc("total_earnings"))
                .limit(_PAGE_SIZE)
                .subquery()
            )
            result = await db.execute(
                select(UserDB, subq.c.total_earnings)
                .join(subq, UserDB.id == subq.c.worker_id)
                .where(UserDB.role.in_(["worker", "both"]))
                .order_by(desc(subq.c.total_earnings))
            )
            rows = result.all()
            entries = [
                LeaderboardEntryOut(
                    rank=i + 1,
                    user_id=u.id,
                    name=u.name,
                    worker_level=u.worker_level,
                    worker_xp=u.worker_xp,
                    worker_tasks_completed=u.worker_tasks_completed,
                    worker_accuracy=u.worker_accuracy,
                    worker_reliability=u.worker_reliability,
                    worker_streak_days=u.worker_streak_days,
                )
                for i, (u, _earnings) in enumerate(rows)
            ]
            return LeaderboardOut(
                period=period,
                category=category,
                entries=entries,
                generated_at=now,
            )

        # Weekly tasks (submitted in last 7 days)
        if category == "tasks":
            subq = (
                select(
                    TaskAssignmentDB.worker_id,
                    func.count().label("task_count"),
                )
                .where(
                    TaskAssignmentDB.status.in_(["submitted", "approved"]),
                    TaskAssignmentDB.submitted_at >= week_ago,
                )
                .group_by(TaskAssignmentDB.worker_id)
                .order_by(desc("task_count"))
                .limit(_PAGE_SIZE)
                .subquery()
            )
            result = await db.execute(
                select(UserDB, subq.c.task_count)
                .join(subq, UserDB.id == subq.c.worker_id)
                .where(UserDB.role.in_(["worker", "both"]))
                .order_by(desc(subq.c.task_count))
            )
            rows = result.all()
            entries = [
                LeaderboardEntryOut(
                    rank=i + 1,
                    user_id=u.id,
                    name=u.name,
                    worker_level=u.worker_level,
                    worker_xp=u.worker_xp,
                    worker_tasks_completed=u.worker_tasks_completed,
                    worker_accuracy=u.worker_accuracy,
                    worker_reliability=u.worker_reliability,
                    worker_streak_days=u.worker_streak_days,
                )
                for i, (u, _count) in enumerate(rows)
            ]
            return LeaderboardOut(
                period=period,
                category=category,
                entries=entries,
                generated_at=now,
            )

    # All-time / weekly XP: just sort by column on UserDB
    if category == "xp":
        order_col = UserDB.worker_xp
    elif category == "tasks":
        order_col = UserDB.worker_tasks_completed
    else:
        # earnings all-time: use worker_tasks_completed as proxy,
        # since we track earnings via transactions (not on user directly)
        order_col = UserDB.worker_tasks_completed

    result = await db.execute(
        select(UserDB)
        .where(UserDB.role.in_(["worker", "both"]))
        .order_by(desc(order_col))
        .limit(_PAGE_SIZE)
    )
    users = result.scalars().all()

    entries = [
        LeaderboardEntryOut(
            rank=i + 1,
            user_id=u.id,
            name=u.name,
            worker_level=u.worker_level,
            worker_xp=u.worker_xp,
            worker_tasks_completed=u.worker_tasks_completed,
            worker_accuracy=u.worker_accuracy,
            worker_reliability=u.worker_reliability,
            worker_streak_days=u.worker_streak_days,
        )
        for i, u in enumerate(users)
    ]

    return LeaderboardOut(
        period=period,
        category=category,
        entries=entries,
        generated_at=now,
    )
