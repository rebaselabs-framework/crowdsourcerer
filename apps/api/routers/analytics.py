"""Requester analytics — per-user, per-org cost and task breakdowns."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta, date
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, text, case

from core.auth import get_current_user_id
from core.database import get_db
from models.db import TaskDB, CreditTransactionDB, OrganizationDB, OrgMemberDB, UserDB
from models.schemas import RequesterOverviewOut, OrgAnalyticsOut, CostBreakdownOut

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/analytics", tags=["analytics"])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Overview ───────────────────────────────────────────────────────────────────

@router.get("/overview", response_model=RequesterOverviewOut)
async def requester_overview(
    days: int = Query(30, ge=1, le=365),
    org_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Overview of your tasks and credit usage."""
    uid = UUID(user_id)
    since = utcnow() - timedelta(days=days)

    # Base query filter
    filters = [TaskDB.user_id == uid]
    if org_id:
        # Verify membership
        mem = await db.execute(
            select(OrgMemberDB).where(
                OrgMemberDB.org_id == org_id,
                OrgMemberDB.user_id == uid,
            )
        )
        if not mem.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Not a member of this organization")
        filters = [TaskDB.org_id == org_id]

    # Total tasks
    total_tasks = (await db.execute(
        select(func.count()).select_from(TaskDB).where(*filters)
    )).scalar() or 0

    tasks_completed = (await db.execute(
        select(func.count()).select_from(TaskDB).where(*filters, TaskDB.status == "completed")
    )).scalar() or 0

    tasks_failed = (await db.execute(
        select(func.count()).select_from(TaskDB).where(*filters, TaskDB.status == "failed")
    )).scalar() or 0

    tasks_pending = (await db.execute(
        select(func.count()).select_from(TaskDB).where(
            *filters, TaskDB.status.in_(["pending", "queued", "running", "open", "assigned"])
        )
    )).scalar() or 0

    # Credits spent
    txn_filters = [CreditTransactionDB.user_id == uid, CreditTransactionDB.amount < 0]
    if org_id:
        txn_filters = [
            CreditTransactionDB.user_id.in_(
                select(OrgMemberDB.user_id).where(OrgMemberDB.org_id == org_id)
            ),
            CreditTransactionDB.amount < 0,
            CreditTransactionDB.type == "charge",
        ]

    total_credits_spent = (await db.execute(
        select(func.coalesce(func.sum(func.abs(CreditTransactionDB.amount)), 0))
        .where(*txn_filters)
    )).scalar() or 0

    # Avg completion time
    avg_time_result = await db.execute(
        select(
            func.avg(
                func.extract("epoch", TaskDB.completed_at - TaskDB.started_at) / 60
            )
        ).where(*filters, TaskDB.completed_at.isnot(None), TaskDB.started_at.isnot(None))
    )
    avg_time = avg_time_result.scalar()

    # Tasks by type
    type_result = await db.execute(
        select(TaskDB.type, func.count().label("cnt"))
        .where(*filters)
        .group_by(TaskDB.type)
        .order_by(func.count().desc())
    )
    tasks_by_type = {row.type: row.cnt for row in type_result}

    # Tasks by status
    status_result = await db.execute(
        select(TaskDB.status, func.count().label("cnt"))
        .where(*filters)
        .group_by(TaskDB.status)
    )
    tasks_by_status = {row.status: row.cnt for row in status_result}

    # Tasks per day (last N days)
    daily_result = await db.execute(
        select(
            func.date_trunc("day", TaskDB.created_at).label("day"),
            func.count().label("cnt"),
        )
        .where(*filters, TaskDB.created_at >= since)
        .group_by(func.date_trunc("day", TaskDB.created_at))
        .order_by(func.date_trunc("day", TaskDB.created_at))
    )
    tasks_last_n_days = [
        {"date": row.day.strftime("%Y-%m-%d"), "count": row.cnt}
        for row in daily_result
    ]

    return RequesterOverviewOut(
        total_tasks=total_tasks,
        tasks_completed=tasks_completed,
        tasks_pending=tasks_pending,
        tasks_failed=tasks_failed,
        total_credits_spent=total_credits_spent,
        avg_completion_time_minutes=round(avg_time, 1) if avg_time else None,
        tasks_by_type=tasks_by_type,
        tasks_by_status=tasks_by_status,
        tasks_last_30_days=tasks_last_n_days,
    )


# ── Org Analytics ──────────────────────────────────────────────────────────────

