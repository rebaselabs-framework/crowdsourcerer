"""Admin API — platform statistics and user management.

Only accessible by users with is_admin=True.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, cast, Date as SADate, and_, or_

import time as _time_module

from core.auth import get_current_user_id, require_admin
from core.database import get_db, AsyncSessionLocal
from core.sweeper import sweep_once, get_sweeper_task, _sweep_scheduled_tasks, _LAST_SWEEP_AT
from core.audit import log_admin_action
from core.result_cache import cache_stats, cache_flush
from models.db import TaskDB, UserDB, CreditTransactionDB, TaskAssignmentDB, WebhookLogDB, PayoutRequestDB, WorkerStrikeDB, AdminAuditLogDB, SystemAlertDB, RequesterOnboardingDB, OnboardingProgressDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/admin", tags=["admin"])


# ─── Platform Stats ────────────────────────────────────────────────────────

@router.get("/stats")
async def get_platform_stats(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Overall platform statistics."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    today = now.date()

    # User counts
    total_users = (await db.execute(select(func.count()).select_from(UserDB))).scalar() or 0
    active_users = (await db.execute(
        select(func.count()).select_from(UserDB).where(UserDB.is_active == True)
    )).scalar() or 0
    workers = (await db.execute(
        select(func.count()).select_from(UserDB).where(
            UserDB.role.in_(["worker", "both"])
        )
    )).scalar() or 0
    new_users_week = (await db.execute(
        select(func.count()).select_from(UserDB).where(UserDB.created_at >= week_ago)
    )).scalar() or 0

    # Task counts
    total_tasks = (await db.execute(select(func.count()).select_from(TaskDB))).scalar() or 0
    completed_tasks = (await db.execute(
        select(func.count()).select_from(TaskDB).where(TaskDB.status == "completed")
    )).scalar() or 0
    failed_tasks = (await db.execute(
        select(func.count()).select_from(TaskDB).where(TaskDB.status == "failed")
    )).scalar() or 0
    running_tasks = (await db.execute(
        select(func.count()).select_from(TaskDB).where(TaskDB.status.in_(["running", "queued"]))
    )).scalar() or 0
    open_human_tasks = (await db.execute(
        select(func.count()).select_from(TaskDB).where(
            TaskDB.execution_mode == "human",
            TaskDB.status == "open",
        )
    )).scalar() or 0
    tasks_this_week = (await db.execute(
        select(func.count()).select_from(TaskDB).where(TaskDB.created_at >= week_ago)
    )).scalar() or 0

    # Task type breakdown (top 10)
    type_counts_result = await db.execute(
        select(TaskDB.type, func.count().label("cnt"))
        .group_by(TaskDB.type)
        .order_by(func.count().desc())
        .limit(10)
    )
    task_type_breakdown = [
        {"type": row.type, "count": row.cnt}
        for row in type_counts_result.all()
    ]

    # Credits
    credits_in_circulation = (await db.execute(
        select(func.sum(UserDB.credits)).select_from(UserDB)
    )).scalar() or 0

    # Revenue proxy: total positive credit transactions (purchases)
    credits_purchased = (await db.execute(
        select(func.sum(CreditTransactionDB.amount)).select_from(CreditTransactionDB).where(
            CreditTransactionDB.type == "credit",
            CreditTransactionDB.amount > 0,
        )
    )).scalar() or 0

    # Worker assignments
    total_assignments = (await db.execute(
        select(func.count()).select_from(TaskAssignmentDB)
    )).scalar() or 0
    submitted_assignments = (await db.execute(
        select(func.count()).select_from(TaskAssignmentDB).where(
            TaskAssignmentDB.status.in_(["submitted", "approved", "rejected"])
        )
    )).scalar() or 0

    # Webhooks
    total_webhooks = (await db.execute(
        select(func.count()).select_from(WebhookLogDB)
    )).scalar() or 0
    failed_webhooks = (await db.execute(
        select(func.count()).select_from(WebhookLogDB).where(WebhookLogDB.success == False)
    )).scalar() or 0

    return {
        "users": {
            "total": total_users,
            "active": active_users,
            "workers": workers,
            "new_this_week": new_users_week,
        },
        "tasks": {
            "total": total_tasks,
            "completed": completed_tasks,
            "failed": failed_tasks,
            "running": running_tasks,
            "open_human": open_human_tasks,
            "this_week": tasks_this_week,
            "success_rate": round(completed_tasks / total_tasks * 100, 1) if total_tasks > 0 else 0,
            "type_breakdown": task_type_breakdown,
        },
        "worker_assignments": {
            "total": total_assignments,
            "submitted": submitted_assignments,
        },
        "credits": {
            "in_circulation": credits_in_circulation,
            "total_purchased": credits_purchased,
        },
        "webhooks": {
            "total": total_webhooks,
            "failed": failed_webhooks,
            "success_rate": round((total_webhooks - failed_webhooks) / total_webhooks * 100, 1)
            if total_webhooks > 0 else 100,
        },
        "generated_at": now.isoformat(),
    }


# ─── User Management ───────────────────────────────────────────────────────

@router.get("/users")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    plan: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """List all users with pagination and filtering."""
    q = select(UserDB)
    if search:
        q = q.where(
            UserDB.email.ilike(f"%{search}%") | UserDB.name.ilike(f"%{search}%")
        )
    if role:
        q = q.where(UserDB.role == role)
    if plan:
        q = q.where(UserDB.plan == plan)

    total = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar() or 0

    q = q.order_by(UserDB.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    users = (await db.execute(q)).scalars().all()

    return {
        "items": [
            {
                "id": str(u.id),
                "email": u.email,
                "name": u.name,
                "plan": u.plan,
                "role": u.role,
                "credits": u.credits,
                "is_active": u.is_active,
                "is_admin": u.is_admin,
                "worker_tasks_completed": u.worker_tasks_completed,
                "worker_level": u.worker_level,
                "worker_xp": u.worker_xp,
                "worker_accuracy": u.worker_accuracy,
                "created_at": u.created_at.isoformat(),
            }
            for u in users
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": (page * page_size) < total,
    }


@router.get("/users/{user_id}")
async def get_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Get detailed info about a specific user."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Task stats for this user
    task_counts = (await db.execute(
        select(TaskDB.status, func.count().label("cnt"))
        .where(TaskDB.user_id == user_id)
        .group_by(TaskDB.status)
    )).all()

    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "plan": user.plan,
        "role": user.role,
        "credits": user.credits,
        "is_active": user.is_active,
        "is_admin": user.is_admin,
        "worker_xp": user.worker_xp,
        "worker_level": user.worker_level,
        "worker_accuracy": user.worker_accuracy,
        "worker_reliability": user.worker_reliability,
        "worker_tasks_completed": user.worker_tasks_completed,
        "worker_streak_days": user.worker_streak_days,
        "created_at": user.created_at.isoformat(),
        "task_stats": {row.status: row.cnt for row in task_counts},
    }


@router.patch("/users/{user_id}")
async def update_user(
    user_id: UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Update user fields (plan, is_active, is_admin, credits adjustment)."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    allowed = {"plan", "is_active", "is_admin", "credits"}
    for key, val in body.items():
        if key in allowed:
            setattr(user, key, val)

    await db.commit()
    logger.info("admin_user_updated", target_user_id=str(user_id), changes=list(body.keys()))
    return {"updated": True, "user_id": str(user_id)}


# ─── Task Management ───────────────────────────────────────────────────────

@router.get("/tasks")
async def list_all_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """List all tasks across all users."""
    q = select(TaskDB)
    if status:
        q = q.where(TaskDB.status == status)
    if type:
        q = q.where(TaskDB.type == type)

    total = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar() or 0

    q = q.order_by(TaskDB.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    tasks = (await db.execute(q)).scalars().all()

    return {
        "items": [
            {
                "id": str(t.id),
                "user_id": str(t.user_id),
                "type": t.type,
                "status": t.status,
                "priority": t.priority,
                "execution_mode": t.execution_mode,
                "credits_used": t.credits_used,
                "created_at": t.created_at.isoformat(),
            }
            for t in tasks
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": (page * page_size) < total,
    }


# ─── Sweeper Controls ──────────────────────────────────────────────────────

@router.post("/sweep")
async def trigger_sweep(
    _: str = Depends(require_admin),
):
    """Manually trigger the assignment timeout sweep.

    Useful for testing or recovering from a period when the sweeper was down.
    """
    result = await sweep_once(AsyncSessionLocal)
    activated = await _sweep_scheduled_tasks(AsyncSessionLocal)
    return {
        "ok": True,
        "summary": {**result, "scheduled_activated": activated},
    }


@router.get("/analytics")
async def get_analytics(
    days: int = Query(30, ge=7, le=90),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Time-series analytics: task throughput, signups, revenue, worker activity."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    # ── Daily task counts ────────────────────────────────────────────────────
    daily_tasks_result = await db.execute(
        select(
            cast(TaskDB.created_at, SADate).label("day"),
            func.count().label("total"),
            func.count().filter(TaskDB.status == "completed").label("completed"),
            func.count().filter(TaskDB.status == "failed").label("failed"),
            func.count().filter(TaskDB.execution_mode == "human").label("human"),
            func.count().filter(TaskDB.execution_mode == "ai").label("ai"),
        )
        .where(TaskDB.created_at >= start)
        .group_by(cast(TaskDB.created_at, SADate))
        .order_by(cast(TaskDB.created_at, SADate))
    )
    daily_tasks_raw = {
        str(row.day): {
            "total": row.total,
            "completed": row.completed,
            "failed": row.failed,
            "human": row.human,
            "ai": row.ai,
        }
        for row in daily_tasks_result.all()
    }

    # ── Daily user signups ───────────────────────────────────────────────────
    daily_signups_result = await db.execute(
        select(
            cast(UserDB.created_at, SADate).label("day"),
            func.count().label("total"),
            func.count().filter(UserDB.role.in_(["worker", "both"])).label("workers"),
        )
        .where(UserDB.created_at >= start)
        .group_by(cast(UserDB.created_at, SADate))
        .order_by(cast(UserDB.created_at, SADate))
    )
    daily_signups_raw = {
        str(row.day): {"total": row.total, "workers": row.workers}
        for row in daily_signups_result.all()
    }

    # ── Daily credits consumed (task charges) ───────────────────────────────
    daily_credits_result = await db.execute(
        select(
            cast(CreditTransactionDB.created_at, SADate).label("day"),
            func.sum(func.abs(CreditTransactionDB.amount)).label("consumed"),
        )
        .where(
            CreditTransactionDB.created_at >= start,
            CreditTransactionDB.type == "charge",
        )
        .group_by(cast(CreditTransactionDB.created_at, SADate))
        .order_by(cast(CreditTransactionDB.created_at, SADate))
    )
    daily_credits_raw = {
        str(row.day): int(row.consumed or 0)
        for row in daily_credits_result.all()
    }

    # ── Daily worker assignments completed ───────────────────────────────────
    daily_completions_result = await db.execute(
        select(
            cast(TaskAssignmentDB.submitted_at, SADate).label("day"),
            func.count().label("total"),
            func.sum(TaskAssignmentDB.earnings_credits).label("credits_earned"),
        )
        .where(
            TaskAssignmentDB.submitted_at >= start,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
        )
        .group_by(cast(TaskAssignmentDB.submitted_at, SADate))
        .order_by(cast(TaskAssignmentDB.submitted_at, SADate))
    )
    daily_completions_raw = {
        str(row.day): {"total": row.total, "credits_earned": int(row.credits_earned or 0)}
        for row in daily_completions_result.all()
    }

    # ── Fill in all days in range ────────────────────────────────────────────
    all_days = []
    daily_tasks = []
    daily_signups = []
    daily_credits = []
    daily_completions = []

    for i in range(days):
        d = (now - timedelta(days=days - 1 - i)).date()
        d_str = str(d)
        all_days.append(d_str)
        t = daily_tasks_raw.get(d_str, {"total": 0, "completed": 0, "failed": 0, "human": 0, "ai": 0})
        daily_tasks.append(t)
        s = daily_signups_raw.get(d_str, {"total": 0, "workers": 0})
        daily_signups.append(s)
        daily_credits.append(daily_credits_raw.get(d_str, 0))
        c = daily_completions_raw.get(d_str, {"total": 0, "credits_earned": 0})
        daily_completions.append(c)

    # ── Top workers ──────────────────────────────────────────────────────────
    top_workers_result = await db.execute(
        select(UserDB)
        .where(
            UserDB.role.in_(["worker", "both"]),
            UserDB.worker_tasks_completed > 0,
        )
        .order_by(UserDB.worker_xp.desc())
        .limit(10)
    )
    top_workers = [
        {
            "id": str(u.id),
            "name": u.name or u.email.split("@")[0],
            "xp": u.worker_xp,
            "level": u.worker_level,
            "tasks_completed": u.worker_tasks_completed,
            "accuracy": round(u.worker_accuracy * 100, 1) if u.worker_accuracy else None,
            "streak_days": u.worker_streak_days,
        }
        for u in top_workers_result.scalars().all()
    ]

    # ── Hourly breakdown today ───────────────────────────────────────────────
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hourly_result = await db.execute(
        select(
            func.date_part("hour", TaskDB.created_at).label("hour"),
            func.count().label("total"),
        )
        .where(TaskDB.created_at >= today_start)
        .group_by(func.date_part("hour", TaskDB.created_at))
        .order_by(func.date_part("hour", TaskDB.created_at))
    )
    hourly_raw = {int(row.hour): row.total for row in hourly_result.all()}
    hourly_tasks = [hourly_raw.get(h, 0) for h in range(24)]

    # ── Payout totals ────────────────────────────────────────────────────────
    payouts_result = await db.execute(
        select(
            PayoutRequestDB.status,
            func.count().label("cnt"),
            func.sum(PayoutRequestDB.usd_amount).label("total_usd"),
        )
        .group_by(PayoutRequestDB.status)
    )
    payout_summary = {
        row.status: {"count": row.cnt, "usd": round(float(row.total_usd or 0), 2)}
        for row in payouts_result.all()
    }

    return {
        "days": days,
        "all_days": all_days,
        "daily_tasks": daily_tasks,
        "daily_signups": daily_signups,
        "daily_credits_consumed": daily_credits,
        "daily_worker_completions": daily_completions,
        "hourly_tasks_today": hourly_tasks,
        "top_workers": top_workers,
        "payout_summary": payout_summary,
        "generated_at": now.isoformat(),
    }


@router.get("/sweeper/status")
async def get_sweeper_status(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Return sweeper task status and count of currently expired (un-swept) assignments."""
    from sqlalchemy import and_
    now = datetime.now(timezone.utc)

    task = get_sweeper_task()
    sweeper_alive = task is not None and not task.done()

    # Count assignments that are currently expired but not yet swept
    expired_count = await db.scalar(
        select(func.count()).select_from(TaskAssignmentDB).where(
            and_(
                TaskAssignmentDB.status == "active",
                TaskAssignmentDB.timeout_at != None,  # noqa: E711
                TaskAssignmentDB.timeout_at <= now,
            )
        )
    ) or 0

    # Count timed_out assignments in the last 24h
    day_ago = now - timedelta(hours=24)
    recent_timeouts = await db.scalar(
        select(func.count()).select_from(TaskAssignmentDB).where(
            and_(
                TaskAssignmentDB.status == "timed_out",
                TaskAssignmentDB.released_at >= day_ago,
            )
        )
    ) or 0

    return {
        "sweeper_running": sweeper_alive,
        "expired_pending_sweep": expired_count,
        "timed_out_last_24h": recent_timeouts,
        "checked_at": now.isoformat(),
    }


