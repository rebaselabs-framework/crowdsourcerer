"""Persistent webhook retry queue — background worker.

Polls the ``webhook_delivery_queue`` table every POLL_INTERVAL seconds,
picks up due items with ``FOR UPDATE SKIP LOCKED`` to prevent duplicate
processing across replicas, and attempts redelivery.

Backoff schedule (seconds after each failed attempt):
  Attempt 1 (first retry):   30s
  Attempt 2:                  2 min
  Attempt 3:                  10 min
  Attempt 4:                  1 hour
  Attempt 5:                  4 hours (then → dead_letter)

This replaces the previous in-memory retry loop that was lost on server restart.
"""

import asyncio
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import structlog
from sqlalchemy import select, and_, update as sa_update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.database import AsyncSessionLocal
from models.db import WebhookDeliveryQueueDB, WebhookEndpointDB, WebhookLogDB

logger = structlog.get_logger()

# ── Configuration ─────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = 30  # How often to check for due retries
BATCH_SIZE = 50             # Max items to process per poll cycle

# Exponential backoff schedule (seconds from failed attempt to next retry).
# Index 0 = delay after attempt 1 fails, etc.
BACKOFF_SCHEDULE = [30, 120, 600, 3600, 14400]  # 30s, 2m, 10m, 1h, 4h

DEFAULT_MAX_ATTEMPTS = 5


def _backoff_seconds(attempt: int) -> int:
    """Return delay in seconds before the next retry after *attempt* fails.

    attempt is 1-indexed (1 = first retry attempt).
    """
    idx = min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)
    return BACKOFF_SCHEDULE[idx]


# ── Enqueue helper (called from core/webhooks.py on first failure) ────────

async def enqueue_retry(
    *,
    endpoint_id: Optional[str],
    user_id: str,
    task_id: Optional[str],
    event_type: str,
    url: str,
    payload: dict,
    headers: Optional[dict] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> None:
    """Insert a failed delivery into the retry queue for background processing.

    Called after the first inline delivery attempt fails.  The first retry
    will be attempted after BACKOFF_SCHEDULE[0] seconds.
    """
    now = datetime.now(timezone.utc)
    next_retry = now + timedelta(seconds=_backoff_seconds(1))

    try:
        async with AsyncSessionLocal() as db:
            item = WebhookDeliveryQueueDB(
                endpoint_id=endpoint_id,
                user_id=user_id,
                task_id=task_id,
                event_type=event_type,
                url=url,
                payload=payload,
                headers=headers,
                attempt=1,
                max_attempts=max_attempts,
                next_retry_at=next_retry,
                status="pending",
            )
            db.add(item)
            await db.commit()
            logger.info(
                "webhook_retry.enqueued",
                queue_id=str(item.id),
                url=url,
                event_type=event_type,
                next_retry_at=next_retry.isoformat(),
            )
    except SQLAlchemyError:
        logger.error(
            "webhook_retry.enqueue_failed",
            url=url,
            event_type=event_type,
            exc_info=True,
        )


# ── Shared HTTP client ───────────────────────────────────────────────────

_retry_client: httpx.AsyncClient | None = None


def _get_retry_client() -> httpx.AsyncClient:
    global _retry_client
    if _retry_client is None or _retry_client.is_closed:
        _retry_client = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "CrowdSorcerer-Webhooks/1.0 (retry)"},
        )
    return _retry_client


# ── Process a single queue item ──────────────────────────────────────────

