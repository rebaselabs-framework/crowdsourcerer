"""Public platform statistics — no authentication required.

Exposes aggregate, anonymised metrics so the landing page and marketing
materials can show live platform activity without leaking per-user data.

All queries are fast (indexed columns only) and results are cached for
5 minutes to avoid hammering the DB from high-traffic pages.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from models.db import TaskDB, UserDB, TaskAssignmentDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/platform", tags=["platform"])

# ── Simple in-process cache (5-minute TTL) ───────────────────────────────

_cache: dict = {}
_cache_at: Optional[datetime] = None
_CACHE_TTL_S = 300


# ── Response schema ───────────────────────────────────────────────────────

class TaskTypeStats(BaseModel):
    task_type: str
    completed: int
    avg_completion_ms: Optional[int]


class PlatformStats(BaseModel):
    tasks_completed_total: int
    tasks_completed_today: int
    tasks_completed_this_week: int
    active_workers_30d: int
    total_requesters: int
    avg_completion_ms_overall: Optional[int]
    top_task_types: list[TaskTypeStats]
    tasks_running_now: int
    platform_uptime_note: str


# ── Endpoint ──────────────────────────────────────────────────────────────

@router.get(
    "/stats",
    response_model=PlatformStats,
    summary="Public platform statistics",
    description=(
        "Returns aggregated, anonymised platform metrics. No authentication required. "
        "Results are cached for 5 minutes."
    ),
)
async def get_platform_stats(db: AsyncSession = Depends(get_db)) -> PlatformStats:
    global _cache, _cache_at

    now = datetime.now(timezone.utc)

    # Return cached result if fresh enough
    if _cache_at and (now - _cache_at).total_seconds() < _CACHE_TTL_S and _cache:
        return PlatformStats(**_cache)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    # ── Combined task completion stats (3 queries → 1) ────────────────────
    # Uses PostgreSQL FILTER clause via SQLAlchemy to compute total, today,
    # and this-week counts plus avg duration in a single table scan.
    combined_q = await db.execute(
        select(
            func.count(TaskDB.id).label("total"),
            func.count(TaskDB.id).filter(
                TaskDB.completed_at >= today_start
            ).label("today"),
            func.count(TaskDB.id).filter(
                TaskDB.completed_at >= week_start
            ).label("this_week"),
            func.avg(TaskDB.duration_ms).filter(
                TaskDB.duration_ms.isnot(None),
                TaskDB.duration_ms > 0,
            ).label("avg_ms"),
        ).where(TaskDB.status == "completed")
    )
    stats_row = combined_q.one()
    tasks_completed_total: int = stats_row.total or 0
    tasks_completed_today: int = stats_row.today or 0
    tasks_completed_this_week: int = stats_row.this_week or 0
    avg_completion_ms_overall: Optional[int] = int(stats_row.avg_ms) if stats_row.avg_ms else None

    # ── Active workers last 30 days ────────────────────────────────────────
    workers_q = await db.execute(
        select(func.count(func.distinct(TaskAssignmentDB.worker_id))).where(
            TaskAssignmentDB.claimed_at >= month_start,
            TaskAssignmentDB.status.in_(["approved", "submitted"]),
        )
    )
    active_workers_30d: int = workers_q.scalar_one() or 0

    # ── Total requesters (users who ever created a task) ───────────────────
    requesters_q = await db.execute(
        select(func.count(func.distinct(TaskDB.user_id)))
    )
    total_requesters: int = requesters_q.scalar_one() or 0

    # ── Top task types by completion count ────────────────────────────────
    top_types_q = await db.execute(
        select(
            TaskDB.type,
            func.count(TaskDB.id).label("cnt"),
            func.avg(TaskDB.duration_ms).label("avg_ms"),
        )
        .where(TaskDB.status == "completed")
        .group_by(TaskDB.type)
        .order_by(func.count(TaskDB.id).desc())
        .limit(5)
    )
    top_task_types = [
        TaskTypeStats(
            task_type=row.type,
            completed=row.cnt,
            avg_completion_ms=int(row.avg_ms) if row.avg_ms else None,
        )
        for row in top_types_q.all()
    ]

    # ── Tasks running right now ────────────────────────────────────────────
    running_q = await db.execute(
        select(func.count(TaskDB.id)).where(
            TaskDB.status.in_(["running", "queued", "assigned"])
        )
    )
    tasks_running_now: int = running_q.scalar_one() or 0

    result = PlatformStats(
        tasks_completed_total=tasks_completed_total,
        tasks_completed_today=tasks_completed_today,
        tasks_completed_this_week=tasks_completed_this_week,
        active_workers_30d=active_workers_30d,
        total_requesters=total_requesters,
        avg_completion_ms_overall=avg_completion_ms_overall,
        top_task_types=top_task_types,
        tasks_running_now=tasks_running_now,
        platform_uptime_note="Platform is operational",
    )

    _cache = result.model_dump()
    _cache_at = now

    return result
