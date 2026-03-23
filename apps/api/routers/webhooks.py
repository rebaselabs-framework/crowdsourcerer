"""Webhook endpoint management + delivery logs + event type catalogue."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.database import get_db
from core.webhooks import ALL_EVENTS, DEFAULT_EVENTS, retry_webhook_log
from models.db import WebhookLogDB, WebhookEndpointDB
from models.schemas import (
    WebhookEndpointCreate,
    WebhookEndpointUpdate,
    WebhookEndpointOut,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


# ─── Event catalogue ────────────────────────────────────────────────────────

EVENT_DESCRIPTIONS = {
    "task.created": "Fired when a task is first created.",
    "task.assigned": "Fired when a worker claims a human task.",
    "task.submission_received": "Fired when a worker submits work on a human task.",
    "task.completed": "Fired when a task finishes successfully (AI result or approved submission).",
    "task.failed": "Fired when a task fails permanently.",
    "task.approved": "Fired when a requester explicitly approves a submission.",
    "task.rejected": "Fired when a requester rejects a submission.",
    "sla.breach": "Fired when a task's SLA deadline is exceeded.",
}


@router.get("/events")
async def list_event_types():
    """Return the full catalogue of supported webhook event types."""
    return {
        "events": [
            {
                "type": e,
                "description": EVENT_DESCRIPTIONS.get(e, ""),
                "is_default": e in DEFAULT_EVENTS,
            }
            for e in ALL_EVENTS
        ],
        "default_events": DEFAULT_EVENTS,
    }


# ─── Persistent endpoint CRUD ────────────────────────────────────────────────

@router.get("/endpoints")
async def list_endpoints(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List all persistent webhook endpoints registered by the current user."""
    result = await db.execute(
        select(WebhookEndpointDB)
        .where(WebhookEndpointDB.user_id == user_id)
        .order_by(WebhookEndpointDB.created_at.desc())
    )
    endpoints = result.scalars().all()
    # Don't expose secret on list
    return {
        "items": [
            {
                "id": str(ep.id),
                "url": ep.url,
                "description": ep.description,
                "events": ep.events,
                "is_active": ep.is_active,
                "delivery_count": ep.delivery_count,
                "failure_count": ep.failure_count,
                "last_triggered_at": ep.last_triggered_at.isoformat() if ep.last_triggered_at else None,
                "last_failure_at": ep.last_failure_at.isoformat() if ep.last_failure_at else None,
                "created_at": ep.created_at.isoformat(),
            }
            for ep in endpoints
        ],
        "total": len(endpoints),
    }