async def _process_item(item: WebhookDeliveryQueueDB) -> None:
    """Attempt to deliver a single queued webhook."""
    t0 = _time.perf_counter()
    status_code: Optional[int] = None
    error_msg: Optional[str] = None
    success = False

    try:
        client = _get_retry_client()
        # Build request headers
        req_headers = {"Content-Type": "application/json"}
        if item.headers and isinstance(item.headers, dict):
            req_headers.update(item.headers)

        import json
        payload_bytes = json.dumps(item.payload).encode()
        resp = await client.post(
            item.url,
            content=payload_bytes,
            headers=req_headers,
        )
        status_code = resp.status_code
        if resp.status_code < 400:
            success = True
        elif resp.status_code < 500:
            # 4xx = client error, don't retry further
            error_msg = f"Client error: HTTP {resp.status_code}"
        else:
            error_msg = f"Server error: HTTP {resp.status_code}"
    except (httpx.HTTPError, OSError) as exc:
        error_msg = str(exc)

    duration_ms = int((_time.perf_counter() - t0) * 1000)
    now = datetime.now(timezone.utc)

    # Log the delivery attempt
    try:
        async with AsyncSessionLocal() as db:
            log = WebhookLogDB(
                task_id=item.task_id,
                user_id=str(item.user_id),
                url=item.url,
                event_type=item.event_type,
                attempt=item.attempt + 1,  # +1 because attempt 1 was inline
                status_code=status_code,
                success=success,
                error=error_msg,
                duration_ms=duration_ms,
                payload=item.payload,
            )
            db.add(log)
            await db.commit()
    except SQLAlchemyError:
        logger.warning("webhook_retry.log_failed", queue_id=str(item.id))

    # Update queue item status
    try:
        async with AsyncSessionLocal() as db:
            if success:
                await db.execute(
                    sa_update(WebhookDeliveryQueueDB)
                    .where(WebhookDeliveryQueueDB.id == item.id)
                    .values(
                        status="completed",
                        last_status_code=status_code,
                        last_error=None,
                        completed_at=now,
                        updated_at=now,
                    )
                )
                logger.info(
                    "webhook_retry.delivered",
                    queue_id=str(item.id),
                    url=item.url,
                    attempt=item.attempt,
                    duration_ms=duration_ms,
                )
                # Update endpoint stats on success
                if item.endpoint_id:
                    await db.execute(
                        sa_update(WebhookEndpointDB)
                        .where(WebhookEndpointDB.id == item.endpoint_id)
                        .values(
                            delivery_count=WebhookEndpointDB.delivery_count + 1,
                            last_triggered_at=now,
                        )
                    )
            elif status_code and 400 <= status_code < 500:
                # 4xx errors — don't retry, go straight to dead letter
                await db.execute(
                    sa_update(WebhookDeliveryQueueDB)
                    .where(WebhookDeliveryQueueDB.id == item.id)
                    .values(
                        status="dead_letter",
                        last_status_code=status_code,
                        last_error=error_msg,
                        updated_at=now,
                    )
                )
                logger.warning(
                    "webhook_retry.client_error_dead_letter",
                    queue_id=str(item.id),
                    url=item.url,
                    status_code=status_code,
                )
                if item.endpoint_id:
                    await db.execute(
                        sa_update(WebhookEndpointDB)
                        .where(WebhookEndpointDB.id == item.endpoint_id)
                        .values(
                            failure_count=WebhookEndpointDB.failure_count + 1,
                            last_failure_at=now,
                        )
                    )
            elif item.attempt >= item.max_attempts:
                # Exhausted all retries → dead letter
                await db.execute(
                    sa_update(WebhookDeliveryQueueDB)
                    .where(WebhookDeliveryQueueDB.id == item.id)
                    .values(
                        status="dead_letter",
                        last_status_code=status_code,
                        last_error=error_msg,
                        updated_at=now,
                    )
                )
                logger.error(
                    "webhook_retry.exhausted",
                    queue_id=str(item.id),
                    url=item.url,
                    attempts=item.attempt,
                    event_type=item.event_type,
                )
                if item.endpoint_id:
                    await db.execute(
                        sa_update(WebhookEndpointDB)
                        .where(WebhookEndpointDB.id == item.endpoint_id)
                        .values(
                            failure_count=WebhookEndpointDB.failure_count + 1,
                            last_failure_at=now,
                        )
                    )
            else:
                # Schedule next retry
                next_attempt = item.attempt + 1
                delay = _backoff_seconds(next_attempt)
                next_retry = now + timedelta(seconds=delay)
                await db.execute(
                    sa_update(WebhookDeliveryQueueDB)
                    .where(WebhookDeliveryQueueDB.id == item.id)
                    .values(
                        status="pending",
                        attempt=next_attempt,
                        next_retry_at=next_retry,
                        last_status_code=status_code,
                        last_error=error_msg,
                        updated_at=now,
                    )
                )
                logger.info(
                    "webhook_retry.scheduled_next",
                    queue_id=str(item.id),
                    url=item.url,
                    attempt=next_attempt,
                    next_retry_at=next_retry.isoformat(),
                    backoff_seconds=delay,
                )

            await db.commit()
    except SQLAlchemyError:
        logger.error(
            "webhook_retry.status_update_failed",
            queue_id=str(item.id),
            exc_info=True,
        )


