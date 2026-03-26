"""Requester analytics — per-user, per-org cost and task breakdowns."""
from __future__ import annotations
import csv
import io
import json
import statistics
import math
from datetime import datetime, timezone, timedelta, date
from typing import Optional, List
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, text, case

from core.auth import get_current_user_id
from core.scopes import require_scope, SCOPE_ANALYTICS_READ
from core.database import get_db
from models.db import TaskDB, CreditTransactionDB, OrganizationDB, OrgMemberDB, UserDB, TaskAssignmentDB
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
    user_id: str = Depends(require_scope(SCOPE_ANALYTICS_READ)),
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

    # Workers used — distinct workers who've submitted to this requester's tasks
    since_30d = utcnow() - timedelta(days=30)
    workers_used_result = await db.execute(
        select(func.count(func.distinct(TaskAssignmentDB.worker_id)))
        .select_from(TaskAssignmentDB)
        .join(TaskDB, TaskAssignmentDB.task_id == TaskDB.id)
        .where(
            TaskDB.user_id == uid,
            TaskAssignmentDB.status.in_(["submitted", "approved", "rejected"]),
        )
    )
    workers_used = workers_used_result.scalar() or 0

    # Credits spent in the last 30 days specifically
    credits_spent_30d_result = await db.execute(
        select(func.coalesce(func.sum(func.abs(CreditTransactionDB.amount)), 0))
        .where(
            CreditTransactionDB.user_id == uid,
            CreditTransactionDB.amount < 0,
            CreditTransactionDB.created_at >= since_30d,
        )
    )
    credits_spent_30d = credits_spent_30d_result.scalar() or 0

    return RequesterOverviewOut(
        total_tasks=total_tasks,
        tasks_completed=tasks_completed,
        tasks_pending=tasks_pending,
        tasks_failed=tasks_failed,
        total_credits_spent=total_credits_spent,
        credits_spent_30d=int(credits_spent_30d),
        avg_completion_time_minutes=round(avg_time, 1) if avg_time else None,
        workers_used=workers_used,
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
    user_id: str = Depends(require_scope(SCOPE_ANALYTICS_READ)),
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

    # Member activity — cap at 500 to guard against orgs with thousands of members.
    members_result = await db.execute(
        select(OrgMemberDB, UserDB)
        .join(UserDB, OrgMemberDB.user_id == UserDB.id)
        .where(OrgMemberDB.org_id == org_id)
        .limit(500)
    )
    members = members_result.all()

    # Bulk-aggregate task counts and credit spend per member — avoids 2N queries
    member_user_ids = [user.id for _, user in members]
    tasks_by_user: dict = {}
    credits_by_user: dict = {}
    if member_user_ids:
        tc_res = await db.execute(
            select(TaskDB.user_id, func.count().label("cnt"))
            .where(TaskDB.user_id.in_(member_user_ids), TaskDB.org_id == org_id)
            .group_by(TaskDB.user_id)
        )
        tasks_by_user = {str(r.user_id): r.cnt for r in tc_res}

        cr_res = await db.execute(
            select(
                CreditTransactionDB.user_id,
                func.coalesce(func.sum(func.abs(CreditTransactionDB.amount)), 0).label("total"),
            )
            .where(
                CreditTransactionDB.user_id.in_(member_user_ids),
                CreditTransactionDB.amount < 0,
                CreditTransactionDB.type == "charge",
            )
            .group_by(CreditTransactionDB.user_id)
        )
        credits_by_user = {str(r.user_id): r.total for r in cr_res}

    member_activity = []
    for mem, user in members:
        uid = str(user.id)
        member_activity.append({
            "user_id": uid,
            "name": user.name or user.email.split("@")[0],
            "email": user.email,
            "role": mem.role,
            "tasks_created": tasks_by_user.get(uid, 0),
            "credits_used": credits_by_user.get(uid, 0),
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
    user_id: str = Depends(require_scope(SCOPE_ANALYTICS_READ)),
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


# ── Analytics Export ────────────────────────────────────────────────────────────

_EXPORT_COLUMNS = [
    "id", "type", "execution_mode", "status", "priority",
    "credits_used", "duration_ms", "created_at", "started_at", "completed_at",
]


@router.get("/export")
async def export_analytics(
    fmt: str = Query("csv", pattern="^(csv|json)$"),
    days: int = Query(30, ge=1, le=365),
    org_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_ANALYTICS_READ)),
):
    """
    Export task analytics as CSV or JSON.

    Query params:
    - fmt: csv (default) or json
    - days: look-back window (1–365, default 30)
    - org_id: filter to org tasks (must be a member)
    """
    uid = UUID(user_id)
    since = utcnow() - timedelta(days=days)

    # Build filters
    filters = [TaskDB.user_id == uid, TaskDB.created_at >= since]
    if org_id:
        mem_result = await db.execute(
            select(OrgMemberDB).where(OrgMemberDB.org_id == org_id, OrgMemberDB.user_id == uid)
        )
        if not mem_result.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Not a member of this organization")
        filters = [TaskDB.org_id == org_id, TaskDB.created_at >= since]

    result = await db.execute(
        select(TaskDB)
        .where(*filters)
        .order_by(TaskDB.created_at.desc())
        .limit(10_000)
    )
    tasks = result.scalars().all()

    def _fmt_dt(dt: Optional[datetime]) -> str:
        return dt.isoformat() if dt else ""

    rows = [
        {
            "id": str(t.id),
            "type": t.type,
            "execution_mode": t.execution_mode,
            "status": t.status,
            "priority": t.priority,
            "credits_used": t.credits_used or 0,
            "duration_ms": t.duration_ms or 0,
            "created_at": _fmt_dt(t.created_at),
            "started_at": _fmt_dt(t.started_at),
            "completed_at": _fmt_dt(t.completed_at),
        }
        for t in tasks
    ]

    if fmt == "json":
        content = json.dumps({"tasks": rows, "count": len(rows), "days": days}, indent=2)
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="analytics_{days}d.json"'},
        )

    # CSV
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="analytics_{days}d.csv"'},
    )


