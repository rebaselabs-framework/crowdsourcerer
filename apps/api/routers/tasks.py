"""Task CRUD + execution endpoints."""
from __future__ import annotations
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.database import get_db
from models.db import TaskDB, UserDB, CreditTransactionDB, TaskAssignmentDB, WebhookLogDB
from models.schemas import (
    TaskCreateRequest, TaskCreateResponse, TaskOut, PaginatedTasks,
    HUMAN_TASK_TYPES,
    SubmissionOut, SubmissionWorkerOut, SubmissionReviewRequest, SubmissionReviewResponse,
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


@router.get("/public")
async def public_task_feed(
    type: Optional[str] = Query(None, description="Filter by task type"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Public task feed — returns open human tasks (no auth required).

    Only exposes safe, non-sensitive fields: id, type, title (from instructions),
    worker_reward_credits, assignments_required, assignments_completed, created_at.
    """
    q = select(TaskDB).where(
        TaskDB.status == "open",
        TaskDB.execution_mode == "human",
    )
    if type:
        q = q.where(TaskDB.type == type)

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar() or 0

    q = q.order_by(TaskDB.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    tasks = result.scalars().all()

    # Return sanitised subset of fields only
    items = [
        {
            "id": str(t.id),
            "type": t.type,
            "title": (t.task_instructions[:60] + "…") if t.task_instructions and len(t.task_instructions) > 60 else (t.task_instructions or t.type.replace("_", " ").title()),
            "worker_reward_credits": t.worker_reward_credits,
            "assignments_required": t.assignments_required,
            "assignments_completed": t.assignments_completed,
            "priority": t.priority,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tasks
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("", response_model=PaginatedTasks)
async def list_tasks(
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    execution_mode: Optional[str] = Query(None),
    has_submissions: Optional[bool] = Query(None, description="Filter for human tasks that have worker submissions pending review"),
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
    if execution_mode:
        q = q.where(TaskDB.execution_mode == execution_mode)
    if has_submissions is True:
        # Only tasks that have at least one submitted/approved assignment
        q = q.where(
            TaskDB.id.in_(
                select(TaskAssignmentDB.task_id).where(
                    TaskAssignmentDB.status.in_(["submitted", "approved", "rejected"])
                ).distinct()
            )
        )

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


# ─── Submission review (requester approves/rejects worker submissions) ─────

@router.get("/{task_id}/submissions", response_model=list[SubmissionOut])
async def list_submissions(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List all worker submissions for a human task owned by the requester."""
    task_result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.execution_mode != "human":
        raise HTTPException(status_code=400, detail="This task has no human submissions")

    # Load assignments with worker info
    result = await db.execute(
        select(TaskAssignmentDB).where(TaskAssignmentDB.task_id == task_id)
        .order_by(TaskAssignmentDB.submitted_at.desc().nullslast())
    )
    assignments = result.scalars().all()

    out: list[SubmissionOut] = []
    for a in assignments:
        worker_result = await db.execute(select(UserDB).where(UserDB.id == a.worker_id))
        worker = worker_result.scalar_one_or_none()
        if not worker:
            continue
        out.append(SubmissionOut(
            id=a.id,
            task_id=a.task_id,
            worker=SubmissionWorkerOut(
                id=worker.id,
                name=worker.name,
                worker_level=worker.worker_level,
                worker_accuracy=worker.worker_accuracy,
                worker_tasks_completed=worker.worker_tasks_completed,
            ),
            status=a.status,
            response=a.response,
            worker_note=a.worker_note,
            earnings_credits=a.earnings_credits,
            xp_earned=a.xp_earned,
            claimed_at=a.claimed_at,
            submitted_at=a.submitted_at,
        ))
    return out


@router.post("/{task_id}/submissions/{assignment_id}/approve",
             response_model=SubmissionReviewResponse)
async def approve_submission(
    task_id: UUID,
    assignment_id: UUID,
    req: SubmissionReviewRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Approve a worker submission. Marks it approved and confirms worker earnings."""
    # Verify requester owns the task
    task_result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Load assignment
    a_result = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.id == assignment_id,
            TaskAssignmentDB.task_id == task_id,
        )
    )
    assignment = a_result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Submission not found")
    if assignment.status not in ("submitted", "approved"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot approve submission with status '{assignment.status}'",
        )
    if assignment.status == "approved":
        return SubmissionReviewResponse(
            assignment_id=assignment.id,
            status="approved",
            message="Already approved.",
        )

    assignment.status = "approved"

    # In-app notification to worker
    try:
        from core.notify import create_notification, NotifType
        await create_notification(
            db, assignment.worker_id,
            NotifType.SUBMISSION_APPROVED,
            "Submission approved! 🎉",
            f"Your {task.type.replace('_', ' ')} submission was approved. "
            f"You earned {assignment.earnings_credits} credits and {assignment.xp_earned} XP.",
            link="/worker/earnings",
        )
    except Exception:
        pass

    await db.commit()

    logger.info(
        "submission_approved",
        task_id=str(task_id),
        assignment_id=str(assignment_id),
        requester_id=user_id,
    )

    # Email notification to worker
    try:
        from core.email import notify_worker_approved
        worker_result = await db.execute(select(UserDB).where(UserDB.id == assignment.worker_id))
        worker_user = worker_result.scalar_one_or_none()
        if worker_user and worker_user.email:
            asyncio.create_task(notify_worker_approved(
                worker_user.email,
                task_type=task.type,
                earnings=assignment.earnings_credits,
                xp=assignment.xp_earned,
            ))
    except Exception:
        pass

    return SubmissionReviewResponse(
        assignment_id=assignment.id,
        status="approved",
        message="Submission approved. Worker earnings confirmed.",
    )


@router.post("/{task_id}/submissions/{assignment_id}/reject",
             response_model=SubmissionReviewResponse)
async def reject_submission(
    task_id: UUID,
    assignment_id: UUID,
    req: SubmissionReviewRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Reject a worker submission. Refunds the worker's reward credits to the requester."""
    # Verify requester owns the task
    task_result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Load assignment
    a_result = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.id == assignment_id,
            TaskAssignmentDB.task_id == task_id,
        )
    )
    assignment = a_result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Submission not found")
    if assignment.status not in ("submitted",):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot reject submission with status '{assignment.status}'",
        )

    assignment.status = "rejected"

    # Refund worker reward to requester
    refund_amount = assignment.earnings_credits
    requester_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    requester = requester_result.scalar_one_or_none()
    if requester and refund_amount > 0:
        requester.credits += refund_amount
        txn = CreditTransactionDB(
            user_id=user_id,
            task_id=task_id,
            amount=refund_amount,
            type="refund",
            description=f"Rejected submission for task {task.type}",
        )
        db.add(txn)

    # Reopen the task if it was completed but now has a rejected slot
    if task.status == "completed" and task.assignments_completed > 0:
        task.assignments_completed = max(0, task.assignments_completed - 1)
        task.status = "open"
        task.completed_at = None

    # In-app notification to worker
    try:
        from core.notify import create_notification, NotifType
        await create_notification(
            db, assignment.worker_id,
            NotifType.SUBMISSION_REJECTED,
            "Submission rejected",
            f"Your {task.type.replace('_', ' ')} submission was not accepted. "
            "Check the task guidelines and try again.",
            link="/worker/marketplace",
        )
    except Exception:
        pass

    await db.commit()

    logger.info(
        "submission_rejected",
        task_id=str(task_id),
        assignment_id=str(assignment_id),
        requester_id=user_id,
        refund=refund_amount,
    )
    return SubmissionReviewResponse(
        assignment_id=assignment.id,
        status="rejected",
        message=f"Submission rejected. {refund_amount} credits refunded to your account.",
    )


# ─── SSE — real-time task status stream ────────────────────────────────────

@router.get("/{task_id}/status-stream")
async def task_status_stream(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Server-Sent Events stream for real-time task status updates.

    Emits an event whenever the task status changes, then closes once terminal.
    Clients should reconnect if the connection drops.
    """
    # Verify the task belongs to the user
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_generator() -> AsyncIterator[str]:
        TERMINAL = {"completed", "failed", "cancelled"}
        last_status: str | None = None
        last_assignments_completed: int | None = None
        poll_interval = 1.5  # seconds
        max_polls = 300       # 7.5 minutes max before closing stream

        from core.database import AsyncSessionLocal

        for _ in range(max_polls):
            async with AsyncSessionLocal() as session:
                r = await session.execute(select(TaskDB).where(TaskDB.id == task_id))
                t = r.scalar_one_or_none()
                if t is None:
                    break

                status_changed = t.status != last_status
                assignments_changed = t.assignments_completed != last_assignments_completed

                if status_changed or assignments_changed:
                    last_status = t.status
                    last_assignments_completed = t.assignments_completed
                    payload = {
                        "task_id": str(task_id),
                        "status": t.status,
                        "assignments_completed": t.assignments_completed,
                        "assignments_required": t.assignments_required,
                        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                        "error": t.error,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"

                    if t.status in TERMINAL:
                        break

            await asyncio.sleep(poll_interval)

        # Send a final heartbeat/close event
        yield "data: {\"event\": \"stream_end\"}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Background task runner ────────────────────────────────────────────────

async def _run_task(task_id: str, user_id: str):
    """Execute an AI task against RebaseKit and store the result."""
    from core.database import AsyncSessionLocal  # avoid circular import at module level
    from core.email import notify_task_completed, notify_task_failed
    from core.notify import create_notification, NotifType

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

            # In-app notification: task completed
            await create_notification(
                db, user_id,
                NotifType.TASK_COMPLETED,
                "Task completed ✅",
                f"Your {task.type.replace('_', ' ')} task finished successfully in {duration_ms}ms.",
                link=f"/dashboard/tasks/{task_id}",
            )
            await db.commit()

            logger.info(
                "task_completed",
                task_id=task_id,
                type=task.type,
                duration_ms=duration_ms,
            )

            # Fetch user email for notification
            user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
            owner = user_result.scalar_one_or_none()

            # Fire webhook if set
            if task.webhook_url:
                asyncio.create_task(_send_webhook(task.webhook_url, task_id, user_id))

            # Email notification
            if owner and owner.email:
                asyncio.create_task(notify_task_completed(
                    owner.email, task_id, task.type
                ))

        except WorkerError as e:
            logger.error("task_failed", task_id=task_id, error=str(e))
            try:
                result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    task.status = "failed"
                    task.error = str(e)
                    task.completed_at = datetime.now(timezone.utc)

                    # In-app notification: task failed
                    await create_notification(
                        db, user_id,
                        NotifType.TASK_FAILED,
                        "Task failed ❌",
                        f"Your {task.type.replace('_', ' ')} task failed: {str(e)[:120]}",
                        link=f"/dashboard/tasks/{task_id}",
                    )
                    await db.commit()

                    # Email notification
                    user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
                    owner = user_result.scalar_one_or_none()
                    if owner and owner.email:
                        asyncio.create_task(notify_task_failed(
                            owner.email, task_id, task.type, str(e)
                        ))
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

                    # In-app notification
                    await create_notification(
                        db, user_id,
                        NotifType.TASK_FAILED,
                        "Task failed ❌",
                        f"Your {task.type.replace('_', ' ')} task encountered an unexpected error.",
                        link=f"/dashboard/tasks/{task_id}",
                    )
                    await db.commit()

                    # Email notification
                    user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
                    owner = user_result.scalar_one_or_none()
                    if owner and owner.email:
                        asyncio.create_task(notify_task_failed(
                            owner.email, task_id, task.type, f"Unexpected error: {type(e).__name__}"
                        ))
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