@router.post("/endpoints", status_code=201)
async def create_endpoint(
    body: WebhookEndpointCreate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Register a new persistent webhook endpoint.

    Returns the endpoint record including the signing `secret` (shown **once**).
    Use the secret to verify `X-Crowdsourcerer-Signature` on incoming requests:

        sig = hmac.new(secret.encode(), payload_bytes, sha256).hexdigest()
        assert sig == request.headers["X-Crowdsourcerer-Signature"]
    """
    # Validate events
    if body.events:
        bad = [e for e in body.events if e not in ALL_EVENTS]
        if bad:
            raise HTTPException(400, f"Unknown event types: {bad}. Valid: {ALL_EVENTS}")

    # Enforce per-user cap
    count = (await db.execute(
        select(func.count()).select_from(WebhookEndpointDB).where(
            WebhookEndpointDB.user_id == user_id
        )
    )).scalar() or 0
    if count >= 20:
        raise HTTPException(400, "Maximum of 20 webhook endpoints per account reached.")

    secret = secrets.token_urlsafe(32)
    ep = WebhookEndpointDB(
        user_id=user_id,
        url=body.url,
        description=body.description,
        events=body.events,
        secret=secret,
    )
    db.add(ep)
    await db.commit()
    await db.refresh(ep)
    logger.info("webhook_endpoint_created", endpoint_id=str(ep.id), user_id=str(user_id))

    data = {
        "id": str(ep.id),
        "url": ep.url,
        "description": ep.description,
        "events": ep.events,
        "is_active": ep.is_active,
        "delivery_count": ep.delivery_count,
        "failure_count": ep.failure_count,
        "last_triggered_at": None,
        "last_failure_at": None,
        "created_at": ep.created_at.isoformat(),
        # Secret only returned on creation
        "secret": secret,
    }
    return data


@router.patch("/endpoints/{endpoint_id}")
async def update_endpoint(
    endpoint_id: UUID,
    body: WebhookEndpointUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Update URL, description, subscribed events, or active state."""
    ep = await _get_owned_endpoint(endpoint_id, user_id, db)

    if body.url is not None:
        ep.url = body.url
    if body.description is not None:
        ep.description = body.description
    if body.events is not None:
        bad = [e for e in body.events if e not in ALL_EVENTS]
        if bad:
            raise HTTPException(400, f"Unknown event types: {bad}")
        ep.events = body.events
    if body.is_active is not None:
        ep.is_active = body.is_active

    ep.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(ep)
    return {
        "id": str(ep.id),
        "url": ep.url,
        "description": ep.description,
        "events": ep.events,
        "is_active": ep.is_active,
        "delivery_count": ep.delivery_count,
        "failure_count": ep.failure_count,
        "last_triggered_at": ep.last_triggered_at.isoformat() if ep.last_triggered_at else None,
        "last_failure_at": ep.last_failure_at.isoformat() if ep.last_failure_at else None,
        "created_at": ep.created_at.isoformat(),
    }


@router.delete("/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(
    endpoint_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Delete a webhook endpoint."""
    ep = await _get_owned_endpoint(endpoint_id, user_id, db)
    await db.delete(ep)
    await db.commit()
    logger.info("webhook_endpoint_deleted", endpoint_id=str(endpoint_id), user_id=str(user_id))


@router.post("/endpoints/{endpoint_id}/rotate-secret")
async def rotate_secret(
    endpoint_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Rotate the signing secret for an endpoint.
    Returns the new secret (shown **once**).
    """
    ep = await _get_owned_endpoint(endpoint_id, user_id, db)
    new_secret = secrets.token_urlsafe(32)
    ep.secret = new_secret
    ep.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"secret": new_secret}


@router.post("/endpoints/{endpoint_id}/test")
async def test_endpoint(
    endpoint_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Send a test ping to the endpoint URL and return the delivery result.

    Delivers a `test.ping` event payload with a sample task payload.
    Does NOT create a WebhookLogDB record.
    """
    ep = await _get_owned_endpoint(endpoint_id, user_id, db)

    payload = {
        "event": "test.ping",
        "endpoint_id": str(ep.id),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {
            "message": "This is a test delivery from CrowdSorcerer.",
            "task_id": "00000000-0000-0000-0000-000000000000",
            "task_type": "web_research",
            "status": "completed",
        },
    }
    payload_bytes = json.dumps(payload).encode()
    sig = hmac.new(ep.secret.encode(), payload_bytes, hashlib.sha256).hexdigest()

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                ep.url,
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Crowdsourcerer-Event": "test.ping",
                    "X-Crowdsourcerer-Signature": sig,
                    "User-Agent": "CrowdSorcerer-Webhooks/1.0",
                },
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        success = 200 <= resp.status_code < 300
        return {
            "success": success,
            "status_code": resp.status_code,
            "duration_ms": duration_ms,
            "error": None if success else f"HTTP {resp.status_code}",
        }
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "success": False,
            "status_code": None,
            "duration_ms": duration_ms,
            "error": str(exc)[:200],
        }


# ─── Delivery logs ──────────────────────────────────────────────────────────

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


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _get_owned_endpoint(
    endpoint_id: UUID,
    user_id: str,
    db: AsyncSession,
) -> WebhookEndpointDB:
    result = await db.execute(
        select(WebhookEndpointDB).where(
            WebhookEndpointDB.id == endpoint_id,
            WebhookEndpointDB.user_id == user_id,
        )
    )
    ep = result.scalar_one_or_none()
    if not ep:
        raise HTTPException(404, "Webhook endpoint not found")
    return ep
