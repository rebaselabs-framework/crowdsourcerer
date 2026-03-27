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
    base_where = [NotificationDB.user_id == user_id]
    if unread_only:
        base_where.append(NotificationDB.is_read == False)  # noqa: E712

    # Two scalar aggregates + one data fetch — simpler than subquery wrappers
    total = (await db.scalar(select(func.count()).where(*base_where))) or 0
    unread_count = (await db.scalar(
        select(func.count()).where(
            NotificationDB.user_id == user_id,
            NotificationDB.is_read == False,  # noqa: E712
        )
    )) or 0

    rows_q = await db.execute(
        select(NotificationDB)
        .where(*base_where)
        .order_by(NotificationDB.created_at.desc())
        .limit(limit)
        .offset(offset)
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
    count = (await db.scalar(
        select(func.count()).where(
            NotificationDB.user_id == user_id,
            NotificationDB.is_read == False,  # noqa: E712
        )
    )) or 0
    return UnreadCountOut(unread_count=count)


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


@router.get("/grouped")
async def get_grouped_notifications(
    limit: int = Query(100, ge=1, le=200),
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Return notifications grouped by type label.

    Each group contains:
      - type: category key (task_event, submission, payout, gamification, system)
      - label: human-readable label
      - unread_count: unread items in this group
      - items: list of notification objects (newest first, up to `limit` total across all groups)
    """
    rows_q = await db.execute(
        select(NotificationDB)
        .where(NotificationDB.user_id == user_id)
        .order_by(NotificationDB.created_at.desc())
        .limit(limit)
    )
    all_notifs = rows_q.scalars().all()

    # Group mapping — type_key → NotificationType prefix patterns
    TYPE_MAP = {
        "task_event": {"TASK_COMPLETED", "TASK_FAILED", "TASK_CREATED", "SLA_BREACH"},
        "submission": {"SUBMISSION_RECEIVED", "WORKER_APPROVED", "WORKER_REJECTED",
                       "TASK_ASSIGNED"},
        "payout": {"PAYOUT_INITIATED", "PAYOUT_COMPLETED", "PAYOUT_FAILED"},
        "gamification": {"BADGE_EARNED", "LEVEL_UP", "CHALLENGE_COMPLETED",
                         "LEADERBOARD_RANK", "REFERRAL_BONUS"},
        "system": set(),  # catch-all
    }
    TYPE_LABELS = {
        "task_event": "Task Events",
        "submission": "Submissions & Assignments",
        "payout": "Payouts",
        "gamification": "Achievements & Rewards",
        "system": "System",
    }

    def _group_key(notif_type: str) -> str:
        for key, prefixes in TYPE_MAP.items():
            if key == "system":
                continue
            if notif_type in prefixes:
                return key
        return "system"

    groups: dict[str, dict] = {
        k: {"type": k, "label": TYPE_LABELS[k], "unread_count": 0, "items": []}
        for k in ["task_event", "submission", "payout", "gamification", "system"]
    }
    for n in all_notifs:
        key = _group_key(n.type.value if hasattr(n.type, "value") else str(n.type))
        groups[key]["items"].append({
            "id": str(n.id),
            "type": n.type.value if hasattr(n.type, "value") else str(n.type),
            "title": n.title,
            "body": n.body,
            "link": n.link,
            "is_read": n.is_read,
            "created_at": n.created_at.isoformat(),
        })
        if not n.is_read:
            groups[key]["unread_count"] += 1

    # Remove empty groups
    return {
        "groups": [g for g in groups.values() if g["items"]],
        "total_unread": sum(g["unread_count"] for g in groups.values()),
    }


@router.delete("/all")
async def delete_all_notifications(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Delete ALL notifications for this user."""
    from sqlalchemy import delete as sa_delete
    await db.execute(
        sa_delete(NotificationDB).where(NotificationDB.user_id == user_id)
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
    email_task_available: Optional[bool] = None
    email_referral_bonus: Optional[bool] = None
    email_sla_breach: Optional[bool] = None
    # In-app
    notif_task_events: Optional[bool] = None
    notif_submissions: Optional[bool] = None
    notif_payouts: Optional[bool] = None
    notif_gamification: Optional[bool] = None
    notif_system: Optional[bool] = None
    # Digest
    digest_frequency: Optional[str] = None  # none | daily | weekly


def _prefs_to_dict(prefs: NotificationPreferencesDB) -> dict:
    return {
        "email_task_completed": prefs.email_task_completed,
        "email_task_failed": prefs.email_task_failed,
        "email_submission_received": prefs.email_submission_received,
        "email_worker_approved": prefs.email_worker_approved,
        "email_payout_update": prefs.email_payout_update,
        "email_daily_challenge": prefs.email_daily_challenge,
        "email_task_available": prefs.email_task_available,
        "email_referral_bonus": prefs.email_referral_bonus,
        "email_sla_breach": prefs.email_sla_breach,
        "notif_task_events": prefs.notif_task_events,
        "notif_submissions": prefs.notif_submissions,
        "notif_payouts": prefs.notif_payouts,
        "notif_gamification": prefs.notif_gamification,
        "notif_system": prefs.notif_system,
        "digest_frequency": prefs.digest_frequency,
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
        "email_task_available": False,
        "email_referral_bonus": True,
        "email_sla_breach": True,
        "notif_task_events": True,
        "notif_submissions": True,
        "notif_payouts": True,
        "notif_gamification": True,
        "notif_system": True,
        "digest_frequency": "weekly",
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
        if field == "digest_frequency" and value not in ("none", "daily", "weekly"):
            continue  # ignore invalid values
        setattr(prefs, field, value)

    await db.commit()
    await db.refresh(prefs)
    return _prefs_to_dict(prefs)