# ── Completion-time percentiles ────────────────────────────────────────────────

class CompletionTimeStats(BaseModel):
    task_type: str
    count: int
    avg_minutes: Optional[float] = None
    p50_minutes: Optional[float] = None
    p95_minutes: Optional[float] = None
    min_minutes: Optional[float] = None
    max_minutes: Optional[float] = None


class CompletionTimesOut(BaseModel):
    days: int
    task_types: list[CompletionTimeStats]


def _percentile(data: list[float], pct: float) -> float:
    """Compute the p-th percentile of a sorted list using linear interpolation."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    n = len(sorted_data)
    idx = pct / 100.0 * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return round(sorted_data[-1], 2)
    frac = idx - lo
    return round(sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo]), 2)


@router.get("/completion-times", response_model=CompletionTimesOut)
async def completion_times(
    days: int = Query(30, ge=1, le=365),
    org_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_ANALYTICS_READ)),
):
    """Return p50/p95 completion time statistics per task type.

    Completion time = ``completed_at - created_at`` for all completed tasks
    that belong to the authenticated requester (or org) within the window.
    This powers the SLA Analytics dashboard for latency percentile charts.
    """
    uid = UUID(user_id)
    since = utcnow() - timedelta(days=days)

    filters = [
        TaskDB.user_id == uid,
        TaskDB.status == "completed",
        TaskDB.completed_at.isnot(None),
        TaskDB.created_at >= since,
    ]
    if org_id:
        mem = await db.execute(
            select(OrgMemberDB).where(
                OrgMemberDB.org_id == org_id,
                OrgMemberDB.user_id == uid,
            )
        )
        if not mem.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Not a member of this organization")
        filters = [
            TaskDB.org_id == org_id,
            TaskDB.status == "completed",
            TaskDB.completed_at.isnot(None),
            TaskDB.created_at >= since,
        ]

    result = await db.execute(
        select(TaskDB.type, TaskDB.created_at, TaskDB.completed_at)
        .where(*filters)
        .order_by(TaskDB.type)
        .limit(10_000)  # percentile accuracy acceptable; protects memory at scale
    )
    rows = result.fetchall()

    # Bucket durations by task_type
    from collections import defaultdict
    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        task_type, created, completed = row
        if created and completed:
            delta_minutes = (
                completed.replace(tzinfo=timezone.utc)
                - created.replace(tzinfo=timezone.utc)
            ).total_seconds() / 60.0
            if delta_minutes >= 0:
                buckets[task_type].append(delta_minutes)

    task_types: list[CompletionTimeStats] = []
    for tt, durations in sorted(buckets.items()):
        if not durations:
            continue
        task_types.append(
            CompletionTimeStats(
                task_type=tt,
                count=len(durations),
                avg_minutes=round(statistics.mean(durations), 2),
                p50_minutes=_percentile(durations, 50),
                p95_minutes=_percentile(durations, 95),
                min_minutes=round(min(durations), 2),
                max_minutes=round(max(durations), 2),
            )
        )

    return CompletionTimesOut(days=days, task_types=task_types)


# ── Revenue / Spend Dashboard ─────────────────────────────────────────────────

class MonthlySpendItem(BaseModel):
    month: str          # "YYYY-MM"
    credits_spent: int
    credits_purchased: int
    task_count: int
    completed_count: int


class WeeklySpendItem(BaseModel):
    week: str           # "YYYY-WXX"
    credits_spent: int
    task_count: int


class SpendForecast(BaseModel):
    next_30_days_credits: int
    next_30_days_usd: float
    trend: str          # "increasing" | "decreasing" | "stable"
    confidence: str     # "high" | "medium" | "low"


class RevenueAnalyticsOut(BaseModel):
    # KPIs
    total_credits_spent: int
    total_usd_spent: float
    avg_daily_credits: float
    total_tasks: int
    completed_tasks: int
    success_rate_pct: float
    avg_credits_per_task: float
    avg_credits_per_completion: float
    # Trends
    monthly_series: List[MonthlySpendItem]
    weekly_series: List[WeeklySpendItem]
    # Breakdown
    by_type: dict
    by_priority: dict
    by_execution_mode: dict
    # Forecast
    forecast: SpendForecast
    # Period
    months: int


@router.get("/revenue", response_model=RevenueAnalyticsOut)
async def revenue_analytics(
    months: int = Query(6, ge=1, le=24),
    org_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_ANALYTICS_READ)),
):
    """
    Requester revenue/spend dashboard: monthly + weekly series, forecast,
    breakdown by type/priority/mode, ROI metrics.
    """
    uid = UUID(user_id)
    since = utcnow() - timedelta(days=months * 30)
    now = utcnow()

    # ─── Set up task filters ───────────────────────────────────────────────
    task_filters = [TaskDB.user_id == uid, TaskDB.created_at >= since]
    txn_filters = [CreditTransactionDB.user_id == uid, CreditTransactionDB.created_at >= since]

    if org_id:
        mem = await db.execute(
            select(OrgMemberDB).where(
                OrgMemberDB.org_id == org_id,
                OrgMemberDB.user_id == uid,
            )
        )
        if not mem.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="Not a member of this organization")
        task_filters = [TaskDB.org_id == org_id, TaskDB.created_at >= since]
        txn_filters = [
            CreditTransactionDB.user_id.in_(
                select(OrgMemberDB.user_id).where(OrgMemberDB.org_id == org_id)
            ),
            CreditTransactionDB.created_at >= since,
        ]

    # ─── KPI aggregates ───────────────────────────────────────────────────
    total_tasks = (await db.scalar(
        select(func.count(TaskDB.id)).where(*task_filters)
    )) or 0

    completed_tasks = (await db.scalar(
        select(func.count(TaskDB.id)).where(*task_filters, TaskDB.status == "completed")
    )) or 0

    total_credits_spent = (await db.scalar(
        select(func.coalesce(func.sum(TaskDB.credits_used), 0)).where(
            *task_filters, TaskDB.credits_used.isnot(None)
        )
    )) or 0

    # ─── Monthly spend + purchases series ─────────────────────────────────
    monthly_spend_result = await db.execute(
        select(
            func.date_trunc("month", TaskDB.created_at).label("month"),
            func.coalesce(func.sum(TaskDB.credits_used), 0).label("credits_spent"),
            func.count(TaskDB.id).label("task_count"),
            func.count(case((TaskDB.status == "completed", TaskDB.id))).label("completed_count"),
        )
        .where(*task_filters, TaskDB.credits_used.isnot(None))
        .group_by(func.date_trunc("month", TaskDB.created_at))
        .order_by(func.date_trunc("month", TaskDB.created_at))
    )
    spend_by_month: dict = {}
    for row in monthly_spend_result:
        key = row.month.strftime("%Y-%m")
        spend_by_month[key] = {
            "credits_spent": int(row.credits_spent or 0),
            "task_count": int(row.task_count or 0),
            "completed_count": int(row.completed_count or 0),
        }

    # Monthly credit purchases (positive transactions = top-ups)
    monthly_purchase_result = await db.execute(
        select(
            func.date_trunc("month", CreditTransactionDB.created_at).label("month"),
            func.coalesce(func.sum(CreditTransactionDB.amount), 0).label("purchased"),
        )
        .where(
            CreditTransactionDB.user_id == uid,
            CreditTransactionDB.amount > 0,
            CreditTransactionDB.type == "credit",
            CreditTransactionDB.created_at >= since,
        )
        .group_by(func.date_trunc("month", CreditTransactionDB.created_at))
    )
    purchases_by_month: dict = {}
    for row in monthly_purchase_result:
        key = row.month.strftime("%Y-%m")
        purchases_by_month[key] = int(row.purchased or 0)

    # Build ordered monthly series (fill gaps)
    monthly_series: list[MonthlySpendItem] = []
    cur = since.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur <= now:
        key = cur.strftime("%Y-%m")
        sd = spend_by_month.get(key, {})
        monthly_series.append(MonthlySpendItem(
            month=key,
            credits_spent=sd.get("credits_spent", 0),
            credits_purchased=purchases_by_month.get(key, 0),
            task_count=sd.get("task_count", 0),
            completed_count=sd.get("completed_count", 0),
        ))
        # Advance to next month
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)

    # ─── Weekly spend series (last 12 weeks) ──────────────────────────────
    twelve_weeks_ago = now - timedelta(weeks=12)
    weekly_result = await db.execute(
        select(
            func.date_trunc("week", TaskDB.created_at).label("week"),
            func.coalesce(func.sum(TaskDB.credits_used), 0).label("credits_spent"),
            func.count(TaskDB.id).label("task_count"),
        )
        .where(
            *task_filters[:1],  # user/org filter only
            TaskDB.created_at >= twelve_weeks_ago,
            TaskDB.credits_used.isnot(None),
        )
        .group_by(func.date_trunc("week", TaskDB.created_at))
        .order_by(func.date_trunc("week", TaskDB.created_at))
    )
    weekly_series = [
        WeeklySpendItem(
            week=row.week.strftime("%Y-W%W"),
            credits_spent=int(row.credits_spent or 0),
            task_count=int(row.task_count or 0),
        )
        for row in weekly_result
    ]

    # ─── Breakdowns ───────────────────────────────────────────────────────
    by_type_result = await db.execute(
        select(TaskDB.type, func.coalesce(func.sum(TaskDB.credits_used), 0).label("credits"))
        .where(*task_filters, TaskDB.credits_used.isnot(None))
        .group_by(TaskDB.type)
        .order_by(func.sum(TaskDB.credits_used).desc())
    )
    by_type = {row.type: int(row.credits or 0) for row in by_type_result}

    by_priority_result = await db.execute(
        select(TaskDB.priority, func.coalesce(func.sum(TaskDB.credits_used), 0).label("credits"))
        .where(*task_filters, TaskDB.credits_used.isnot(None))
        .group_by(TaskDB.priority)
    )
    by_priority = {(row.priority or "normal"): int(row.credits or 0) for row in by_priority_result}

    by_mode_result = await db.execute(
        select(TaskDB.execution_mode, func.coalesce(func.sum(TaskDB.credits_used), 0).label("credits"))
        .where(*task_filters, TaskDB.credits_used.isnot(None))
        .group_by(TaskDB.execution_mode)
    )
    by_execution_mode = {(row.execution_mode or "ai"): int(row.credits or 0) for row in by_mode_result}

    # ─── Spend Forecast (simple linear regression on weekly spend) ────────
    forecast = _compute_forecast(weekly_series)

    # ─── Derived KPIs ─────────────────────────────────────────────────────
    days_in_period = max(1, (now - since).days)
    avg_daily_credits = round(total_credits_spent / days_in_period, 1)
    success_rate_pct = round(100 * completed_tasks / total_tasks, 1) if total_tasks else 0.0
    avg_credits_per_task = round(total_credits_spent / total_tasks, 1) if total_tasks else 0.0
    avg_credits_per_completion = round(total_credits_spent / completed_tasks, 1) if completed_tasks else 0.0

    return RevenueAnalyticsOut(
        total_credits_spent=total_credits_spent,
        total_usd_spent=round(total_credits_spent / 100, 2),
        avg_daily_credits=avg_daily_credits,
        total_tasks=total_tasks,
        completed_tasks=completed_tasks,
        success_rate_pct=success_rate_pct,
        avg_credits_per_task=avg_credits_per_task,
        avg_credits_per_completion=avg_credits_per_completion,
        monthly_series=monthly_series,
        weekly_series=weekly_series,
        by_type=by_type,
        by_priority=by_priority,
        by_execution_mode=by_execution_mode,
        forecast=forecast,
        months=months,
    )


def _compute_forecast(weekly_series: list[WeeklySpendItem]) -> SpendForecast:
    """
    Simple least-squares linear regression on the last 8 weeks of spend
    to project the next 30 days of credits consumption.
    """
    data = [w.credits_spent for w in weekly_series[-8:]]
    n = len(data)

    if n < 2:
        return SpendForecast(
            next_30_days_credits=data[0] * 4 if data else 0,
            next_30_days_usd=round((data[0] * 4 if data else 0) / 100, 2),
            trend="stable",
            confidence="low",
        )

    # OLS: y = a + b*x
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(data) / n
    num = sum((xs[i] - mean_x) * (data[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    slope = num / den if den else 0
    intercept = mean_y - slope * mean_x

    # Project 4 more weeks ahead (≈30 days)
    projected_weekly = max(0, intercept + slope * (n + 3))  # midpoint of next 4 weeks
    projected_30d = int(projected_weekly * 4)

    # Determine trend from slope
    rel_slope = slope / mean_y if mean_y else 0
    if rel_slope > 0.05:
        trend = "increasing"
    elif rel_slope < -0.05:
        trend = "decreasing"
    else:
        trend = "stable"

    # Confidence based on R² and data points
    ss_res = sum((data[i] - (intercept + slope * xs[i])) ** 2 for i in range(n))
    ss_tot = sum((data[i] - mean_y) ** 2 for i in range(n))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0

    if r2 >= 0.7 and n >= 6:
        confidence = "high"
    elif r2 >= 0.4 and n >= 4:
        confidence = "medium"
    else:
        confidence = "low"

    return SpendForecast(
        next_30_days_credits=projected_30d,
        next_30_days_usd=round(projected_30d / 100, 2),
        trend=trend,
        confidence=confidence,
    )
