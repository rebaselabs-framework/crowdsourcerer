"""In-app notification endpoints + notification preferences."""
from __future__ import annotations

from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from models.db import NotificationDB, NotificationPreferencesDB
from models.schemas import NotificationListOut, NotificationOut, UnreadCountOut

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListOut)
async def list_notifications(
    unread_only: bool = Query(False, description="Return only unread notifications"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Return paginated notifications for the authenticated user."""
    base = select(NotificationDB).where(NotificationDB.user_id == user_id)
    if unread_only:
        base = base.where(NotificationDB.is_read == False)  # noqa: E712

    total_q = await db.execute(select(func.count()).select_from(base.subquery()))
    total = total_q.scalar_one()

    unread_q = await db.execute(
        select(func.count()).select_from(
            select(NotificationDB)
            .where(NotificationDB.user_id == user_id)
            .where(NotificationDB.is_read == False)  # noqa: E712
            .subquery()
        )
    )
    unread_count = unread_q.scalar_one()

    rows_q = await db.execute(
        base.order_by(NotificationDB.created_at.desc()).limit(limit).offset(offset)
    )
    items = rows_q.scalars().all()

    return NotificationListOut(
        items=items,
        total=total,
        unread_count=unread_count,
    )


@router.get("/unread-count", response_model=UnreadCountOut)
async def get_unread_count(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Return just the unread notification count (lightweight for nav badge)."""
    result = await db.execute(
        select(func.count()).select_from(
            select(NotificationDB)
            .where(NotificationDB.user_id == user_id)
            .where(NotificationDB.is_read == False)  # noqa: E712
            .subquery()
        )
    )
    return UnreadCountOut(unread_count=result.scalar_one())


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Mark a single notification as read."""
    result = await db.execute(
        select(NotificationDB).where(
            NotificationDB.id == notification_id,
            NotificationDB.user_id == user_id,
        )
    )
    notif = result.scalar_one_or_none()
    if not notif:
        raise HTTPException(404, "Notification not found")

    notif.is_read = True
    await db.commit()
    await db.refresh(notif)
    return notif


@router.post("/read-all")
async def mark_all_read(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Mark all notifications for this user as read."""
    await db.execute(
        update(NotificationDB)
        .where(NotificationDB.user_id == user_id)
        .where(NotificationDB.is_read == False)  # noqa: E712
        .values(is_read=True)
    )
    await db.commit()
    return {"ok": True}


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single notification."""
    result = await db.execute(
        select(NotificationDB).where(
            NotificationDB.id == notification_id,
            NotificationDB.user_id == user_id,
        )
    )
    notif = result.scalar_one_or_none()
    if not notif:
        raise HTTPException(404, "Notification not found")

    await db.delete(notif)
    await db.commit()
    return {"ok": True}


# ─── Notification Preferences ──────────────────────────────────────────────

class NotificationPreferencesUpdate(BaseModel):
    """All fields optional — only provided fields are updated."""
    # Email
    email_task_completed: Optional[bool] = None
    email_task_failed: Optional[bool] = None
    email_submission_received: Optional[bool] = None
    email_worker_approved: Optional[bool] = None
    email_payout_update: Optional[bool] = None
    email_daily_challenge: Optional[bool] = None
    email_referral_bonus: Optional[bool] = None
    email_sla_breach: Optional[bool] = None
    # In-app
    notif_task_events: Optional[bool] = None
    notif_submissions: Optional[bool] = None
    notif_payouts: Optional[bool] = None
    notif_gamification: Optional[bool] = None
    notif_system: Optional[bool] = None


def _prefs_to_dict(prefs: NotificationPreferencesDB) -> dict:
    return {
        "email_task_completed": prefs.email_task_completed,
        "email_task_failed": prefs.email_task_failed,
        "email_submission_received": prefs.email_submission_received,
        "email_worker_approved": prefs.email_worker_approved,
        "email_payout_update": prefs.email_payout_update,
        "email_daily_challenge": prefs.email_daily_challenge,
        "email_referral_bonus": prefs.email_referral_bonus,
        "email_sla_breach": prefs.email_sla_breach,
        "notif_task_events": prefs.notif_task_events,
        "notif_submissions": prefs.notif_submissions,
        "notif_payouts": prefs.notif_payouts,
        "notif_gamification": prefs.notif_gamification,
        "notif_system": prefs.notif_system,
        "updated_at": prefs.updated_at.isoformat() if prefs.updated_at else None,
    }


def _default_prefs_dict() -> dict:
    """Return the factory-default preferences (no DB row exists yet)."""
    return {
        "email_task_completed": True,
        "email_task_failed": True,
        "email_submission_received": True,
        "email_worker_approved": True,
        "email_payout_update": True,
        "email_daily_challenge": False,
        "email_referral_bonus": True,
        "email_sla_breach": True,
        "notif_task_events": True,
        "notif_submissions": True,
        "notif_payouts": True,
        "notif_gamification": True,
        "notif_system": True,
        "updated_at": None,
    }


@router.get("/preferences")
async def get_notification_preferences(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user's notification preferences."""
    result = await db.execute(
        select(NotificationPreferencesDB).where(
            NotificationPreferencesDB.user_id == user_id
        )
    )
    prefs = result.scalar_one_or_none()
    if not prefs:
        return _default_prefs_dict()
    return _prefs_to_dict(prefs)


@router.put("/preferences")
async def update_notification_preferences(
    body: NotificationPreferencesUpdate,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Update notification preferences. Only provided fields are changed."""
    result = await db.execute(
        select(NotificationPreferencesDB).where(
            NotificationPreferencesDB.user_id == user_id
        )
    )
    prefs = result.scalar_one_or_none()

    if not prefs:
        # Create a fresh row with defaults
        prefs = NotificationPreferencesDB(user_id=user_id)
        db.add(prefs)

    # Apply only the fields that were explicitly sent
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(prefs, field, value)

    await db.commit()
    await db.refresh(prefs)
    return _prefs_to_dict(prefs)