@router.get("/org/{org_id}", response_model=OrgAnalyticsOut)
async def org_analytics(
    org_id: UUID,
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Per-organization analytics — members, task volume, cost."""
    uid = UUID(user_id)

    # Verify org membership
    org_result = await db.execute(select(OrganizationDB).where(OrganizationDB.id == org_id))
    org = org_result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    mem_result = await db.execute(
        select(OrgMemberDB).where(OrgMemberDB.org_id == org_id, OrgMemberDB.user_id == uid)
    )
    if not mem_result.scalar_one_or_none() and str(org.owner_id) != user_id:
        raise HTTPException(status_code=403, detail="Not a member of this organization")

    since = utcnow() - timedelta(days=days)

    # Total tasks in org
    total_tasks = (await db.execute(
        select(func.count()).select_from(TaskDB).where(TaskDB.org_id == org_id)
    )).scalar() or 0

    tasks_completed = (await db.execute(
        select(func.count()).select_from(TaskDB)
        .where(TaskDB.org_id == org_id, TaskDB.status == "completed")
    )).scalar() or 0

    credits_spent = (await db.execute(
        select(func.coalesce(func.sum(func.abs(CreditTransactionDB.amount)), 0))
        .where(
            CreditTransactionDB.amount < 0,
            CreditTransactionDB.type == "charge",
            CreditTransactionDB.user_id.in_(
                select(OrgMemberDB.user_id).where(OrgMemberDB.org_id == org_id)
            ),
        )
    )).scalar() or 0

    # Member activity
    members_result = await db.execute(
        select(OrgMemberDB, UserDB)
        .join(UserDB, OrgMemberDB.user_id == UserDB.id)
        .where(OrgMemberDB.org_id == org_id)
    )
    members = members_result.all()

    member_activity = []
    for mem, user in members:
        member_tasks = (await db.execute(
            select(func.count()).select_from(TaskDB)
            .where(TaskDB.user_id == user.id, TaskDB.org_id == org_id)
        )).scalar() or 0

        member_credits = (await db.execute(
            select(func.coalesce(func.sum(func.abs(CreditTransactionDB.amount)), 0))
            .where(
                CreditTransactionDB.user_id == user.id,
                CreditTransactionDB.amount < 0,
                CreditTransactionDB.type == "charge",
            )
        )).scalar() or 0

        member_activity.append({
            "user_id": str(user.id),
            "name": user.name or user.email.split("@")[0],
            "email": user.email,
            "role": mem.role,
            "tasks_created": member_tasks,
            "credits_used": member_credits,
        })

    # Tasks by type
    type_result = await db.execute(
        select(TaskDB.type, func.count().label("cnt"))
        .where(TaskDB.org_id == org_id)
        .group_by(TaskDB.type)
        .order_by(func.count().desc())
    )
    tasks_by_type = {row.type: row.cnt for row in type_result}

    # Daily tasks
    daily_result = await db.execute(
        select(
            func.date_trunc("day", TaskDB.created_at).label("day"),
            func.count().label("cnt"),
        )
        .where(TaskDB.org_id == org_id, TaskDB.created_at >= since)
        .group_by(func.date_trunc("day", TaskDB.created_at))
        .order_by(func.date_trunc("day", TaskDB.created_at))
    )
    tasks_last_n_days = [
        {"date": row.day.strftime("%Y-%m-%d"), "count": row.cnt}
        for row in daily_result
    ]

    return OrgAnalyticsOut(
        org_id=org_id,
        org_name=org.name,
        total_tasks=total_tasks,
        tasks_completed=tasks_completed,
        credits_spent=credits_spent,
        member_activity=member_activity,
        tasks_by_type=tasks_by_type,
        tasks_last_30_days=tasks_last_n_days,
    )


# ── Cost Breakdown ─────────────────────────────────────────────────────────────

@router.get("/costs", response_model=CostBreakdownOut)
async def cost_breakdown(
    months: int = Query(6, ge=1, le=24),
    org_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Detailed credit cost breakdown by task type, execution mode, and time."""
    uid = UUID(user_id)
    since = utcnow() - timedelta(days=months * 30)

    # Set up task filters
    task_filters = [TaskDB.user_id == uid, TaskDB.credits_used.isnot(None)]
    if org_id:
        mem_result = await db.execute(
            select(OrgMemberDB).where(OrgMemberDB.org_id == org_id, OrgMemberDB.user_id == uid)
        )
        if not mem_result.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Not a member of this organization")
        task_filters = [TaskDB.org_id == org_id, TaskDB.credits_used.isnot(None)]

    # Total credits
    total_credits = (await db.execute(
        select(func.coalesce(func.sum(TaskDB.credits_used), 0)).where(*task_filters)
    )).scalar() or 0

    # By task type
    type_result = await db.execute(
        select(TaskDB.type, func.sum(TaskDB.credits_used).label("total"))
        .where(*task_filters)
        .group_by(TaskDB.type)
        .order_by(func.sum(TaskDB.credits_used).desc())
    )
    by_type = {row.type: row.total for row in type_result}

    # By execution mode
    mode_result = await db.execute(
        select(TaskDB.execution_mode, func.sum(TaskDB.credits_used).label("total"))
        .where(*task_filters)
        .group_by(TaskDB.execution_mode)
    )
    by_execution_mode = {row.execution_mode: row.total for row in mode_result}

    # By month
    monthly_result = await db.execute(
        select(
            func.date_trunc("month", TaskDB.created_at).label("month"),
            func.sum(TaskDB.credits_used).label("credits"),
        )
        .where(*task_filters, TaskDB.created_at >= since)
        .group_by(func.date_trunc("month", TaskDB.created_at))
        .order_by(func.date_trunc("month", TaskDB.created_at))
    )
    by_month = [
        {"month": row.month.strftime("%Y-%m"), "credits": row.credits or 0}
        for row in monthly_result
    ]

    # Top task types (combined type + credits + count)
    top_result = await db.execute(
        select(
            TaskDB.type,
            func.sum(TaskDB.credits_used).label("credits"),
            func.count().label("count"),
        )
        .where(*task_filters)
        .group_by(TaskDB.type)
        .order_by(func.sum(TaskDB.credits_used).desc())
        .limit(10)
    )
    top_task_types = [
        {"type": row.type, "credits": row.credits or 0, "count": row.count}
        for row in top_result
    ]

    return CostBreakdownOut(
        total_credits_spent=total_credits,
        by_type=by_type,
        by_execution_mode=by_execution_mode,
        by_month=by_month,
        top_task_types=top_task_types,
    )
