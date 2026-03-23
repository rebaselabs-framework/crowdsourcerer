"""Task CRUD + execution endpoints."""
from __future__ import annotations
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.database import get_db
from models.db import TaskDB, UserDB, CreditTransactionDB, WebhookLogDB
from models.schemas import (
    TaskCreateRequest, TaskCreateResponse, TaskOut, PaginatedTasks,
    HUMAN_TASK_TYPES,
)
from workers.base import get_rebasekit_client, WorkerError
from workers.router import execute_task, TASK_CREDITS

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

# Default credits cost for human tasks (requester pays this to fund worker rewards)
HUMAN_TASK_BASE_CREDITS: dict[str, int] = {
    "label_image": 3,
    "label_text": 2,
    "rate_quality": 2,
    "verify_fact": 3,
    "moderate_content": 2,
    "compare_rank": 2,
    "answer_question": 4,
    "transcription_review": 5,
}


@router.post("", response_model=TaskCreateResponse, status_code=201)
async def create_task(
    req: TaskCreateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    is_human = req.type in HUMAN_TASK_TYPES

    if is_human:
        # For human tasks: requester pays a platform fee.
        # The worker reward is set by requester (or defaults to base cost).
        worker_reward = req.worker_reward_credits or HUMAN_TASK_BASE_CREDITS.get(req.type, 2)
        # Total cost = worker_reward * assignments_required + 20% platform fee
        platform_fee = max(1, int(worker_reward * req.assignments_required * 0.2))
        estimated_credits = worker_reward * req.assignments_required + platform_fee
    else:
        estimated_credits = TASK_CREDITS.get(req.type, 5)

    # Check credits
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.credits < estimated_credits:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_credits",
                "required": estimated_credits,
                "available": user.credits,
            },
        )

    # Deduct credits (reserve)
    user.credits -= estimated_credits

    if is_human:
        # Human tasks go to the worker marketplace immediately
        task = TaskDB(
            user_id=user_id,
            type=req.type,
            status="open",
            execution_mode="human",
            priority=req.priority,
            input=req.input,
            metadata=req.metadata,
            webhook_url=req.webhook_url,
            worker_reward_credits=worker_reward,
            assignments_required=req.assignments_required,
            claim_timeout_minutes=req.claim_timeout_minutes,
            task_instructions=req.task_instructions,
        )
    else:
        # AI tasks go straight to the processing queue
        task = TaskDB(
            user_id=user_id,
            type=req.type,
            status="queued",
            execution_mode="ai",
            priority=req.priority,
            input=req.input,
            metadata=req.metadata,
            webhook_url=req.webhook_url,
        )

    db.add(task)

    # Log credit charge
    txn = CreditTransactionDB(
        user_id=user_id,
        task_id=task.id,
        amount=-estimated_credits,
        type="charge",
        description=f"Task: {req.type}",
    )
    db.add(txn)

    await db.commit()
    await db.refresh(task)

    if not is_human:
        # Run AI task in background
        background_tasks.add_task(_run_task, str(task.id), str(user_id))

    return TaskCreateResponse(
        task_id=task.id,
        status=task.status,
        estimated_credits=estimated_credits,
    )


@router.get("", response_model=PaginatedTasks)
async def list_tasks(
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    q = select(TaskDB).where(TaskDB.user_id == user_id)
    if status:
        q = q.where(TaskDB.status == status)
    if type:
        q = q.where(TaskDB.type == type)

    total_result = await db.execute(
        select(func.count()).select_from(q.subquery())
    )
    total = total_result.scalar() or 0

    q = q.order_by(TaskDB.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    tasks = result.scalars().all()

    return PaginatedTasks(
        items=tasks,
        total=total,
        page=page,
        page_size=page_size,
        has_next=(page * page_size) < total,
    )


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/{task_id}/cancel", status_code=204)
async def cancel_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in ("pending", "queued", "open"):
        raise HTTPException(status_code=409, detail="Task cannot be cancelled")
    task.status = "cancelled"
    await db.commit()


# ─── Background task runner ────────────────────────────────────────────────

async def _run_task(task_id: str, user_id: str):
    """Execute an AI task against RebaseKit and store the result."""
    from core.database import AsyncSessionLocal  # avoid circular import at module level

    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
            task = result.scalar_one_or_none()
            if not task:
                return

            task.status = "running"
            task.started_at = datetime.now(timezone.utc)
            await db.commit()

            t0 = time.perf_counter()
            client = get_rebasekit_client()
            output = await execute_task(task.type, task.input, client)
            duration_ms = int((time.perf_counter() - t0) * 1000)

            task.status = "completed"
            task.output = output
            task.duration_ms = duration_ms
            task.completed_at = datetime.now(timezone.utc)
            task.credits_used = TASK_CREDITS.get(task.type, 5)
            await db.commit()

            logger.info(
                "task_completed",
                task_id=task_id,
                type=task.type,
                duration_ms=duration_ms,
            )

            # Fire webhook if set
            if task.webhook_url:
                asyncio.create_task(_send_webhook(task.webhook_url, task_id, user_id))

        except WorkerError as e:
            logger.error("task_failed", task_id=task_id, error=str(e))
            try:
                result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    task.status = "failed"
                    task.error = str(e)
                    task.completed_at = datetime.now(timezone.utc)
                    await db.commit()
            except Exception:
                pass

        except Exception as e:
            logger.exception("task_unexpected_error", task_id=task_id)
            try:
                result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    task.status = "failed"
                    task.error = f"Unexpected error: {type(e).__name__}"
                    task.completed_at = datetime.now(timezone.utc)
                    await db.commit()
            except Exception:
                pass


async def _send_webhook(url: str, task_id: str, user_id: str, max_retries: int = 3):
    """Fire-and-forget webhook notification with exponential backoff + delivery logging."""
    import httpx
    import time as _time
    from core.database import AsyncSessionLocal  # avoid circular import

    payload = {"task_id": task_id, "event": "task.completed"}
    for attempt in range(max_retries):
        t0 = _time.perf_counter()
        status_code = None
        error_msg = None
        success = False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                status_code = resp.status_code
                if resp.status_code < 500:
                    success = resp.status_code < 400
                    if not success:
                        logger.warning(
                            "webhook_client_error",
                            url=url,
                            status=resp.status_code,
                        )
                        error_msg = f"Client error: HTTP {resp.status_code}"
                    # log and return — no retry on 4xx
                    await _log_webhook(task_id, user_id, url, attempt + 1,
                                       status_code, success, error_msg,
                                       int((_time.perf_counter() - t0) * 1000))
                    if success:
                        return
                    return  # don't retry 4xx
                logger.warning(
                    "webhook_server_error",
                    url=url,
                    status=resp.status_code,
                    attempt=attempt + 1,
                )
                error_msg = f"Server error: HTTP {resp.status_code}"
        except Exception as e:
            error_msg = str(e)
            logger.warning("webhook_failed", url=url, error=str(e), attempt=attempt + 1)

        duration_ms = int((_time.perf_counter() - t0) * 1000)
        await _log_webhook(task_id, user_id, url, attempt + 1,
                           status_code, False, error_msg, duration_ms)

        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s

    logger.error("webhook_exhausted_retries", url=url, task_id=task_id)


async def _log_webhook(
    task_id: str,
    user_id: str,
    url: str,
    attempt: int,
    status_code: int | None,
    success: bool,
    error: str | None,
    duration_ms: int,
) -> None:
    """Persist a webhook delivery log entry."""
    from core.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            log = WebhookLogDB(
                task_id=task_id,
                user_id=user_id,
                url=url,
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
