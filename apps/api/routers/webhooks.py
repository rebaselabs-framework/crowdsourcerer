"""Webhook delivery logs — lets requesters see delivery history for their tasks."""
from __future__ import annotations
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.database import get_db
from models.db import WebhookLogDB, TaskDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.get("/logs")
async def list_webhook_logs(
    task_id: Optional[UUID] = Query(None, description="Filter by task ID"),
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

    return {
        "total_deliveries": total,
        "succeeded": succeeded,
        "failed": failed,
        "success_rate": round(succeeded / total * 100, 1) if total > 0 else 100.0,
        "avg_duration_ms": round(avg_duration) if avg_duration else None,
    }