# ── Poll loop ─────────────────────────────────────────────────────────────

async def _poll_once() -> int:
    """Process one batch of due retry items.  Returns count processed."""
    now = datetime.now(timezone.utc)
    processed = 0

    try:
        async with AsyncSessionLocal() as db:
            # Select due items with row-level locking (prevents double processing)
            result = await db.execute(
                select(WebhookDeliveryQueueDB)
                .where(
                    and_(
                        WebhookDeliveryQueueDB.status == "pending",
                        WebhookDeliveryQueueDB.next_retry_at <= now,
                    )
                )
                .order_by(WebhookDeliveryQueueDB.next_retry_at.asc())
                .limit(BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
            items = list(result.scalars().all())

            if not items:
                return 0

            # Mark all as processing atomically
            item_ids = [item.id for item in items]
            await db.execute(
                sa_update(WebhookDeliveryQueueDB)
                .where(WebhookDeliveryQueueDB.id.in_(item_ids))
                .values(status="processing", updated_at=now)
            )
            await db.commit()

    except SQLAlchemyError:
        logger.error("webhook_retry.poll_failed", exc_info=True)
        return 0

    # Process each item (could parallelize, but sequential is safer for now)
    for item in items:
        try:
            await _process_item(item)
            processed += 1
        except Exception:
            logger.error(
                "webhook_retry.item_failed",
                queue_id=str(item.id),
                exc_info=True,
            )
            # Reset to pending so it gets picked up next cycle
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        sa_update(WebhookDeliveryQueueDB)
                        .where(WebhookDeliveryQueueDB.id == item.id)
                        .values(status="pending", updated_at=now)
                    )
                    await db.commit()
            except SQLAlchemyError:
                # Row will be retried on the next poll — nothing else we can do.
                pass

    return processed


async def run_retry_worker(interval: int = POLL_INTERVAL_SECONDS) -> None:
    """Run the webhook retry worker indefinitely.

    Polls every *interval* seconds for due retry items and processes them.
    """
    logger.info("webhook_retry.worker_started", poll_interval=interval)
    while True:
        try:
            count = await _poll_once()
            if count > 0:
                logger.info("webhook_retry.batch_processed", count=count)
        except asyncio.CancelledError:
            logger.info("webhook_retry.worker_stopped")
            return
        except Exception:
            logger.error("webhook_retry.worker_error", exc_info=True)

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("webhook_retry.worker_stopped")
            return


# ── Start/stop helpers (mirrors sweeper pattern) ─────────────────────────

_worker_task: Optional[asyncio.Task] = None


def start_retry_worker(interval: int = POLL_INTERVAL_SECONDS) -> asyncio.Task:
    """Start the webhook retry worker as a background asyncio task."""
    from core.background import safe_create_task

    global _worker_task
    _worker_task = safe_create_task(
        run_retry_worker(interval),
        name="webhook-retry-worker",
    )
    return _worker_task


def stop_retry_worker() -> None:
    """Cancel the retry worker task.  Call at shutdown."""
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()


def get_retry_worker_task() -> Optional[asyncio.Task]:
    """Return the current worker task (for admin/health inspection)."""
    return _worker_task


# ── Queue stats helper ───────────────────────────────────────────────────

async def get_queue_stats() -> dict:
    """Return summary statistics for the webhook retry queue."""
    from sqlalchemy import func

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(
                    WebhookDeliveryQueueDB.status,
                    func.count(WebhookDeliveryQueueDB.id).label("count"),
                )
                .group_by(WebhookDeliveryQueueDB.status)
            )
            rows = result.all()

        stats: dict = {
            "pending": 0,
            "processing": 0,
            "completed": 0,
            "dead_letter": 0,
            "total": 0,
        }
        for status, count in rows:
            stats[status] = count
            stats["total"] += count

        return stats
    except SQLAlchemyError:
        logger.error("webhook_retry.stats_failed", exc_info=True)
        return {"error": "Failed to fetch queue stats"}