# ─── Worker Matching Stats ────────────────────────────────────────────────

@router.get("/matching/stats")
async def get_matching_stats(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_admin),
):
    """Return platform-wide skill matching statistics."""
    from models.db import WorkerSkillDB
    from sqlalchemy import distinct

    # Total worker skill rows
    total_skills = await db.scalar(select(func.count()).select_from(WorkerSkillDB)) or 0

    # Workers with at least one skill profile
    skilled_workers = await db.scalar(
        select(func.count(distinct(WorkerSkillDB.worker_id)))
    ) or 0

    # Average proficiency per task type
    prof_rows = (await db.execute(
        select(
            WorkerSkillDB.task_type,
            func.avg(WorkerSkillDB.proficiency_level).label("avg_prof"),
            func.count().label("workers"),
        ).group_by(WorkerSkillDB.task_type).order_by(func.count().desc())
    )).all()

    # Tasks with a min_skill_level set
    gated_tasks = await db.scalar(
        select(func.count()).select_from(TaskDB).where(TaskDB.min_skill_level != None)  # noqa: E711
    ) or 0

    return {
        "total_skill_profiles": total_skills,
        "workers_with_skills": skilled_workers,
        "gated_tasks": gated_tasks,
        "proficiency_by_type": [
            {
                "task_type": r.task_type,
                "avg_proficiency": round(r.avg_prof, 2),
                "worker_count": r.workers,
            }
            for r in prof_rows
        ],
    }


