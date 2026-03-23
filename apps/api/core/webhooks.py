"""
core/webhooks.py — Centralised webhook delivery with typed events.

Each task can subscribe to a list of events via `webhook_events` (JSON array).
Supported events:
  task.created          — fired when the task record is first saved
  task.assigned         — fired when a worker claims a human task
  task.submission_received — fired when a worker submits work for a human task
  task.completed        — fired when the task is finished (AI result / approved submission)
  task.failed           — fired when the task fails permanently
  task.approved         — fired when requester explicitly approves a submission
  task.rejected         — fired when requester rejects a submission
  sla.breach            — fired when a task's SLA is breached

Payloads always include:
  - event       (string)
  - task_id     (UUID string)
  - occurred_at (ISO-8601 UTC)
  - ...event-specific fields...
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time as _time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import structlog
from sqlalchemy import select

from core.database import AsyncSessionLocal
from models.db import WebhookLogDB, TaskDB, WebhookEndpointDB, WebhookPayloadTemplateDB

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# All recognised event type strings
# ---------------------------------------------------------------------------
ALL_EVENTS = [
    "task.created",
    "task.assigned",
    "task.submission_received",
    "task.completed",
    "task.failed",
    "task.approved",
    "task.rejected",
    "sla.breach",
]

DEFAULT_EVENTS = ["task.completed"]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Custom payload template support
# ---------------------------------------------------------------------------

def _render_payload_template(template_str: str, context: dict[str, Any]) -> dict:
    """Replace {{key}} placeholders in template_str with values from context dict."""
    import json
    import re

    def replacer(m: Any) -> str:
        key = m.group(1).strip()
        value = context.get(key, "")
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value) if value is not None else ""

    rendered = re.sub(r"\{\{(\s*\w[\w.]*\s*)\}\}", replacer, template_str)
    try:
        return json.loads(rendered)
    except Exception:
        # Fall back to raw rendered string if not valid JSON
        return {"_raw": rendered}


async def _get_user_event_template(
    user_id: str,
    event_type: str,
) -> Optional[str]:
    """Return the user's custom payload template string for event_type, or None."""
    from sqlalchemy import select as sa_select
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                sa_select(WebhookPayloadTemplateDB).where(
                    WebhookPayloadTemplateDB.user_id == user_id,
                    WebhookPayloadTemplateDB.event_type == event_type,
                )
            )
            tpl = result.scalar_one_or_none()
            return tpl.template if tpl else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public helper — fire a webhook for a task if the task subscribes to that
# event.  Safe to call as an asyncio.create_task() fire-and-forget.
# ---------------------------------------------------------------------------

async def fire_webhook(
    *,
    task_id: str,
    user_id: str,
    webhook_url: str,
    webhook_events: list[str] | None,
    event_type: str,
    extra: dict[str, Any] | None = None,
    max_retries: int = 3,
) -> None:
    """
    Fire a webhook for *event_type* if the task subscribes to it.

    Parameters
    ----------
    task_id       — UUID string of the task
    user_id       — UUID string of the task owner (for logging)
    webhook_url   — destination URL
    webhook_events — list of subscribed events (None → default ["task.completed"])
    event_type    — one of ALL_EVENTS
    extra         — additional fields merged into the payload
    max_retries   — max delivery attempts (default 3, exponential back-off)
    """
    subscribed = webhook_events if webhook_events is not None else DEFAULT_EVENTS
    if event_type not in subscribed:
        return  # task not subscribed to this event

    payload: dict[str, Any] = {
        "event": event_type,
        "task_id": task_id,
        "occurred_at": _utcnow_iso(),
    }
    if extra:
        payload.update(extra)

    await _deliver(
        url=webhook_url,
        payload=payload,
        task_id=task_id,
        user_id=user_id,
        event_type=event_type,
        max_retries=max_retries,
    )


async def fire_webhook_for_task(
    *,
    task: TaskDB,
    event_type: str,
    extra: dict[str, Any] | None = None,
    max_retries: int = 3,
) -> None:
    """Convenience wrapper that reads URL + subscriptions directly from a TaskDB row."""
    if not task.webhook_url:
        return
    webhook_events: list[str] | None = task.webhook_events  # type: ignore[attr-defined]
    await fire_webhook(
        task_id=str(task.id),
        user_id=str(task.user_id),
        webhook_url=task.webhook_url,
        webhook_events=webhook_events,
        event_type=event_type,
        extra=extra,
        max_retries=max_retries,
    )


