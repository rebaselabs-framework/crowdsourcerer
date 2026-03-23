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
    Generate a sample digest and log it (mock send).

    Builds a real digest from the last 7 days of the user's data:
      - Stats: total tasks, completions, credits spent
      - Recent task updates (up to 5 most recent)
      - Pending tasks summary
    """
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    # Fetch user
    user_res = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

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

    # Recent task updates (most recently updated, up to 5)
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
        "user_name": user.name or user.email.split("@")[0],
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
    }

    logger.info(
        "digest.test_send",
        user_id=str(user_id),
        email=user.email,
        total_tasks=total_tasks,
        completions=completions,
        credits_spent=credits_spent,
        pending_count=len(pending_summary),
        digest=digest_content,
    )

    return {
        "ok": True,
        "message": f"Test digest logged (mock send to {user.email})",
        "digest": digest_content,
    }
