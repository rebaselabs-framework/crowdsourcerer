"""SLA management — task SLA status, breach tracking, priority queues."""
from __future__ import annotations
import uuid as _uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

import asyncio

from core.auth import get_current_user_id, require_admin
from core.database import get_db
from core.sla import get_sla_hours, sla_status, compute_sla_deadline, PRIORITY_CREDIT_MULTIPLIER
from core.webhooks import fire_webhook_for_task, fire_persistent_endpoints
from models.db import TaskDB, UserDB, SLABreachDB

logger = structlog.get_logger()
router = APIRouter(tags=["sla"])


# ─── Schemas ──────────────────────────────────────────────────────────────────

class SLAStatusOut(BaseModel):
    task_id: UUID
    title: Optional[str]
    status: str          # on_track | breached | met
    priority: str
    plan: str
    sla_hours: float
    deadline: str
    remaining_hours: Optional[float] = None
    overdue_hours: Optional[float] = None
    pct_elapsed: Optional[float] = None
    completed_at: Optional[datetime] = None


class SLABreachOut(BaseModel):
    id: UUID
    task_id: UUID
    task_title: Optional[str]
    plan: str
    priority: str
    sla_hours: float
    task_created_at: datetime
    breach_at: datetime
    resolved_at: Optional[datetime]
    credits_refunded: int

    model_config = ConfigDict(from_attributes=True)


class SLASummaryOut(BaseModel):
    total_tasks: int
    on_track: int
    breached_unresolved: int
    breached_resolved: int
    breach_rate_pct: float
    by_priority: dict
    by_plan: dict


class PriorityInfoOut(BaseModel):
    priority: str
    label: str
    sla_hours_by_plan: dict
    credit_multiplier: float
    description: str


# ─── Public info endpoints ────────────────────────────────────────────────────

@router.get("/v1/sla/priorities", response_model=list[PriorityInfoOut])
async def get_priority_info():
    """Return available priority tiers and their SLA/credit info."""
    return [
        PriorityInfoOut(
            priority="low",
            label="Low",
            sla_hours_by_plan={
                "free": get_sla_hours("free", "low"),
                "starter": get_sla_hours("starter", "low"),
                "pro": get_sla_hours("pro", "low"),
                "enterprise": get_sla_hours("enterprise", "low"),
            },
            credit_multiplier=PRIORITY_CREDIT_MULTIPLIER["low"],
            description="Relaxed SLA. 25% credit discount.",
        ),
        PriorityInfoOut(
            priority="normal",
            label="Normal",
            sla_hours_by_plan={
                "free": get_sla_hours("free", "normal"),
                "starter": get_sla_hours("starter", "normal"),
                "pro": get_sla_hours("pro", "normal"),
                "enterprise": get_sla_hours("enterprise", "normal"),
            },
            credit_multiplier=PRIORITY_CREDIT_MULTIPLIER["normal"],
            description="Standard SLA at base cost.",
        ),
        PriorityInfoOut(
            priority="high",
            label="High",
            sla_hours_by_plan={
                "free": get_sla_hours("free", "high"),
                "starter": get_sla_hours("starter", "high"),
                "pro": get_sla_hours("pro", "high"),
                "enterprise": get_sla_hours("enterprise", "high"),
            },
            credit_multiplier=PRIORITY_CREDIT_MULTIPLIER["high"],
            description="2× faster SLA. 25% credit premium.",
        ),
        PriorityInfoOut(
            priority="urgent",
            label="Urgent",
            sla_hours_by_plan={
                "free": get_sla_hours("free", "urgent"),
                "starter": get_sla_hours("starter", "urgent"),
                "pro": get_sla_hours("pro", "urgent"),
                "enterprise": get_sla_hours("enterprise", "urgent"),
            },
            credit_multiplier=PRIORITY_CREDIT_MULTIPLIER["urgent"],
            description="4× faster SLA. 75% credit premium. Shown first to workers.",
        ),
    ]


# ─── Requester SLA endpoints ──────────────────────────────────────────────────