async def fire_persistent_endpoints(
    *,
    user_id: str,
    task_id: str,
    event_type: str,
    extra: dict[str, Any] | None = None,
    max_retries: int = 3,
) -> None:
    """
    Fire all active persistent webhook endpoints owned by *user_id* that subscribe
    to *event_type*.  Safe to call as fire-and-forget via asyncio.create_task().

    Each endpoint receives an HMAC-SHA256 signed payload via
    X-Crowdsourcerer-Signature header.  Delivery stats (delivery_count,
    failure_count, last_triggered_at, last_failure_at) are updated after each
    delivery attempt.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(WebhookEndpointDB).where(
                WebhookEndpointDB.user_id == user_id,
                WebhookEndpointDB.is_active.is_(True),
            )
        )
        endpoints: list[WebhookEndpointDB] = result.scalars().all()

    # Fire to each matching endpoint concurrently
    tasks = []
    for ep in endpoints:
        # None means "all events"; otherwise check the list
        subscribed: list[str] | None = ep.events  # type: ignore[assignment]
        if subscribed is not None and event_type not in subscribed:
            continue
        tasks.append(
            _deliver_to_endpoint(
                endpoint=ep,
                task_id=task_id,
                user_id=user_id,
                event_type=event_type,
                extra=extra,
                max_retries=max_retries,
            )
        )

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _deliver_to_endpoint(
    *,
    endpoint: WebhookEndpointDB,
    task_id: str,
    user_id: str,
    event_type: str,
    extra: dict[str, Any] | None,
    max_retries: int,
) -> None:
    """Deliver a signed event payload to a single persistent endpoint."""
    # Build default payload
    default_payload: dict[str, Any] = {
        "event": event_type,
        "task_id": task_id,
        "occurred_at": _utcnow_iso(),
        "endpoint_id": str(endpoint.id),
    }
    if extra:
        default_payload.update(extra)

    # Check if user has a custom template for this event
    custom_template = await _get_user_event_template(user_id, event_type)
    if custom_template:
        context = {
            "event_type": event_type,
            "task_id": task_id,
            "user_id": user_id,
            "occurred_at": _utcnow_iso(),
            "timestamp": _utcnow_iso(),
            **(extra or {}),
        }
        try:
            payload = _render_payload_template(custom_template, context)
        except Exception:
            payload = default_payload
    else:
        payload = default_payload

    payload_bytes = json.dumps(payload).encode()
    sig = hmac.new(endpoint.secret.encode(), payload_bytes, hashlib.sha256).hexdigest()

    endpoint_id = str(endpoint.id)

    for attempt in range(max_retries):
        t0 = _time.perf_counter()
        status_code: Optional[int] = None
        error_msg: Optional[str] = None
        success = False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    endpoint.url,
                    content=payload_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "X-Crowdsourcerer-Event": event_type,
                        "X-Crowdsourcerer-Signature": sig,
                        "User-Agent": "CrowdSorcerer-Webhooks/1.0",
                    },
                )
            status_code = resp.status_code
            if resp.status_code < 500:
                success = resp.status_code < 400
                if not success:
                    error_msg = f"Client error: HTTP {resp.status_code}"
                duration_ms = int((_time.perf_counter() - t0) * 1000)
                await _log_endpoint_delivery(
                    endpoint_id=endpoint_id,
                    task_id=task_id,
                    user_id=user_id,
                    url=endpoint.url,
                    event_type=event_type,
                    attempt=attempt + 1,
                    status_code=status_code,
                    success=success,
                    error=error_msg,
                    duration_ms=duration_ms,
                )
                await _update_endpoint_stats(endpoint_id=endpoint_id, success=success)
                return
            error_msg = f"Server error: HTTP {resp.status_code}"
            logger.warning("persistent_webhook_server_error", endpoint_id=endpoint_id,
                           url=endpoint.url, status=resp.status_code, attempt=attempt + 1)
        except Exception as exc:
            error_msg = str(exc)
            logger.warning("persistent_webhook_failed", endpoint_id=endpoint_id,
                           url=endpoint.url, error=error_msg, attempt=attempt + 1)

        duration_ms = int((_time.perf_counter() - t0) * 1000)
        await _log_endpoint_delivery(
            endpoint_id=endpoint_id,
            task_id=task_id,
            user_id=user_id,
            url=endpoint.url,
            event_type=event_type,
            attempt=attempt + 1,
            status_code=status_code,
            success=False,
            error=error_msg,
            duration_ms=duration_ms,
        )

        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)

    # All retries exhausted
    await _update_endpoint_stats(endpoint_id=endpoint_id, success=False)
    logger.error("persistent_webhook_exhausted_retries", endpoint_id=endpoint_id,
                 task_id=task_id, event=event_type)


async def _update_endpoint_stats(*, endpoint_id: str, success: bool) -> None:
    """Increment delivery/failure counters and update last_triggered_at."""
    try:
        from sqlalchemy import update as sa_update
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            if success:
                await db.execute(
                    sa_update(WebhookEndpointDB)
                    .where(WebhookEndpointDB.id == endpoint_id)
                    .values(
                        delivery_count=WebhookEndpointDB.delivery_count + 1,
                        last_triggered_at=now,
                    )
                )
            else:
                await db.execute(
                    sa_update(WebhookEndpointDB)
                    .where(WebhookEndpointDB.id == endpoint_id)
                    .values(
                        failure_count=WebhookEndpointDB.failure_count + 1,
                        last_failure_at=now,
                    )
                )
            await db.commit()
    except Exception:
        logger.warning("webhook_endpoint_stats_update_failed", endpoint_id=endpoint_id)


async def _log_endpoint_delivery(
    *,
    endpoint_id: str,
    task_id: str,
    user_id: str,
    url: str,
    event_type: str,
    attempt: int,
    status_code: Optional[int],
    success: bool,
    error: Optional[str],
    duration_ms: int,
) -> None:
    """Persist a WebhookLogDB row for a persistent endpoint delivery."""
    try:
        async with AsyncSessionLocal() as db:
            log = WebhookLogDB(
                task_id=task_id,
                user_id=user_id,
                url=url,
                event_type=event_type,
                attempt=attempt,
                status_code=status_code,
                success=success,
                error=error,
                duration_ms=duration_ms,
            )
            db.add(log)
            await db.commit()
    except Exception:
        logger.warning("persistent_webhook_log_failed", endpoint_id=endpoint_id, task_id=task_id)


# ---------------------------------------------------------------------------
# Internal delivery loop
# ---------------------------------------------------------------------------

async def _deliver(
    *,
    url: str,
    payload: dict[str, Any],
    task_id: str,
    user_id: str,
    event_type: str,
    max_retries: int,
) -> None:
    for attempt in range(max_retries):
        t0 = _time.perf_counter()
        status_code: Optional[int] = None
        error_msg: Optional[str] = None
        success = False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                status_code = resp.status_code
                if resp.status_code < 500:
                    success = resp.status_code < 400
                    if not success:
                        error_msg = f"Client error: HTTP {resp.status_code}"
                        logger.warning("webhook_client_error", url=url, status=resp.status_code,
                                       event=event_type)
                    duration_ms = int((_time.perf_counter() - t0) * 1000)
                    await _log(task_id, user_id, url, event_type, attempt + 1,
                               status_code, success, error_msg, duration_ms)
                    return  # don't retry 4xx
                error_msg = f"Server error: HTTP {resp.status_code}"
                logger.warning("webhook_server_error", url=url, status=resp.status_code,
                               attempt=attempt + 1, event=event_type)
        except Exception as exc:
            error_msg = str(exc)
            logger.warning("webhook_failed", url=url, error=error_msg, attempt=attempt + 1,
                           event=event_type)

        duration_ms = int((_time.perf_counter() - t0) * 1000)
        await _log(task_id, user_id, url, event_type, attempt + 1,
                   status_code, False, error_msg, duration_ms)

        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)  # 1s, 2s backoff

    logger.error("webhook_exhausted_retries", url=url, task_id=task_id, event=event_type)


async def retry_webhook_log(*, log_id: str, user_id: str) -> dict:
    """
    Manually retry a previously failed (or any) webhook delivery.

    Looks up the original log record, re-delivers the stored payload, and
    creates a new log entry marked as a manual retry.

    Returns a dict with the new log's result fields.
    Raises ValueError if the log is not found or does not belong to user_id.
    """
    from sqlalchemy import select as sa_select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            sa_select(WebhookLogDB).where(
                WebhookLogDB.id == log_id,
                WebhookLogDB.user_id == user_id,
            )
        )
        original: Optional[WebhookLogDB] = result.scalar_one_or_none()
        if original is None:
            raise ValueError("Webhook log not found or access denied")

    # Re-fire with the same URL and event_type
    event_type = original.event_type or "task.completed"
    payload: dict = {
        "event": event_type,
        "task_id": str(original.task_id),
        "occurred_at": _utcnow_iso(),
        "is_manual_retry": True,
        "original_log_id": str(original.id),
    }

    t0 = _time.perf_counter()
    status_code: Optional[int] = None
    error_msg: Optional[str] = None
    success = False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(original.url, json=payload)
            status_code = resp.status_code
            success = resp.status_code < 400
            if not success:
                error_msg = f"HTTP {resp.status_code}"
    except Exception as exc:
        error_msg = str(exc)

    duration_ms = int((_time.perf_counter() - t0) * 1000)

    # Persist new log row
    try:
        async with AsyncSessionLocal() as db:
            new_log = WebhookLogDB(
                task_id=original.task_id,
                user_id=original.user_id,
                url=original.url,
                event_type=event_type,
                attempt=1,
                status_code=status_code,
                success=success,
                error=error_msg,
                duration_ms=duration_ms,
                retry_of=original.id,
                is_manual_retry=True,
            )
            db.add(new_log)
            await db.commit()
            await db.refresh(new_log)
            new_id = str(new_log.id)
    except Exception:
        logger.warning("webhook_retry_log_failed", original_id=log_id)
        new_id = None

    return {
        "new_log_id": new_id,
        "success": success,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "error": error_msg,
    }


async def _log(
    task_id: str,
    user_id: str,
    url: str,
    event_type: str,
    attempt: int,
    status_code: Optional[int],
    success: bool,
    error: Optional[str],
    duration_ms: int,
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            log = WebhookLogDB(
                task_id=task_id,
                user_id=user_id,
                url=url,
                event_type=event_type,
                attempt=attempt,
                status_code=status_code,
                success=success,
                error=error,
                duration_ms=duration_ms,
            )
            db.add(log)
            await db.commit()
    except Exception:
        logger.warning("webhook_log_failed", task_id=task_id)
