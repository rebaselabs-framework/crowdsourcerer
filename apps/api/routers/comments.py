"""Task comment/discussion API."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from core.notify import create_notification, NotifType
from models.db import TaskCommentDB, TaskDB, TaskAssignmentDB, UserDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

MAX_BODY = 500
MAX_COMMENTS_PER_TASK = 200


class CommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=MAX_BODY)
    parent_id: Optional[UUID] = None
    is_internal: bool = False


class CommentEdit(BaseModel):
    body: str = Field(..., min_length=1, max_length=MAX_BODY)


def _comment_out(c: TaskCommentDB, author: UserDB) -> dict:
    return {
        "id": str(c.id),
        "task_id": str(c.task_id),
        "user_id": str(c.user_id),
        "author_name": author.name or author.email.split("@")[0],
        "parent_id": str(c.parent_id) if c.parent_id else None,
        "body": c.body,
        "is_internal": c.is_internal,
        "edited_at": c.edited_at.isoformat() if c.edited_at else None,
        "created_at": c.created_at.isoformat(),
    }


@router.get("/{task_id}/comments")
async def list_comments(
    task_id: UUID,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List comments on a task (requester + their assigned workers only)."""
    task = await db.get(TaskDB, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    current_user = await db.get(UserDB, user_id)

    # Access check: must be requester or have an assignment
    is_requester = str(task.user_id) == user_id
    if not is_requester and not current_user.is_admin:
        asgn = await db.scalar(
            select(func.count()).where(
                and_(
                    TaskAssignmentDB.task_id == task_id,
                    TaskAssignmentDB.worker_id == user_id,
                )
            )
        )
        if not asgn:
            raise HTTPException(403, "Not authorized to view comments on this task")

    query = select(TaskCommentDB).where(TaskCommentDB.task_id == task_id)
    # Workers don't see internal notes
    if not is_requester and not current_user.is_admin:
        query = query.where(TaskCommentDB.is_internal == False)  # noqa: E712
    query = query.order_by(TaskCommentDB.created_at.asc())

    total = await db.scalar(select(func.count()).where(
        TaskCommentDB.task_id == task_id,
        *([TaskCommentDB.is_internal == False] if not is_requester and not current_user.is_admin else [])  # noqa: E712
    )) or 0

    result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    comments = result.scalars().all()

    # Fetch authors in one shot
    author_ids = list({c.user_id for c in comments})
    authors_res = await db.execute(select(UserDB).where(UserDB.id.in_(author_ids)))
    authors = {u.id: u for u in authors_res.scalars().all()}

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "comments": [_comment_out(c, authors[c.user_id]) for c in comments if c.user_id in authors],
    }


@router.post("/{task_id}/comments", status_code=201)
async def post_comment(
    task_id: UUID,
    payload: CommentCreate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Post a comment on a task."""
    task = await db.get(TaskDB, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    current_user = await db.get(UserDB, user_id)

    is_requester = str(task.user_id) == user_id

    # Access check
    if not is_requester and not current_user.is_admin:
        asgn = await db.scalar(
            select(func.count()).where(
                and_(
                    TaskAssignmentDB.task_id == task_id,
                    TaskAssignmentDB.worker_id == user_id,
                )
            )
        )
        if not asgn:
            raise HTTPException(403, "You are not assigned to this task")

    # Only requester can post internal notes
    if payload.is_internal and not is_requester and not current_user.is_admin:
        raise HTTPException(403, "Only the requester can post internal notes")

    # Comment cap
    count = await db.scalar(
        select(func.count()).where(TaskCommentDB.task_id == task_id)
    ) or 0
    if count >= MAX_COMMENTS_PER_TASK:
        raise HTTPException(429, "Comment limit reached for this task")

    # Validate parent
    if payload.parent_id:
        parent = await db.get(TaskCommentDB, payload.parent_id)
        if not parent or parent.task_id != task_id:
            raise HTTPException(404, "Parent comment not found")

    comment = TaskCommentDB(
        task_id=task_id,
        user_id=user_id,
        parent_id=payload.parent_id,
        body=payload.body.strip(),
        is_internal=payload.is_internal,
    )
    db.add(comment)
    await db.flush()

    # Notify the other party
    author_name = current_user.name or current_user.email.split("@")[0]
    if is_requester:
        # Notify all assigned workers (skip internal notes)
        if not payload.is_internal:
            workers_res = await db.execute(
                select(TaskAssignmentDB).where(
                    and_(
                        TaskAssignmentDB.task_id == task_id,
                        TaskAssignmentDB.status.in_(["active", "submitted"]),
                    )
                )
            )
            for asgn in workers_res.scalars().all():
                await create_notification(
                    db,
                    user_id=asgn.worker_id,
                    type=NotifType.COMMENT_RECEIVED,
                    title="New comment on your task",
                    body=f"{author_name} left a comment on task #{str(task_id)[:8]}",
                    link=f"/dashboard/tasks/{task_id}#comments",
                )
    else:
        # Notify requester
        await create_notification(
            db,
            user_id=task.user_id,
            type=NotifType.COMMENT_RECEIVED,
            title="New comment on your task",
            body=f"{author_name} left a comment on task #{str(task_id)[:8]}",
            link=f"/dashboard/tasks/{task_id}#comments",
        )

    await db.commit()
    await db.refresh(comment)

    return _comment_out(comment, current_user)


@router.patch("/{task_id}/comments/{comment_id}")
async def edit_comment(
    task_id: UUID,
    comment_id: UUID,
    payload: CommentEdit,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Edit own comment (within 15 minutes of posting)."""
    comment = await db.get(TaskCommentDB, comment_id)
    if not comment or comment.task_id != task_id:
        raise HTTPException(404, "Comment not found")
    current_user = await db.get(UserDB, user_id)

    if str(comment.user_id) != user_id and not current_user.is_admin:
        raise HTTPException(403, "Can only edit your own comments")

    age = (datetime.now(timezone.utc) - comment.created_at).total_seconds()
    if age > 900 and not current_user.is_admin:  # 15 minutes
        raise HTTPException(403, "Comments can only be edited within 15 minutes of posting")

    comment.body = payload.body.strip()
    comment.edited_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(comment)

    return _comment_out(comment, current_user)


@router.delete("/{task_id}/comments/{comment_id}", status_code=204, response_model=None)
async def delete_comment(
    task_id: UUID,
    comment_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Delete a comment (own comment or admin)."""
    comment = await db.get(TaskCommentDB, comment_id)
    if not comment or comment.task_id != task_id:
        raise HTTPException(404, "Comment not found")
    current_user = await db.get(UserDB, user_id)

    if str(comment.user_id) != user_id and not current_user.is_admin:
        raise HTTPException(403, "Can only delete your own comments")

    await db.delete(comment)
    await db.commit()
