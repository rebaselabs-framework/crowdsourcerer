"""Direct messages between requester and worker about a task."""
from __future__ import annotations
import datetime
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_

from core.auth import get_current_user_id
from core.database import get_db
from core.notify import create_notification, NotifType
from models.db import TaskDB, TaskMessageDB, UserDB

router = APIRouter(prefix="/v1/tasks", tags=["task-messages"])


class MessageIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=2000)
    recipient_id: UUID


class MessageOut(BaseModel):
    id: UUID
    task_id: UUID
    sender_id: UUID
    sender_username: str
    recipient_id: UUID
    body: str
    is_read: bool
    created_at: datetime.datetime

    class Config:
        from_attributes = True


class UnreadCountOut(BaseModel):
    count: int


@router.post("/{task_id}/messages", response_model=MessageOut, status_code=201)
async def send_message(
    task_id: UUID,
    body: MessageIn,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Send a DM about a task. Recipient must be involved with the task."""
    task_result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    recipient_result = await db.execute(select(UserDB).where(UserDB.id == body.recipient_id))
    recipient = recipient_result.scalar_one_or_none()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")

    if body.recipient_id == UUID(user_id):
        raise HTTPException(status_code=400, detail="Cannot message yourself")

    sender_result = await db.execute(select(UserDB).where(UserDB.id == UUID(user_id)))
    sender = sender_result.scalar_one_or_none()
    sender_username = (sender.name or sender.email or "unknown") if sender else "unknown"

    msg = TaskMessageDB(
        task_id=task_id,
        sender_id=UUID(user_id),
        recipient_id=body.recipient_id,
        body=body.body,
    )
    db.add(msg)

    # Notify recipient
    task_title = str(task.type)[:60]
    await create_notification(
        db=db,
        user_id=body.recipient_id,
        type=NotifType.TASK_MESSAGE,
        title="New message",
        body=f"{sender_username} sent you a message about \u201c{task_title}\u201d",
        link=f"/dashboard/tasks/{task_id}",
    )

    await db.commit()
    await db.refresh(msg)

    return MessageOut(
        id=msg.id,
        task_id=msg.task_id,
        sender_id=msg.sender_id,
        sender_username=sender_username,
        recipient_id=msg.recipient_id,
        body=msg.body,
        is_read=msg.is_read,
        created_at=msg.created_at,
    )


@router.get("/{task_id}/messages", response_model=List[MessageOut])
async def get_task_messages(
    task_id: UUID,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get all DMs for a task (only shows messages where you are sender or recipient)."""
    task_result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    current_uuid = UUID(user_id)
    msgs_result = await db.execute(
        select(TaskMessageDB)
        .where(
            TaskMessageDB.task_id == task_id,
            or_(
                TaskMessageDB.sender_id == current_uuid,
                TaskMessageDB.recipient_id == current_uuid,
            ),
        )
        .order_by(TaskMessageDB.created_at.asc())
    )
    msgs = msgs_result.scalars().all()

    # Mark all as read where current user is recipient
    for m in msgs:
        if m.recipient_id == current_uuid and not m.is_read:
            m.is_read = True
    await db.commit()

    result = []
    for m in msgs:
        sender_result = await db.execute(select(UserDB).where(UserDB.id == m.sender_id))
        sender = sender_result.scalar_one_or_none()
        if sender:
            sender_username = sender.name or sender.email or "unknown"
        else:
            sender_username = "unknown"
        result.append(MessageOut(
            id=m.id,
            task_id=m.task_id,
            sender_id=m.sender_id,
            sender_username=sender_username,
            recipient_id=m.recipient_id,
            body=m.body,
            is_read=m.is_read,
            created_at=m.created_at,
        ))
    return result


@router.get("/messages/unread-count", response_model=UnreadCountOut)
async def get_unread_message_count(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Total unread task messages for current user."""
    from sqlalchemy import func
    count_result = await db.execute(
        select(func.count()).select_from(TaskMessageDB).where(
            TaskMessageDB.recipient_id == UUID(user_id),
            TaskMessageDB.is_read == False,  # noqa: E712
        )
    )
    count = count_result.scalar_one()
    return UnreadCountOut(count=count)
