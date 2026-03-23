"""Webhook delivery logs + event type catalogue."""
from __future__ import annotations
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.database import get_db
from core.webhooks import ALL_EVENTS, DEFAULT_EVENTS, retry_webhook_log
from models.db import WebhookLogDB, TaskDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.get("/events")
async def list_event_types():
    """Return the full catalogue of supported webhook event types."""
    descriptions = {
        "task.created": "Fired when a task is first created.",
        "task.assigned": "Fired when a worker claims a human task.",
        "task.submission_received": "Fired when a worker submits work on a human task.",
        "task.completed": "Fired when a task finishes successfully (AI result or approved submission).",
        "task.failed": "Fired when a task fails permanently.",
        "task.approved": "Fired when a requester explicitly approves a submission.",
        "task.rejected": "Fired when a requester rejects a submission.",
        "sla.breach": "Fired when a task's SLA deadline is exceeded.",
    }
    return {
        "events": [
            {
                "type": e,
                "description": descriptions.get(e, ""),
                "is_default": e in DEFAULT_EVENTS,
            }
            for e in ALL_EVENTS
        ],
        "default_events": DEFAULT_EVENTS,
    }


@router.get("/logs")
async def list_webhook_logs(
    task_id: Optional[UUID] = Query(None, description="Filter by task ID"),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    success: Optional[bool] = Query(None, description="Filter by success/failure"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List webhook delivery logs for tasks owned by the current user."""
    q = select(WebhookLogDB).where(WebhookLogDB.user_id == user_id)

    if task_id:
        q = q.where(WebhookLogDB.task_id == task_id)
    if event_type:
        q = q.where(WebhookLogDB.event_type == event_type)
    if success is not None:
        q = q.where(WebhookLogDB.success == success)

    total = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar() or 0

    q = q.order_by(WebhookLogDB.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    logs = (await db.execute(q)).scalars().all()

    return {
        "items": [
            {
                "id": str(log.id),
                "task_id": str(log.task_id),
                "url": log.url,
                "event_type": log.event_type or "task.completed",
                "attempt": log.attempt,
                "status_code": log.status_code,
                "success": log.success,
                "error": log.error,
                "duration_ms": log.duration_ms,
                "created_at": log.created_at.isoformat(),
            }
            for log in logs
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": (page * page_size) < total,
    }


@router.post("/logs/{log_id}/retry")
async def retry_webhook(
    log_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Manually retry a single webhook delivery.

    Creates a new log entry with `is_manual_retry=true` linked to the original.
    Returns the result of the retry attempt.
    """
    try:
        result = await retry_webhook_log(log_id=str(log_id), user_id=str(user_id))
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return result


@router.get("/stats")
async def webhook_stats(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Summary stats for the user's webhook deliveries."""
    total = (await db.execute(
        select(func.count()).select_from(WebhookLogDB).where(WebhookLogDB.user_id == user_id)
    )).scalar() or 0

    succeeded = (await db.execute(
        select(func.count()).select_from(WebhookLogDB).where(
            WebhookLogDB.user_id == user_id,
            WebhookLogDB.success == True,
        )
    )).scalar() or 0

    failed = total - succeeded

    avg_duration = (await db.execute(
        select(func.avg(WebhookLogDB.duration_ms)).select_from(WebhookLogDB).where(
            WebhookLogDB.user_id == user_id,
            WebhookLogDB.success == True,
        )
    )).scalar()

    # Breakdown by event type
    event_rows = (await db.execute(
        select(WebhookLogDB.event_type, func.count().label("cnt"))
        .where(WebhookLogDB.user_id == user_id)
        .group_by(WebhookLogDB.event_type)
    )).all()
    by_event = {r.event_type or "task.completed": r.cnt for r in event_rows}

    return {
        "total_deliveries": total,
        "succeeded": succeeded,
        "failed": failed,
        "success_rate": round(succeeded / total * 100, 1) if total > 0 else 100.0,
        "avg_duration_ms": round(avg_duration) if avg_duration else None,
        "by_event_type": by_event,
    }