# ─── Task Queue Visibility ────────────────────────────────────────────────

_PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}

@router.get("/queue")
async def get_task_queue(
    execution_mode: Optional[str] = Query(None, description="Filter: ai | human"),
    priority: Optional[str] = Query(None, description="Filter: urgent|high|normal|low"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """
    Return the current task queue grouped by priority with ETA estimates.

    ETA is estimated from recent median completion times for matching task types.
    Queue includes tasks in: pending, queued, running, open, assigned.
    """
    now = datetime.now(timezone.utc)
    lookback = now - timedelta(days=14)

    # Build queue query
    queue_statuses = ["pending", "queued", "running", "open", "assigned"]
    q = select(TaskDB).where(TaskDB.status.in_(queue_statuses))
    if execution_mode:
        q = q.where(TaskDB.execution_mode == execution_mode)
    if priority:
        q = q.where(TaskDB.priority == priority)
    q = q.order_by(
        TaskDB.priority.desc(),   # urgent first (relies on enum sort or we sort in Python)
        TaskDB.created_at.asc(),  # FIFO within same priority
    )

    tasks_result = await db.execute(q)
    tasks = tasks_result.scalars().all()

    # Compute average completion times per task type (last 14 days)
    completed_q = select(
        TaskDB.type,
        func.avg(TaskDB.duration_ms).label("avg_ms"),
        func.count().label("sample_size"),
    ).where(
        TaskDB.status == "completed",
        TaskDB.completed_at >= lookback,
        TaskDB.duration_ms.is_not(None),
    ).group_by(TaskDB.type)

    completed_rows = (await db.execute(completed_q)).all()
    median_by_type: dict[str, float] = {r.type: (r.avg_ms or 0) for r in completed_rows}
    sample_by_type: dict[str, int] = {r.type: r.sample_size for r in completed_rows}

    # For human tasks with no duration_ms, estimate from assignment → completion times
    human_fallback_ms = 4 * 3600 * 1000  # 4 hours default for human tasks
    ai_fallback_ms = 30_000              # 30 seconds default for AI tasks

    def _eta(task: TaskDB) -> Optional[float]:
        """Return estimated ms until completion from now."""
        median = median_by_type.get(task.type)
        if median:
            elapsed = (now - task.created_at).total_seconds() * 1000
            remaining = median - elapsed
            return max(remaining, 0)
        # Fallback
        mode = task.execution_mode or "ai"
        fb = human_fallback_ms if mode == "human" else ai_fallback_ms
        elapsed = (now - task.created_at).total_seconds() * 1000
        return max(fb - elapsed, 0)

    def _eta_label(ms: Optional[float]) -> str:
        if ms is None:
            return "Unknown"
        if ms <= 0:
            return "Overdue"
        s = ms / 1000
        if s < 60:
            return f"{int(s)}s"
        if s < 3600:
            return f"{int(s/60)}m"
        return f"{s/3600:.1f}h"

    # Group by priority
    by_priority: dict[str, list] = {
        "urgent": [], "high": [], "normal": [], "low": []
    }
    for task in tasks:
        p = task.priority or "normal"
        eta_ms = _eta(task)
        by_priority[p].append({
            "id": str(task.id),
            "type": task.type,
            "status": task.status,
            "execution_mode": task.execution_mode,
            "priority": task.priority,
            "created_at": task.created_at.isoformat(),
            "age_minutes": round((now - task.created_at).total_seconds() / 60, 1),
            "eta_ms": round(eta_ms) if eta_ms is not None else None,
            "eta_label": _eta_label(eta_ms),
            "has_eta_sample": task.type in median_by_type,
        })

    # Summary stats per priority
    priority_summary = []
    for p in ["urgent", "high", "normal", "low"]:
        items = by_priority[p]
        priority_summary.append({
            "priority": p,
            "count": len(items),
            "oldest_age_minutes": max((i["age_minutes"] for i in items), default=0),
            "tasks": items,
        })

    # ETA confidence — how many types have real timing data
    covered_types = len(median_by_type)
    total_types_in_queue = len({t.type for t in tasks})

    return {
        "queue_size": len(tasks),
        "by_priority": priority_summary,
        "eta_model": {
            "covered_task_types": covered_types,
            "total_queue_types": total_types_in_queue,
            "confidence": "high" if covered_types >= total_types_in_queue else (
                "medium" if covered_types > 0 else "low"
            ),
            "lookback_days": 14,
        },
        "generated_at": now.isoformat(),
    }


# ─── Weekly Digest ────────────────────────────────────────────────────────────

@router.post("/digest/send", tags=["admin"])
async def trigger_weekly_digest(
    db: AsyncSession = Depends(get_db),
    current_user: UserDB = Depends(require_admin),
):
    """Manually trigger the weekly digest email to all active users (admin only)."""
    from core.email import send_weekly_digest
    from sqlalchemy import func as sqlfunc

    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    week_label = f"{week_start.strftime('%b %d')} – {now.strftime('%b %d, %Y')}"
    sent = 0

    # Get top 5 workers this week
    top_workers_res = await db.execute(
        select(UserDB.id, UserDB.name, UserDB.email,
               sqlfunc.count(TaskAssignmentDB.id).label("task_count"),
               sqlfunc.sum(TaskAssignmentDB.earnings_credits).label("earnings"))
        .join(TaskAssignmentDB, TaskAssignmentDB.worker_id == UserDB.id)
        .where(
            TaskAssignmentDB.status == "approved",
            TaskAssignmentDB.submitted_at >= week_start,
        )
        .group_by(UserDB.id, UserDB.name, UserDB.email)
        .order_by(sqlfunc.count(TaskAssignmentDB.id).desc())
        .limit(5)
    )
    top_workers = [
        {"name": r.name or r.email.split("@")[0], "tasks": r.task_count, "earnings": r.earnings or 0}
        for r in top_workers_res
    ]

    users_res = await db.execute(
        select(UserDB).where(UserDB.is_active == True, UserDB.is_banned == False)
    )
    users = users_res.scalars().all()

    for user in users:
        tasks_created = await db.scalar(
            select(sqlfunc.count(TaskDB.id)).where(
                TaskDB.user_id == user.id, TaskDB.created_at >= week_start
            )
        ) or 0
        tasks_completed = await db.scalar(
            select(sqlfunc.count(TaskDB.id)).where(
                TaskDB.user_id == user.id, TaskDB.status == "completed",
                TaskDB.updated_at >= week_start
            )
        ) or 0
        credits_spent = await db.scalar(
            select(sqlfunc.abs(sqlfunc.sum(CreditTransactionDB.amount))).where(
                CreditTransactionDB.user_id == user.id,
                CreditTransactionDB.amount < 0,
                CreditTransactionDB.created_at >= week_start,
            )
        ) or 0
        is_worker = user.role in ("worker", "both")
        worker_tasks = worker_earnings_val = worker_xp = 0
        if is_worker:
            worker_tasks = await db.scalar(
                select(sqlfunc.count(TaskAssignmentDB.id)).where(
                    TaskAssignmentDB.worker_id == user.id,
                    TaskAssignmentDB.status == "approved",
                    TaskAssignmentDB.submitted_at >= week_start,
                )
            ) or 0
            we = await db.scalar(
                select(sqlfunc.sum(TaskAssignmentDB.earnings_credits)).where(
                    TaskAssignmentDB.worker_id == user.id,
                    TaskAssignmentDB.status == "approved",
                    TaskAssignmentDB.submitted_at >= week_start,
                )
            )
            worker_earnings_val = int(we or 0)
            worker_xp = worker_tasks * 10

        user_name = user.name or user.email.split("@")[0]
        await send_weekly_digest(
            to_email=user.email,
            user_name=user_name,
            week_label=week_label,
            tasks_created=tasks_created,
            tasks_completed=tasks_completed,
            credits_spent=int(credits_spent),
            credits_balance=user.credits,
            top_workers=top_workers,
            worker_tasks_done=worker_tasks,
            worker_earnings=worker_earnings_val,
            worker_xp=worker_xp,
            is_worker=is_worker,
        )
        sent += 1

    return {"sent": sent, "week": week_label}


@router.post("/digest/send-daily", tags=["admin"])
async def trigger_daily_digest(
    db: AsyncSession = Depends(get_db),
    current_user: UserDB = Depends(require_admin),
):
    """Manually trigger the daily digest to users who opted in (admin only)."""
    from core.sweeper import send_daily_digests
    from core.database import AsyncSessionLocal
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(
        AsyncSessionLocal.bind if hasattr(AsyncSessionLocal, "bind") else None,
        expire_on_commit=False,
    ) if False else None  # Use direct approach below

    # Direct implementation using current session
    from core.email import send_daily_digest
    from models.db import NotificationPreferencesDB, NotificationDB
    from sqlalchemy import func as sqlfunc

    now = datetime.now(timezone.utc)
    date_label = now.strftime("%A, %B %d, %Y")
    since = now - timedelta(hours=24)
    sent = 0

    prefs_res = await db.execute(
        select(NotificationPreferencesDB).where(
            NotificationPreferencesDB.digest_frequency == "daily"
        )
    )
    all_prefs = prefs_res.scalars().all()

    for prefs in all_prefs:
        user_res = await db.execute(
            select(UserDB).where(
                UserDB.id == prefs.user_id,
                UserDB.is_active == True,
                UserDB.is_banned == False,
            )
        )
        user = user_res.scalar_one_or_none()
        if not user:
            continue

        unread_count = await db.scalar(
            select(sqlfunc.count(NotificationDB.id)).where(
                NotificationDB.user_id == user.id,
                NotificationDB.is_read == False,
                NotificationDB.created_at >= since,
            )
        ) or 0

        notifs_res = await db.execute(
            select(NotificationDB)
            .where(
                NotificationDB.user_id == user.id,
                NotificationDB.is_read == False,
                NotificationDB.created_at >= since,
            )
            .order_by(NotificationDB.created_at.desc())
            .limit(8)
        )
        notifs = notifs_res.scalars().all()
        highlights = [
            {"title": n.title or n.notif_type, "body": n.body or "", "link": n.link or ""}
            for n in notifs
        ]

        user_name = user.name or user.email.split("@")[0]
        await send_daily_digest(
            to_email=user.email,
            user_name=user_name,
            date_label=date_label,
            unread_count=unread_count,
            highlights=highlights,
            credits_balance=user.credits,
        )
        sent += 1

    return {"sent": sent, "date": date_label}


# ─── Billing Analytics ────────────────────────────────────────────────────────

@router.get("/billing/analytics")
async def get_billing_analytics(
    months: int = Query(12, ge=1, le=24),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Revenue analytics: MRR, plan distribution, credit purchase history."""
    from sqlalchemy import extract

    now = datetime.now(timezone.utc)

    # ── Plan distribution (current snapshot) ─────────────────────────────────
    plan_dist_result = await db.execute(
        select(UserDB.plan, func.count().label("cnt"))
        .where(UserDB.is_active == True)
        .group_by(UserDB.plan)
    )
    plan_distribution = {row.plan: row.cnt for row in plan_dist_result.all()}

    # ── Plan prices (credits to USD at $0.01/credit baseline) ────────────────
    PLAN_MONTHLY_USD = {"free": 0, "starter": 9, "pro": 29, "enterprise": 99}
    # Derived MRR from active paid plan users
    mrr_usd = sum(
        PLAN_MONTHLY_USD.get(plan, 0) * count
        for plan, count in plan_distribution.items()
        if plan != "free"
    )

    # ── Monthly credit purchases (type=credit) ───────────────────────────────
    start_dt = now - timedelta(days=months * 31)
    monthly_credits_result = await db.execute(
        select(
            extract("year", CreditTransactionDB.created_at).label("yr"),
            extract("month", CreditTransactionDB.created_at).label("mo"),
            func.sum(CreditTransactionDB.amount).label("credits"),
            func.count().label("transactions"),
        )
        .where(
            CreditTransactionDB.created_at >= start_dt,
            CreditTransactionDB.type == "credit",
            CreditTransactionDB.amount > 0,
        )
        .group_by(
            extract("year", CreditTransactionDB.created_at),
            extract("month", CreditTransactionDB.created_at),
        )
        .order_by(
            extract("year", CreditTransactionDB.created_at),
            extract("month", CreditTransactionDB.created_at),
        )
    )
    monthly_purchases_raw = {
        (int(r.yr), int(r.mo)): {"credits": int(r.credits or 0), "transactions": r.transactions}
        for r in monthly_credits_result.all()
    }

    # ── Monthly charges (credits consumed by tasks) ──────────────────────────
    monthly_charges_result = await db.execute(
        select(
            extract("year", CreditTransactionDB.created_at).label("yr"),
            extract("month", CreditTransactionDB.created_at).label("mo"),
            func.sum(func.abs(CreditTransactionDB.amount)).label("credits"),
        )
        .where(
            CreditTransactionDB.created_at >= start_dt,
            CreditTransactionDB.type == "charge",
        )
        .group_by(
            extract("year", CreditTransactionDB.created_at),
            extract("month", CreditTransactionDB.created_at),
        )
        .order_by(
            extract("year", CreditTransactionDB.created_at),
            extract("month", CreditTransactionDB.created_at),
        )
    )
    monthly_charges_raw = {
        (int(r.yr), int(r.mo)): int(r.credits or 0)
        for r in monthly_charges_result.all()
    }

    # ── New paying users per month (plan != free, created in range) ──────────
    monthly_new_paid_result = await db.execute(
        select(
            extract("year", UserDB.created_at).label("yr"),
            extract("month", UserDB.created_at).label("mo"),
            func.count().label("cnt"),
        )
        .where(
            UserDB.created_at >= start_dt,
            UserDB.plan != "free",
        )
        .group_by(
            extract("year", UserDB.created_at),
            extract("month", UserDB.created_at),
        )
        .order_by(
            extract("year", UserDB.created_at),
            extract("month", UserDB.created_at),
        )
    )
    monthly_new_paid_raw = {
        (int(r.yr), int(r.mo)): r.cnt
        for r in monthly_new_paid_result.all()
    }

    # ── Build full month array ────────────────────────────────────────────────
    monthly_data = []
    for i in range(months - 1, -1, -1):
        target = now - timedelta(days=i * 30)
        yr, mo = target.year, target.month
        key = (yr, mo)
        label = target.strftime("%b %Y")
        purch = monthly_purchases_raw.get(key, {"credits": 0, "transactions": 0})
        monthly_data.append({
            "month": label,
            "credits_purchased": purch["credits"],
            "credits_consumed": monthly_charges_raw.get(key, 0),
            "new_paid_users": monthly_new_paid_raw.get(key, 0),
            "estimated_usd": round(purch["credits"] * 0.01, 2),
        })

    # ── Lifetime totals ───────────────────────────────────────────────────────
    total_credits_purchased = (await db.scalar(
        select(func.sum(CreditTransactionDB.amount))
        .where(CreditTransactionDB.type == "credit", CreditTransactionDB.amount > 0)
    )) or 0
    total_credits_consumed = (await db.scalar(
        select(func.sum(func.abs(CreditTransactionDB.amount)))
        .where(CreditTransactionDB.type == "charge")
    )) or 0
    total_paid_users = (await db.scalar(
        select(func.count()).select_from(UserDB).where(UserDB.plan != "free")
    )) or 0

    # ── Top spenders ─────────────────────────────────────────────────────────
    top_spenders_result = await db.execute(
        select(
            UserDB.id, UserDB.email, UserDB.name, UserDB.plan,
            func.sum(func.abs(CreditTransactionDB.amount)).label("spent"),
        )
        .join(CreditTransactionDB, CreditTransactionDB.user_id == UserDB.id)
        .where(CreditTransactionDB.type == "charge")
        .group_by(UserDB.id, UserDB.email, UserDB.name, UserDB.plan)
        .order_by(func.sum(func.abs(CreditTransactionDB.amount)).desc())
        .limit(10)
    )
    top_spenders = [
        {
            "id": str(r.id),
            "email": r.email,
            "name": r.name or r.email.split("@")[0],
            "plan": r.plan,
            "credits_spent": int(r.spent or 0),
            "usd_spent": round(int(r.spent or 0) * 0.01, 2),
        }
        for r in top_spenders_result.all()
    ]

    return {
        "mrr_usd": mrr_usd,
        "plan_distribution": plan_distribution,
        "total_paid_users": total_paid_users,
        "total_credits_purchased": int(total_credits_purchased),
        "total_credits_consumed": int(total_credits_consumed),
        "estimated_total_revenue_usd": round(int(total_credits_purchased) * 0.01, 2),
        "monthly_data": monthly_data,
        "top_spenders": top_spenders,
        "generated_at": now.isoformat(),
    }


# ─── Worker Management ────────────────────────────────────────────────────

class BanWorkerRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)
    expires_at: Optional[datetime] = None  # None = permanent


class AddStrikeRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)
    severity: Literal["warning", "minor", "major", "critical"] = "minor"
    expires_at: Optional[datetime] = None


@router.get("/workers")
async def list_workers(
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    search: str = Query("", max_length=100),
    status: str = Query("", description="all | banned | active"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """List workers with ban status and strike count for admin management."""
    query = select(UserDB).where(UserDB.role.in_(["worker", "both"]))
    if search:
        query = query.where(
            or_(
                UserDB.email.ilike(f"%{search}%"),
                UserDB.name.ilike(f"%{search}%"),
            )
        )
    if status == "banned":
        query = query.where(UserDB.is_banned == True)
    elif status == "active":
        query = query.where(UserDB.is_banned == False)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    result = await db.execute(
        query.order_by(UserDB.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    workers = result.scalars().all()

    items = []
    for w in workers:
        active_strikes = await db.scalar(
            select(func.count()).where(
                WorkerStrikeDB.worker_id == w.id,
                WorkerStrikeDB.is_active == True,
            )
        ) or 0
        items.append({
            "id": str(w.id),
            "email": w.email,
            "name": w.name,
            "role": w.role,
            "plan": w.plan,
            "is_banned": w.is_banned,
            "ban_reason": w.ban_reason,
            "ban_expires_at": w.ban_expires_at.isoformat() if w.ban_expires_at else None,
            "strike_count": w.strike_count,
            "active_strikes": active_strikes,
            "reputation_score": w.reputation_score,
            "worker_tasks_completed": w.worker_tasks_completed,
            "worker_level": w.worker_level,
            "worker_xp": w.worker_xp,
            "availability_status": getattr(w, "availability_status", "available"),
            "created_at": w.created_at.isoformat(),
        })

    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("/workers/{worker_id}/ban", status_code=200)
async def ban_worker(
    worker_id: UUID,
    payload: BanWorkerRequest,
    db: AsyncSession = Depends(get_db),
    admin_id: str = Depends(require_admin),
):
    """Ban a worker."""
    worker = await db.get(UserDB, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.role not in ("worker", "both"):
        raise HTTPException(400, "User is not a worker")
    worker.is_banned = True
    worker.ban_reason = payload.reason
    worker.ban_expires_at = payload.expires_at
    await log_admin_action(db, admin_id, "ban_worker", "user", str(worker_id),
                           {"reason": payload.reason, "expires_at": str(payload.expires_at)})
    await db.commit()
    logger.info("worker_banned", worker_id=str(worker_id), admin_id=admin_id, reason=payload.reason)
    return {"banned": True, "worker_id": str(worker_id)}


@router.delete("/workers/{worker_id}/ban", status_code=200)
async def unban_worker(
    worker_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin_id: str = Depends(require_admin),
):
    """Unban a worker."""
    worker = await db.get(UserDB, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    worker.is_banned = False
    worker.ban_reason = None
    worker.ban_expires_at = None
    await log_admin_action(db, admin_id, "unban_worker", "user", str(worker_id))
    await db.commit()
    logger.info("worker_unbanned", worker_id=str(worker_id), admin_id=admin_id)
    return {"unbanned": True, "worker_id": str(worker_id)}


@router.get("/workers/{worker_id}/strikes")
async def list_worker_strikes(
    worker_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """List all strikes for a worker."""
    worker = await db.get(UserDB, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    result = await db.execute(
        select(WorkerStrikeDB)
        .where(WorkerStrikeDB.worker_id == worker_id)
        .order_by(WorkerStrikeDB.created_at.desc())
    )
    strikes = result.scalars().all()
    return [
        {
            "id": str(s.id),
            "severity": s.severity,
            "reason": s.reason,
            "is_active": s.is_active,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            "created_at": s.created_at.isoformat(),
        }
        for s in strikes
    ]


@router.post("/workers/{worker_id}/strikes", status_code=201)
async def add_strike(
    worker_id: UUID,
    payload: AddStrikeRequest,
    db: AsyncSession = Depends(get_db),
    admin_id: str = Depends(require_admin),
):
    """Add a moderation strike to a worker."""
    worker = await db.get(UserDB, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.role not in ("worker", "both"):
        raise HTTPException(400, "User is not a worker")

    strike = WorkerStrikeDB(
        worker_id=worker_id,
        issued_by=UUID(admin_id),
        severity=payload.severity,
        reason=payload.reason,
        expires_at=payload.expires_at,
    )
    db.add(strike)
    worker.strike_count = (worker.strike_count or 0) + 1
    await log_admin_action(db, admin_id, "issue_strike", "user", str(worker_id),
                           {"severity": payload.severity, "reason": payload.reason})
    await db.commit()
    logger.info("worker_strike_added", worker_id=str(worker_id), admin_id=admin_id, severity=payload.severity)
    return {"strike_id": str(strike.id), "total_strikes": worker.strike_count}


@router.delete("/workers/{worker_id}/strikes/{strike_id}", status_code=200)
async def pardon_strike(
    worker_id: UUID,
    strike_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin_id: str = Depends(require_admin),
):
    """Pardon (deactivate) a strike."""
    strike = await db.get(WorkerStrikeDB, strike_id)
    if not strike or strike.worker_id != worker_id:
        raise HTTPException(404, "Strike not found")
    if not strike.is_active:
        raise HTTPException(400, "Strike is already pardoned")
    strike.is_active = False
    # Decrement strike count
    worker = await db.get(UserDB, worker_id)
    if worker and worker.strike_count > 0:
        worker.strike_count -= 1
    await log_admin_action(db, admin_id, "pardon_strike", "user", str(worker_id),
                           {"strike_id": str(strike_id)})
    await db.commit()
    logger.info("worker_strike_pardoned", strike_id=str(strike_id), admin_id=admin_id)
    return {"pardoned": True, "strike_id": str(strike_id)}


# ─── System Health ──────────────────────────────────────────────────────────

@router.get("/health")
async def get_system_health(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Real-time platform health metrics (admin only)."""
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)

    # DB ping
    t0 = _time_module.perf_counter()
    await db.execute(select(func.count()).select_from(UserDB).limit(1))
    db_ping_ms = round((_time_module.perf_counter() - t0) * 1000, 1)

    # Task queue
    pending = (await db.scalar(select(func.count()).where(TaskDB.status == "pending"))) or 0
    running = (await db.scalar(select(func.count()).where(TaskDB.status.in_(["running", "queued"])))) or 0
    open_tasks = (await db.scalar(select(func.count()).where(TaskDB.status == "open"))) or 0

    # Tasks last hour
    tasks_created_1h = (await db.scalar(
        select(func.count()).where(TaskDB.created_at >= hour_ago)
    )) or 0
    tasks_completed_1h = (await db.scalar(
        select(func.count()).where(
            TaskDB.status == "completed",
            TaskDB.completed_at >= hour_ago,
        )
    )) or 0
    tasks_failed_1h = (await db.scalar(
        select(func.count()).where(
            TaskDB.status == "failed",
            TaskDB.completed_at >= hour_ago,
        )
    )) or 0

    # Error rate (failed / (completed + failed)) in last hour
    terminal_1h = tasks_completed_1h + tasks_failed_1h
    error_rate_1h = round(tasks_failed_1h / terminal_1h, 4) if terminal_1h > 0 else 0.0

    # Top failing task types
    failing_types_result = await db.execute(
        select(TaskDB.type, func.count().label("failures"))
        .where(TaskDB.status == "failed", TaskDB.completed_at >= hour_ago)
        .group_by(TaskDB.type)
        .order_by(func.count().desc())
        .limit(5)
    )
    top_failing_types = [
        {"type": row.type, "failures": row.failures}
        for row in failing_types_result.all()
    ]

    # Total users
    total_users = (await db.scalar(select(func.count()).select_from(UserDB))) or 0

    # Active workers in last 24h (workers with approved assignments)
    active_workers_24h = (await db.scalar(
        select(func.count(TaskAssignmentDB.worker_id.distinct())).where(
            TaskAssignmentDB.claimed_at >= day_ago
        )
    )) or 0

    # Sweeper last run
    from core.sweeper import _LAST_SWEEP_AT as _sweep_ts
    sweeper_ago: Optional[float] = None
    if _sweep_ts is not None:
        sweeper_ago = round((now - _sweep_ts).total_seconds(), 1)

    # ── Stuck task detection ─────────────────────────────────────────────────
    # AI tasks running/queued for > 30 minutes
    stuck_threshold_ai = now - timedelta(minutes=30)
    stuck_ai_q = await db.execute(
        select(TaskDB.id, TaskDB.type, TaskDB.status, TaskDB.created_at)
        .where(
            TaskDB.status.in_(["running", "queued"]),
            TaskDB.execution_mode == "ai",
            TaskDB.created_at <= stuck_threshold_ai,
        )
        .order_by(TaskDB.created_at.asc())
        .limit(20)
    )
    stuck_ai_tasks = [
        {
            "id": str(r.id),
            "type": r.type,
            "status": r.status,
            "stuck_for_minutes": round((now - r.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 60),
        }
        for r in stuck_ai_q.all()
    ]

    # Human tasks open for > 24h with no assignments claimed
    stuck_threshold_human = now - timedelta(hours=24)
    stuck_human_q = await db.execute(
        select(TaskDB.id, TaskDB.type, TaskDB.created_at)
        .where(
            TaskDB.status == "open",
            TaskDB.execution_mode == "human",
            TaskDB.created_at <= stuck_threshold_human,
        )
        .order_by(TaskDB.created_at.asc())
        .limit(20)
    )
    stuck_human_tasks = [
        {
            "id": str(r.id),
            "type": r.type,
            "open_for_hours": round((now - r.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600),
        }
        for r in stuck_human_q.all()
    ]

    # Timed-out assignments (timeout_at in the past, still in claimed/in_progress state)
    timed_out_assignments_count = (await db.scalar(
        select(func.count()).where(
            TaskAssignmentDB.timeout_at != None,  # noqa: E711
            TaskAssignmentDB.timeout_at <= now,
            TaskAssignmentDB.status.in_(["claimed", "in_progress"]),
        )
    )) or 0

    # ── Overall status ───────────────────────────────────────────────────────
    total_stuck = len(stuck_ai_tasks) + len(stuck_human_tasks)
    if db_ping_ms > 500 or error_rate_1h > 0.5:
        status = "down"
    elif (
        db_ping_ms > 200
        or error_rate_1h > 0.2
        or sweeper_ago is None
        or sweeper_ago > 900
        or total_stuck > 10
    ):
        status = "degraded"
    else:
        status = "healthy"

    return {
        "status": status,
        "db_ping_ms": db_ping_ms,
        "task_queue": {
            "pending": pending,
            "running": running,
            "open": open_tasks,
        },
        "sweeper_last_run_ago_seconds": sweeper_ago,
        "tasks_last_hour": {
            "created": tasks_created_1h,
            "completed": tasks_completed_1h,
            "failed": tasks_failed_1h,
        },
        "error_rate_1h": error_rate_1h,
        "top_failing_types": top_failing_types,
        "total_users": total_users,
        "active_workers_24h": active_workers_24h,
        "stuck_tasks": {
            "ai_running_over_30m": stuck_ai_tasks,
            "human_open_over_24h": stuck_human_tasks,
            "timed_out_assignments": timed_out_assignments_count,
            "total_stuck": total_stuck,
        },
        "timestamp": now.isoformat(),
    }


# ─── Admin Audit Log ─────────────────────────────────────────────────────────

@router.get("/audit-log")
async def list_audit_log(
    action: Optional[str] = Query(None, description="Filter by action type"),
    admin_id_filter: Optional[UUID] = Query(None, alias="admin_id"),
    target_type: Optional[str] = Query(None),
    target_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Return paginated admin audit log entries, newest first."""
    query = (
        select(AdminAuditLogDB)
        .order_by(AdminAuditLogDB.created_at.desc())
    )
    if action:
        query = query.where(AdminAuditLogDB.action == action)
    if admin_id_filter:
        query = query.where(AdminAuditLogDB.admin_id == admin_id_filter)
    if target_type:
        query = query.where(AdminAuditLogDB.target_type == target_type)
    if target_id:
        query = query.where(AdminAuditLogDB.target_id == target_id)

    total = (await db.scalar(
        select(func.count(AdminAuditLogDB.id)).where(
            *([AdminAuditLogDB.action == action] if action else []),
            *([AdminAuditLogDB.admin_id == admin_id_filter] if admin_id_filter else []),
            *([AdminAuditLogDB.target_type == target_type] if target_type else []),
            *([AdminAuditLogDB.target_id == target_id] if target_id else []),
        )
    )) or 0

    result = await db.execute(query.offset(offset).limit(limit))
    entries = result.scalars().all()

    # Resolve admin names
    admin_ids = {e.admin_id for e in entries if e.admin_id}
    admin_names: dict = {}
    if admin_ids:
        users_res = await db.execute(
            select(UserDB.id, UserDB.name, UserDB.email).where(UserDB.id.in_(admin_ids))
        )
        for row in users_res:
            admin_names[str(row.id)] = row.name or row.email.split("@")[0]

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "entries": [
            {
                "id": str(e.id),
                "admin_id": str(e.admin_id) if e.admin_id else None,
                "admin_name": admin_names.get(str(e.admin_id), "Unknown") if e.admin_id else "System",
                "action": e.action,
                "target_type": e.target_type,
                "target_id": e.target_id,
                "detail": e.detail,
                "ip_address": e.ip_address,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ],
    }


# ─── System Alerts ─────────────────────────────────────────────────────────

@router.get("/alerts")
async def list_system_alerts(
    status: Optional[Literal["active", "resolved", "all"]] = "active",
    severity: Optional[Literal["critical", "warning"]] = None,
    alert_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _admin: UUID = Depends(require_admin),
):
    """List system health alerts. Defaults to active (unresolved) alerts."""
    query = select(SystemAlertDB).order_by(SystemAlertDB.created_at.desc())

    if status == "active":
        query = query.where(SystemAlertDB.resolved_at.is_(None))
    elif status == "resolved":
        query = query.where(SystemAlertDB.resolved_at.isnot(None))
    # "all" → no filter

    if severity:
        query = query.where(SystemAlertDB.severity == severity)
    if alert_type:
        query = query.where(SystemAlertDB.alert_type == alert_type)

    # Count total matching
    count_query = select(func.count(SystemAlertDB.id))
    if status == "active":
        count_query = count_query.where(SystemAlertDB.resolved_at.is_(None))
    elif status == "resolved":
        count_query = count_query.where(SystemAlertDB.resolved_at.isnot(None))
    if severity:
        count_query = count_query.where(SystemAlertDB.severity == severity)
    if alert_type:
        count_query = count_query.where(SystemAlertDB.alert_type == alert_type)

    total = (await db.scalar(count_query)) or 0

    result = await db.execute(query.offset(offset).limit(limit))
    alerts = result.scalars().all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "alerts": [
            {
                "id": str(a.id),
                "alert_type": a.alert_type,
                "severity": a.severity,
                "title": a.title,
                "detail": a.detail,
                "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
                "notified_at": a.notified_at.isoformat() if a.notified_at else None,
                "created_at": a.created_at.isoformat(),
            }
            for a in alerts
        ],
    }


@router.post("/alerts/{alert_id}/resolve")
async def resolve_system_alert(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin_id: UUID = Depends(require_admin),
):
    """Mark a system alert as resolved."""
    alert = await db.get(SystemAlertDB, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    if alert.resolved_at is not None:
        raise HTTPException(status_code=409, detail="Alert is already resolved")

    alert.resolved_at = datetime.now(timezone.utc)
    await db.commit()

    await log_admin_action(
        db=db,
        admin_id=admin_id,
        action="resolve_system_alert",
        target_type="system_alert",
        target_id=str(alert_id),
        detail={"alert_type": alert.alert_type, "severity": alert.severity},
    )
    await db.commit()

    return {"ok": True, "alert_id": str(alert_id), "resolved_at": alert.resolved_at.isoformat()}


# ─── Task Result Cache ──────────────────────────────────────────────────────

@router.get("/cache/stats")
async def get_cache_stats(
    db: AsyncSession = Depends(get_db),
    _admin_id: UUID = Depends(require_admin),
):
    """Return aggregate statistics for the task result cache.

    Includes total entries, hit counts, and estimated credits saved.
    """
    return await cache_stats(db)


@router.delete("/cache/flush", status_code=200)
async def flush_cache(
    task_type: Optional[str] = Query(None, description="Flush only this task type (omit for all)"),
    expired_only: bool = Query(False, description="Only remove expired entries"),
    db: AsyncSession = Depends(get_db),
    admin_id: UUID = Depends(require_admin),
):
    """Flush task result cache entries.

    * ``task_type`` — limit flush to a specific task type
    * ``expired_only=true`` — only remove entries whose TTL has elapsed
    """
    deleted = await cache_flush(db, task_type=task_type, expired_only=expired_only)

    await log_admin_action(
        db=db,
        admin_id=admin_id,
        action="flush_result_cache",
        target_type="cache",
        target_id=task_type or "all",
        detail={"expired_only": expired_only, "deleted": deleted},
    )
    await db.commit()

    return {"ok": True, "deleted": deleted}


# ─── Onboarding Funnel ──────────────────────────────────────────────────────

@router.get("/onboarding/funnel")
async def onboarding_funnel(
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Onboarding completion-rate funnel for requester users.

    Returns counts at each stage of the 5-step requester onboarding flow,
    from initial registration through full completion.
    """
    # Total registered requesters (role includes 'requester' or 'both')
    total_requesters_res = await db.execute(
        select(func.count()).where(
            UserDB.role.in_(["requester", "both"])
        )
    )
    total_requesters = total_requesters_res.scalar_one() or 0

    # Count onboarding rows (users who have at least *started* onboarding)
    started_res = await db.execute(
        select(func.count()).select_from(RequesterOnboardingDB)
    )
    started = started_res.scalar_one() or 0

    # Count per-step completions
    steps: list[tuple[str, str]] = [
        ("step_welcome", "Step 1: Welcome"),
        ("step_create_task", "Step 2: Create task"),
        ("step_view_results", "Step 3: View results"),
        ("step_set_webhook", "Step 4: Set webhook"),
        ("step_invite_team", "Step 5: Invite team"),
    ]
    step_counts: dict[str, int] = {}
    for col_name, _ in steps:
        col = getattr(RequesterOnboardingDB, col_name)
        res = await db.execute(
            select(func.count()).where(col == True)  # noqa: E712
        )
        step_counts[col_name] = res.scalar_one() or 0

    # Completed (all steps done, completed_at is set)
    completed_res = await db.execute(
        select(func.count()).where(RequesterOnboardingDB.completed_at != None)  # noqa: E711
    )
    completed = completed_res.scalar_one() or 0

    completion_rate = round(completed / total_requesters, 4) if total_requesters > 0 else 0.0

    funnel = [
        {"stage": "registered_requesters", "label": "Registered (requester)", "count": total_requesters},
        {"stage": "started_onboarding", "label": "Started onboarding", "count": started},
    ]
    for col_name, label in steps:
        funnel.append({"stage": col_name, "label": label, "count": step_counts[col_name]})
    funnel.append({"stage": "completed", "label": "All steps complete (+200 credits)", "count": completed})

    # Drop-off between consecutive funnel stages
    drop_off = []
    for i in range(1, len(funnel)):
        prev = funnel[i - 1]["count"]
        curr = funnel[i]["count"]
        lost = prev - curr
        pct = round(lost / prev * 100, 1) if prev > 0 else 0.0
        drop_off.append({
            "from": funnel[i - 1]["stage"],
            "to": funnel[i]["stage"],
            "dropped": lost,
            "drop_pct": pct,
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_requesters": total_requesters,
        "completion_rate": completion_rate,
        "funnel": funnel,
        "drop_off": drop_off,
    }


# ─── Worker Onboarding Funnel ────────────────────────────────────────────────

@router.get("/worker-onboarding/funnel")
async def worker_onboarding_funnel(
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Worker onboarding completion-rate funnel.

    Returns counts at each stage of the 5-step worker onboarding flow,
    from initial registration through full completion.
    """
    # Total registered workers (role includes 'worker' or 'both')
    total_workers_res = await db.execute(
        select(func.count()).where(
            UserDB.role.in_(["worker", "both"])
        )
    )
    total_workers = total_workers_res.scalar_one() or 0

    # Count onboarding rows (workers who have at least *started* onboarding)
    started_res = await db.execute(
        select(func.count()).select_from(OnboardingProgressDB)
    )
    started = started_res.scalar_one() or 0

    # Count per-step completions
    steps: list[tuple[str, str]] = [
        ("step_profile", "Step 1: Set display name"),
        ("step_explore", "Step 2: Browse marketplace"),
        ("step_first_task", "Step 3: Complete first task"),
        ("step_skills", "Step 4: View skills"),
        ("step_cert", "Step 5: Attempt certification"),
    ]
    step_counts: dict[str, int] = {}
    for col_name, _ in steps:
        col = getattr(OnboardingProgressDB, col_name)
        res = await db.execute(
            select(func.count()).where(col == True)  # noqa: E712
        )
        step_counts[col_name] = res.scalar_one() or 0

    # Completed (all steps done, completed_at is set)
    completed_res = await db.execute(
        select(func.count()).where(OnboardingProgressDB.completed_at != None)  # noqa: E711
    )
    completed = completed_res.scalar_one() or 0

    # Skipped
    skipped_res = await db.execute(
        select(func.count()).where(OnboardingProgressDB.skipped_at != None)  # noqa: E711
    )
    skipped = skipped_res.scalar_one() or 0

    # Bonus claimed
    bonus_claimed_res = await db.execute(
        select(func.count()).where(OnboardingProgressDB.bonus_claimed == True)  # noqa: E712
    )
    bonus_claimed = bonus_claimed_res.scalar_one() or 0

    completion_rate = round(completed / total_workers, 4) if total_workers > 0 else 0.0

    funnel = [
        {"stage": "registered_workers", "label": "Registered (worker)", "count": total_workers},
        {"stage": "started_onboarding", "label": "Started onboarding", "count": started},
    ]
    for col_name, label in steps:
        funnel.append({"stage": col_name, "label": label, "count": step_counts[col_name]})
    funnel.append({"stage": "completed", "label": "All steps complete (+100 credits)", "count": completed})

    # Drop-off between consecutive funnel stages
    drop_off = []
    for i in range(1, len(funnel)):
        prev = funnel[i - 1]["count"]
        curr = funnel[i]["count"]
        lost = prev - curr
        pct_val = round(lost / prev * 100, 1) if prev > 0 else 0.0
        drop_off.append({
            "from": funnel[i - 1]["stage"],
            "to": funnel[i]["stage"],
            "dropped": lost,
            "drop_pct": pct_val,
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_workers": total_workers,
        "completion_rate": completion_rate,
        "skipped": skipped,
        "bonus_claimed": bonus_claimed,
        "funnel": funnel,
        "drop_off": drop_off,
    }


@router.get("/config/status")
async def config_status(
    _admin: str = Depends(require_admin),
):
    """Return a checklist of platform configuration status.

    Shows which features are enabled/disabled based on environment variables,
    without exposing sensitive credentials. Useful for initial setup verification.
    """
    from core.config import get_settings
    s = get_settings()

    def _bool_check(value: str | bool) -> dict:
        ok = bool(value) and str(value).lower() not in ("", "0", "false", "change-me-in-production")
        return {"configured": ok}

    checks = {
        "database": {
            "name": "PostgreSQL Database",
            "configured": True,  # If we're responding, DB is connected
            "detail": s.database_url.split("@")[-1] if "@" in s.database_url else "connected",
            "required": True,
        },
        "jwt_secret": {
            "name": "JWT Secret",
            "configured": s.jwt_secret != "change-me-in-production",
            "detail": "Custom secret set" if s.jwt_secret != "change-me-in-production" else "⚠️ Using default — CHANGE IN PRODUCTION",
            "required": True,
        },
        "api_key_salt": {
            "name": "API Key Salt",
            "configured": s.api_key_salt != "change-me-in-production",
            "detail": "Custom salt set" if s.api_key_salt != "change-me-in-production" else "⚠️ Using default — CHANGE IN PRODUCTION",
            "required": True,
        },
        "rebasekit": {
            "name": "RebaseKit API (AI tasks)",
            "configured": bool(s.rebasekit_api_key),
            "detail": f"Base URL: {s.rebasekit_base_url}" if s.rebasekit_api_key else "Not set — AI tasks will fail",
            "required": True,
        },
        "email": {
            "name": "Email (SMTP)",
            "configured": s.email_enabled and bool(s.smtp_host),
            "detail": f"{s.smtp_host}:{s.smtp_port}" if s.smtp_host else "Not configured — email notifications disabled",
            "required": False,
        },
        "stripe": {
            "name": "Stripe (billing)",
            "configured": bool(s.stripe_secret_key),
            "detail": "Connected" if s.stripe_secret_key else "Not set — credit purchases disabled",
            "required": False,
        },
        "google_oauth": {
            "name": "Google OAuth (social login)",
            "configured": bool(s.google_client_id),
            "detail": f"Client ID: {s.google_client_id[:8]}..." if s.google_client_id else "Not set — Google sign-in disabled",
            "required": False,
        },
        "task_cache": {
            "name": "Task Result Cache",
            "configured": s.task_result_cache_enabled,
            "detail": "Enabled — duplicate AI calls will be deduplicated" if s.task_result_cache_enabled else "Disabled",
            "required": False,
        },
    }

    required_ok = all(v["configured"] for v in checks.values() if v.get("required"))
    all_ok = all(v["configured"] for v in checks.values())

    return {
        "ready": required_ok,
        "all_optional_configured": all_ok,
        "checks": checks,
        "summary": f"{sum(1 for v in checks.values() if v['configured'])}/{len(checks)} items configured",
    }