@router.get("/v1/tasks/sla-status", response_model=list[SLAStatusOut])
async def get_my_sla_status(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return SLA status for the current user's active (non-completed) human tasks."""
    uid = UUID(user_id)
    user_res = await db.execute(select(UserDB).where(UserDB.id == uid))
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    q = (
        select(TaskDB)
        .where(
            TaskDB.user_id == uid,
            TaskDB.execution_mode == "human",
            TaskDB.status.in_(["open", "assigned", "completed", "failed"]),
        )
        .order_by(TaskDB.created_at.desc())
        .limit(limit)
    )
    res = await db.execute(q)
    tasks = list(res.scalars().all())

    plan = user.plan
    results: list[SLAStatusOut] = []
    for t in tasks:
        priority = t.priority or "normal"
        s = sla_status(
            created_at=t.created_at,
            plan=plan,
            priority=priority,
            completed_at=t.updated_at if t.status in ("completed", "failed") else None,
        )
        out = SLAStatusOut(
            task_id=t.id,
            title=t.input.get("title") if isinstance(t.input, dict) else None,
            status=s["status"],
            priority=priority,
            plan=plan,
            sla_hours=s["sla_hours"],
            deadline=s["deadline"],
            remaining_hours=s.get("remaining_hours"),
            overdue_hours=s.get("overdue_hours"),
            pct_elapsed=s.get("pct_elapsed"),
            completed_at=t.updated_at if t.status in ("completed", "failed") else None,
        )
        if status_filter and out.status != status_filter:
            continue
        results.append(out)

    return results


# ─── Admin SLA endpoints ──────────────────────────────────────────────────────

@router.get("/v1/admin/sla/breaches", response_model=list[SLABreachOut],
            dependencies=[Depends(require_admin)])
async def list_sla_breaches(
    resolved: Optional[bool] = Query(None),
    plan: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Admin: list all SLA breach records."""
    q = select(SLABreachDB).order_by(SLABreachDB.breach_at.desc())
    if resolved is not None:
        if resolved:
            q = q.where(SLABreachDB.resolved_at.isnot(None))
        else:
            q = q.where(SLABreachDB.resolved_at.is_(None))
    if plan:
        q = q.where(SLABreachDB.plan == plan)
    if priority:
        q = q.where(SLABreachDB.priority == priority)
    q = q.limit(limit)
    res = await db.execute(q)
    breaches = list(res.scalars().all())

    # Enrich with task title
    out = []
    for b in breaches:
        task_res = await db.execute(select(TaskDB).where(TaskDB.id == b.task_id))
        task = task_res.scalar_one_or_none()
        title = task.input.get("title") if task and isinstance(task.input, dict) else None
        out.append(SLABreachOut(
            id=b.id,
            task_id=b.task_id,
            task_title=title,
            plan=b.plan,
            priority=b.priority,
            sla_hours=b.sla_hours,
            task_created_at=b.task_created_at,
            breach_at=b.breach_at,
            resolved_at=b.resolved_at,
            credits_refunded=b.credits_refunded,
        ))
    return out


@router.get("/v1/admin/sla/summary", response_model=SLASummaryOut,
            dependencies=[Depends(require_admin)])
async def sla_summary(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Admin: aggregate SLA health metrics."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Human tasks in window
    total_res = await db.execute(
        select(func.count()).where(
            TaskDB.execution_mode == "human",
            TaskDB.created_at >= since,
        )
    )
    total = total_res.scalar_one() or 0

    breach_res = await db.execute(
        select(SLABreachDB).where(SLABreachDB.breach_at >= since)
    )
    breaches = list(breach_res.scalars().all())

    breached_unresolved = sum(1 for b in breaches if b.resolved_at is None)
    breached_resolved = sum(1 for b in breaches if b.resolved_at is not None)

    by_priority: dict[str, int] = {}
    by_plan: dict[str, int] = {}
    for b in breaches:
        by_priority[b.priority] = by_priority.get(b.priority, 0) + 1
        by_plan[b.plan] = by_plan.get(b.plan, 0) + 1

    return SLASummaryOut(
        total_tasks=total,
        on_track=max(0, total - len(breaches)),
        breached_unresolved=breached_unresolved,
        breached_resolved=breached_resolved,
        breach_rate_pct=round(len(breaches) / max(total, 1) * 100, 2),
        by_priority=by_priority,
        by_plan=by_plan,
    )


# ─── Internal helper ─────────────────────────────────────────────────────────

async def record_sla_breach(task: TaskDB, user: UserDB, db: AsyncSession) -> None:
    """Create an SLABreachDB record if not already breached. Called by sweeper."""
    exist_res = await db.execute(
        select(SLABreachDB).where(SLABreachDB.task_id == task.id)
    )
    if exist_res.scalar_one_or_none():
        return  # already recorded

    priority = task.priority or "normal"
    plan = user.plan or "free"
    hours = get_sla_hours(plan, priority)
    deadline = compute_sla_deadline(task.created_at, plan, priority)
    now = datetime.now(timezone.utc)

    if now <= deadline:
        return  # Not breached yet

    breach = SLABreachDB(
        id=_uuid.uuid4(),
        task_id=task.id,
        user_id=user.id,
        plan=plan,
        priority=priority,
        sla_hours=hours,
        task_created_at=task.created_at,
        breach_at=deadline,
    )
    db.add(breach)
    logger.warning("sla.breach", task_id=str(task.id), plan=plan, priority=priority,
                   sla_hours=hours)

    # Fire sla.breach webhook (per-task + persistent endpoints)
    _sla_extra = {"type": task.type, "plan": plan, "priority": priority,
                  "sla_hours": hours, "deadline": deadline.isoformat()}
    if task.webhook_url:
        asyncio.create_task(fire_webhook_for_task(
            task=task,
            event_type="sla.breach",
            extra=_sla_extra,
        ))
    asyncio.create_task(fire_persistent_endpoints(
        user_id=str(task.user_id),
        task_id=str(task.id),
        event_type="sla.breach",
        extra=_sla_extra,
    ))
