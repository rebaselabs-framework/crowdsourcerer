"""Notification digest preferences + test-send endpoint."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from models.db import (
    NotificationPreferencesDB,
    TaskDB,
    TaskAssignmentDB,
    CreditTransactionDB,
    UserDB,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/notifications", tags=["notifications"])

# ─── Default digest prefs ─────────────────────────────────────────────────────

_DIGEST_DEFAULTS = {
    "enabled": True,
    "frequency": "daily",
    "send_at_hour": 8,
    "include_task_updates": True,
    "include_worker_activity": True,
    "include_credit_changes": True,
}


# ─── Schemas ──────────────────────────────────────────────────────────────────

class DigestPrefsUpdate(BaseModel):
    enabled: Optional[bool] = None
    frequency: Optional[str] = Field(None, pattern="^(daily|weekly)$")
    send_at_hour: Optional[int] = Field(None, ge=0, le=23)
    include_task_updates: Optional[bool] = None
    include_worker_activity: Optional[bool] = None
    include_credit_changes: Optional[bool] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _get_or_create_prefs(
    user_id: UUID, db: AsyncSession
) -> NotificationPreferencesDB:
    result = await db.execute(
        select(NotificationPreferencesDB).where(
            NotificationPreferencesDB.user_id == user_id
        )
    )
    prefs = result.scalar_one_or_none()
    if not prefs:
        prefs = NotificationPreferencesDB(user_id=user_id)
        db.add(prefs)
        await db.flush()
    return prefs


def _digest_prefs_dict(prefs: NotificationPreferencesDB) -> dict:
    stored = prefs.notification_digest_prefs or {}
    merged = {**_DIGEST_DEFAULTS, **stored}
    return merged


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/digest-prefs")
async def get_digest_prefs(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user's notification digest preferences."""
    prefs = await _get_or_create_prefs(user_id, db)
    return _digest_prefs_dict(prefs)


@router.put("/digest-prefs")
async def update_digest_prefs(
    body: DigestPrefsUpdate,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Update notification digest preferences (partial update)."""
    prefs = await _get_or_create_prefs(user_id, db)
    current = dict(_digest_prefs_dict(prefs))

    updates = body.model_dump(exclude_none=True)
    current.update(updates)

    prefs.notification_digest_prefs = current
    await db.commit()
    await db.refresh(prefs)
    return _digest_prefs_dict(prefs)


@router.post("/digest/send-test")
async def send_test_digest(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Build a real digest from the last 7 days of data and send (or log if email disabled).

    Builds a real digest from the last 7 days of the user's data:
      - Stats: total tasks, completions, credits spent
      - Recent task updates (up to 5 most recent)
      - Pending tasks summary
    """
    from core.email import send_weekly_digest
    from core.config import get_settings
    EMAIL_ENABLED = get_settings().email_enabled
    from models.db import NotificationDB

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    week_label = now.strftime("%-d %b %Y")

    # Fetch user
    user_res = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    user_name = user.name or user.email.split("@")[0]

    # Stats: total tasks created in last 7 days
    total_tasks = await db.scalar(
        select(func.count(TaskDB.id)).where(
            TaskDB.user_id == user_id,
            TaskDB.created_at >= week_ago,
        )
    ) or 0

    # Completions
    completions = await db.scalar(
        select(func.count(TaskDB.id)).where(
            TaskDB.user_id == user_id,
            TaskDB.status == "completed",
            TaskDB.completed_at >= week_ago,
        )
    ) or 0

    # Credits spent (negative transactions)
    credits_spent_raw = await db.scalar(
        select(func.abs(func.sum(CreditTransactionDB.amount))).where(
            CreditTransactionDB.user_id == user_id,
            CreditTransactionDB.amount < 0,
            CreditTransactionDB.created_at >= week_ago,
        )
    )
    credits_spent = int(credits_spent_raw or 0)

    # Unread notifications for worker stats
    unread_count = await db.scalar(
        select(func.count(NotificationDB.id)).where(
            NotificationDB.user_id == user_id,
            NotificationDB.is_read == False,  # noqa: E712
        )
    ) or 0

    # Top workers (workers who submitted tasks for this requester in last 7 days)
    from models.db import TaskAssignmentDB
    workers_res = await db.execute(
        select(UserDB.name, UserDB.email, func.count(TaskAssignmentDB.id).label("tasks"))
        .join(TaskDB, TaskDB.id == TaskAssignmentDB.task_id)
        .join(UserDB, UserDB.id == TaskAssignmentDB.worker_id)
        .where(
            TaskDB.user_id == user_id,
            TaskAssignmentDB.claimed_at >= week_ago,
        )
        .group_by(UserDB.id, UserDB.name, UserDB.email)
        .order_by(func.count(TaskAssignmentDB.id).desc())
        .limit(5)
    )
    top_workers = [
        {"name": r.name or r.email.split("@")[0], "tasks": r.tasks}
        for r in workers_res
    ]

    # Recent task updates
    recent_res = await db.execute(
        select(TaskDB)
        .where(TaskDB.user_id == user_id)
        .order_by(TaskDB.created_at.desc())
        .limit(5)
    )
    recent_tasks = recent_res.scalars().all()
    recent_updates = [
        {
            "id": str(t.id),
            "type": t.type,
            "status": t.status,
            "created_at": t.created_at.isoformat(),
        }
        for t in recent_tasks
    ]

    # Pending tasks summary
    pending_res = await db.execute(
        select(TaskDB).where(
            TaskDB.user_id == user_id,
            TaskDB.status.in_(["pending", "open", "queued", "running", "assigned"]),
        )
    )
    pending_tasks = pending_res.scalars().all()
    pending_summary = [
        {
            "id": str(t.id),
            "type": t.type,
            "status": t.status,
            "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
        }
        for t in pending_tasks
    ]

    digest_content = {
        "to": user.email,
        "user_name": user_name,
        "period": "Last 7 days",
        "generated_at": now.isoformat(),
        "stats": {
            "total_tasks": total_tasks,
            "completions": completions,
            "credits_spent": credits_spent,
            "credits_balance": user.credits,
        },
        "recent_task_updates": recent_updates,
        "pending_tasks_summary": pending_summary,
        "pending_count": len(pending_summary),
        "email_enabled": EMAIL_ENABLED,
    }

    logger.info(
        "digest.test_send",
        user_id=str(user_id),
        email=user.email,
        total_tasks=total_tasks,
        completions=completions,
        credits_spent=credits_spent,
        email_enabled=EMAIL_ENABLED,
    )

    # ── Send real email if enabled ────────────────────────────────────────
    email_sent = False
    email_error: Optional[str] = None
    if EMAIL_ENABLED:
        try:
            await send_weekly_digest(
                to_email=user.email,
                user_name=user_name,
                week_label=f"Test — {week_label}",
                tasks_created=total_tasks,
                tasks_completed=completions,
                credits_spent=credits_spent,
                credits_balance=user.credits,
                top_workers=top_workers,
            )
            email_sent = True
        except Exception as exc:
            email_error = str(exc)
            logger.exception("digest.test_send_error", user_id=str(user_id))
    else:
        email_error = "EMAIL_ENABLED=false — email not sent (log-only mode)"

    message = (
        f"Test digest sent to {user.email}" if email_sent
        else f"Test digest logged (email disabled). {email_error or ''}"
    )

    return {
        "ok": True,
        "email_sent": email_sent,
        "message": message,
        "digest": digest_content,
    }
