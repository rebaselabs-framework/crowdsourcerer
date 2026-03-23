"""Admin API — platform statistics and user management.

Only accessible by users with is_admin=True.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.database import get_db, AsyncSessionLocal
from core.sweeper import sweep_once, get_sweeper_task
from models.db import TaskDB, UserDB, CreditTransactionDB, TaskAssignmentDB, WebhookLogDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/admin", tags=["admin"])


async def require_admin(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
) -> str:
    """Dependency: verify the caller is an admin."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id


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
    return {
        "ok": True,
        "summary": result,
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
