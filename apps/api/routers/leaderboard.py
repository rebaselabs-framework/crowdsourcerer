"""Leaderboard — top workers by XP, tasks completed, or earnings.

Public endpoint: no authentication required.  If a valid token is supplied
the response marks the caller's own entry so the UI can highlight "you".
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, and_, not_, or_

from core.auth import get_optional_user_id
from core.database import get_db
from models.db import UserDB, TaskAssignmentDB
from models.schemas import LeaderboardOut, LeaderboardEntryOut

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/leaderboard", tags=["leaderboard"])

_PAGE_SIZE = 50

# Exclude test/seed accounts from public leaderboard
_EXCLUDED_EMAIL_PATTERNS = ["%@example.com", "%e2e-test%", "%seed-%"]


def _real_user_filter():
    """SQLAlchemy filter clause that excludes test accounts from leaderboard."""
    return and_(
        *[not_(UserDB.email.ilike(p)) for p in _EXCLUDED_EMAIL_PATTERNS]
    )


def _entry(i: int, u: UserDB, *, total_earnings: int | None = None) -> LeaderboardEntryOut:
    return LeaderboardEntryOut(
        rank=i + 1,
        user_id=u.id,
        name=u.name,
        worker_level=u.worker_level,
        worker_xp=u.worker_xp,
        worker_tasks_completed=u.worker_tasks_completed,
        worker_accuracy=u.worker_accuracy,
        worker_reliability=u.worker_reliability,
        worker_streak_days=u.worker_streak_days,
        total_earnings=total_earnings,
        profile_public=getattr(u, "profile_public", True),
    )


@router.get("", response_model=LeaderboardOut)
async def get_leaderboard(
    category: Literal["xp", "tasks", "earnings"] = Query(
        "xp", description="Sort by: xp | tasks | earnings"
    ),
    period: Literal["all_time", "weekly"] = Query(
        "all_time", description="Time window: all_time | weekly"
    ),
    db: AsyncSession = Depends(get_db),
    caller_id: Optional[str] = Depends(get_optional_user_id),
):
    """Return the top 50 workers for a given category and period.

    Authentication is **optional**.  Supply a Bearer token and the response
    will include ``is_me=true`` on the caller's entry so the UI can highlight it.
    """
    now = datetime.now(timezone.utc)

    if period == "weekly":
        week_ago = now - timedelta(days=7)

        if category == "earnings":
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
                .where(UserDB.role.in_(["worker", "both"]), _real_user_filter())
                .order_by(desc(subq.c.total_earnings))
            )
            rows = result.all()
            entries = [_entry(i, u, total_earnings=int(te or 0)) for i, (u, te) in enumerate(rows)]
            return LeaderboardOut(
                period=period, category=category, entries=entries,
                generated_at=now, caller_id=caller_id,
            )

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
                .where(UserDB.role.in_(["worker", "both"]), _real_user_filter())
                .order_by(desc(subq.c.task_count))
            )
            rows = result.all()
            entries = [_entry(i, u) for i, (u, _) in enumerate(rows)]
            return LeaderboardOut(
                period=period, category=category, entries=entries,
                generated_at=now, caller_id=caller_id,
            )

    # All-time earnings: aggregate from task_assignments
    if category == "earnings":
        subq = (
            select(
                TaskAssignmentDB.worker_id,
                func.coalesce(func.sum(TaskAssignmentDB.earnings_credits), 0).label("total_earnings"),
            )
            .where(TaskAssignmentDB.status.in_(["submitted", "approved"]))
            .group_by(TaskAssignmentDB.worker_id)
            .order_by(desc("total_earnings"))
            .limit(_PAGE_SIZE)
            .subquery()
        )
        result = await db.execute(
            select(UserDB, subq.c.total_earnings)
            .join(subq, UserDB.id == subq.c.worker_id)
            .where(UserDB.role.in_(["worker", "both"]), _real_user_filter())
            .order_by(desc(subq.c.total_earnings))
        )
        rows = result.all()
        entries = [_entry(i, u, total_earnings=int(te or 0)) for i, (u, te) in enumerate(rows)]
        return LeaderboardOut(
            period=period, category=category, entries=entries,
            generated_at=now, caller_id=caller_id,
        )

    # All-time XP or tasks
    if category == "xp":
        order_col = UserDB.worker_xp
    else:
        order_col = UserDB.worker_tasks_completed

    result = await db.execute(
        select(UserDB)
        .where(UserDB.role.in_(["worker", "both"]), _real_user_filter())
        .order_by(desc(order_col))
        .limit(_PAGE_SIZE)
    )
    users = result.scalars().all()
    entries = [_entry(i, u) for i, u in enumerate(users)]

    return LeaderboardOut(
        period=period, category=category, entries=entries,
        generated_at=now, caller_id=caller_id,
    )
