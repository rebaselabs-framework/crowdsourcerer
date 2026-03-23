"""In-app notification endpoints."""
from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from models.db import NotificationDB
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
