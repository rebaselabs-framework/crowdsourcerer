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
from core.scopes import require_scope, SCOPE_TASKS_READ, SCOPE_TASKS_WRITE
from core.database import get_db
from core.webhooks import fire_webhook_for_task, fire_persistent_endpoints, ALL_EVENTS, DEFAULT_EVENTS
from models.db import TaskDB, UserDB, CreditTransactionDB, TaskAssignmentDB, WebhookLogDB
from models.schemas import (
    TaskCreateRequest, TaskCreateResponse, TaskOut, PaginatedTasks,
    BatchTaskCreateRequest, BatchTaskCreateResponse,
    BulkActionRequest, BulkActionResult,
    HUMAN_TASK_TYPES,
    SubmissionOut, SubmissionWorkerOut, SubmissionReviewRequest, SubmissionReviewResponse,
    TaskAnalyticsOut, AssignmentAnalyticsRow,
    TaskTagsUpdate, TagStats,
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
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
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

    # Check credits — may come from personal account or org pool
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Enforce plan quota limits
    from core.quotas import enforce_task_creation_quota, record_task_creation
    await enforce_task_creation_quota(db, user_id, user.plan)

    # Org billing: if org_id specified, check membership and deduct from org pool
    org = None
    if req.org_id:
        from models.db import OrganizationDB, OrgMemberDB
        org_result = await db.execute(
            select(OrganizationDB).where(OrganizationDB.id == req.org_id)
        )
        org = org_result.scalar_one_or_none()
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        # Verify user is a member
        mem_result = await db.execute(
            select(OrgMemberDB).where(
                OrgMemberDB.org_id == req.org_id,
                OrgMemberDB.user_id == user_id,
            )
        )
        member = mem_result.scalar_one_or_none()
        if not member:
            raise HTTPException(status_code=403, detail="You are not a member of this organization")
        if org.credits < estimated_credits:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "insufficient_org_credits",
                    "required": estimated_credits,
                    "available": org.credits,
                },
            )
        org.credits -= estimated_credits
    else:
        if user.credits < estimated_credits:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "insufficient_credits",
                    "required": estimated_credits,
                    "available": user.credits,
                },
            )
        user.credits -= estimated_credits
        # Fire low-credit alert if threshold crossed
        from core.credit_alerts import maybe_fire_credit_alert
        await maybe_fire_credit_alert(db, user)

    # Validate tags
    validated_tags = None
    if req.tags:
        cleaned = [t.strip()[:50] for t in req.tags if t.strip()]
        validated_tags = list(dict.fromkeys(cleaned))[:20]  # dedup + cap

    # Determine if task should be scheduled (deferred)
    now_utc = datetime.now(timezone.utc)
    is_scheduled = req.scheduled_at is not None and req.scheduled_at > now_utc

    if is_human:
        # Human tasks go to the worker marketplace immediately (or deferred)
        # Determine effective consensus strategy
        consensus_strategy = req.consensus_strategy
        if req.assignments_required == 1 and consensus_strategy in ("majority_vote", "unanimous"):
            # Single-assignment tasks can't vote — fall back
            consensus_strategy = "any_first"

        task = TaskDB(
            user_id=user_id,
            type=req.type,
            status="pending" if is_scheduled else "open",
            execution_mode="human",
            priority=req.priority,
            input=req.input,
            metadata=req.metadata,
            webhook_url=req.webhook_url,
            webhook_events=req.webhook_events or DEFAULT_EVENTS,
            worker_reward_credits=worker_reward,
            assignments_required=req.assignments_required,
            claim_timeout_minutes=req.claim_timeout_minutes,
            task_instructions=req.task_instructions,
            consensus_strategy=consensus_strategy,
            org_id=req.org_id,
            min_skill_level=req.min_skill_level,
            tags=validated_tags,
            scheduled_at=req.scheduled_at,
        )
    else:
        # AI tasks go straight to the processing queue (or deferred)
        task = TaskDB(
            user_id=user_id,
            type=req.type,
            status="pending" if is_scheduled else "queued",
            execution_mode="ai",
            priority=req.priority,
            input=req.input,
            metadata=req.metadata,
            webhook_url=req.webhook_url,
            webhook_events=req.webhook_events or DEFAULT_EVENTS,
            tags=validated_tags,
            scheduled_at=req.scheduled_at,
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

    # Record quota usage
    await record_task_creation(db, user_id)

    # Hook requester onboarding: create_task step
    asyncio.create_task(_mark_requester_onboarding(user_id, "create_task"))

    if not is_human and not is_scheduled:
        # Run AI task in background (scheduled tasks wait for the sweeper)
        background_tasks.add_task(_run_task, str(task.id), str(user_id))

    # Fire task.created webhook (per-task + persistent endpoints)
    _wh_extra = {"type": task.type, "status": task.status, "priority": task.priority}
    if task.webhook_url:
        asyncio.create_task(fire_webhook_for_task(
            task=task,
            event_type="task.created",
            extra=_wh_extra,
        ))
    asyncio.create_task(fire_persistent_endpoints(
        user_id=str(task.user_id),
        task_id=str(task.id),
        event_type="task.created",
        extra=_wh_extra,
    ))

    # Notify workers whose saved-search alerts match this new task
    if is_human:
        asyncio.create_task(_trigger_saved_search_alerts(
            task_type=task.type,
            priority=task.priority,
            reward_credits=task.worker_reward_credits,
        ))

    return TaskCreateResponse(
        task_id=task.id,
        status=task.status,
        estimated_credits=estimated_credits,
    )


async def _trigger_saved_search_alerts(
    task_type: str,
    priority: str,
    reward_credits,
) -> None:
    """Background task: notify workers whose saved searches match a new human task."""
    from core.database import AsyncSessionLocal
    from routers.saved_searches import notify_matching_saved_searches
    async with AsyncSessionLocal() as db:
        try:
            await notify_matching_saved_searches(task_type, priority, reward_credits, db)
        except Exception:
            pass  # Never crash task creation


async def _mark_requester_onboarding(user_id: str, step: str) -> None:
    """Background task: advance requester onboarding step."""
    from core.database import AsyncSessionLocal
    from routers.requester_onboarding import complete_step_internal
    async with AsyncSessionLocal() as db:
        try:
            await complete_step_internal(user_id, step, db)
        except Exception:
            pass


def _calc_credits(req: TaskCreateRequest) -> int:
    """Calculate estimated credits for a task request."""
    if req.type in HUMAN_TASK_TYPES:
        worker_reward = req.worker_reward_credits or HUMAN_TASK_BASE_CREDITS.get(req.type, 2)
        platform_fee = max(1, int(worker_reward * req.assignments_required * 0.2))
        return worker_reward * req.assignments_required + platform_fee
    return TASK_CREDITS.get(req.type, 5)


@router.post("/batch", status_code=201)
async def create_tasks_batch(
    req: BatchTaskCreateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Create up to 50 tasks in a single API call.

    All credits are reserved atomically. Partial failures are reported in
    the `failed` array without rolling back successful tasks.
    """
    # Pre-flight: calculate total cost
    total_credits = sum(_calc_credits(t) for t in req.tasks)

    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Enforce plan quota limits (batch size + daily limit)
    from core.quotas import enforce_task_creation_quota, record_task_creation, enforce_batch_size
    enforce_batch_size(user.plan, len(req.tasks))
    await enforce_task_creation_quota(db, user_id, user.plan, task_count=len(req.tasks))

    if user.credits < total_credits:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_credits",
                "required": total_credits,
                "available": user.credits,
                "task_count": len(req.tasks),
            },
        )

    # Deduct all credits upfront
    user.credits -= total_credits

    created = []
    failed = []
    ai_task_ids: list[str] = []

    for i, task_req in enumerate(req.tasks):
        try:
            is_human = task_req.type in HUMAN_TASK_TYPES
            est = _calc_credits(task_req)

            if is_human:
                worker_reward = task_req.worker_reward_credits or HUMAN_TASK_BASE_CREDITS.get(task_req.type, 2)
                task = TaskDB(
                    user_id=user_id,
                    type=task_req.type,
                    status="open",
                    execution_mode="human",
                    priority=task_req.priority,
                    input=task_req.input,
                    metadata=task_req.metadata,
                    webhook_url=task_req.webhook_url,
                    webhook_events=task_req.webhook_events or DEFAULT_EVENTS,
                    worker_reward_credits=worker_reward,
                    assignments_required=task_req.assignments_required,
                    claim_timeout_minutes=task_req.claim_timeout_minutes,
                    task_instructions=task_req.task_instructions,
                    credits_used=est,
                    min_skill_level=task_req.min_skill_level,
                )
            else:
                task = TaskDB(
                    user_id=user_id,
                    type=task_req.type,
                    status="queued",
                    execution_mode="ai",
                    priority=task_req.priority,
                    input=task_req.input,
                    metadata=task_req.metadata,
                    webhook_url=task_req.webhook_url,
                    webhook_events=task_req.webhook_events or DEFAULT_EVENTS,
                    credits_used=est,
                )

            db.add(task)
            txn = CreditTransactionDB(
                user_id=user_id,
                task_id=task.id,
                amount=-est,
                type="charge",
                description=f"Batch task: {task_req.type}",
            )
            db.add(txn)
            created.append((task, est, is_human))
        except Exception as e:
            failed.append({"index": i, "type": task_req.type, "error": str(e)})

    await db.commit()

    # Record quota usage for successfully created tasks
    if created:
        await record_task_creation(db, user_id, task_count=len(created))

    # Refresh and schedule AI tasks
    results = []
    for task, est, is_human in created:
        await db.refresh(task)
        if not is_human:
            background_tasks.add_task(_run_task, str(task.id), str(user_id))
        results.append(TaskCreateResponse(
            task_id=task.id,
            status=task.status,
            estimated_credits=est,
        ))

    logger.info("batch_tasks_created", count=len(results), failed=len(failed), user_id=user_id)
    return BatchTaskCreateResponse(
        created=results,
        total_credits=total_credits,
        failed=failed,
    )


@router.get("/templates")
async def get_task_templates():
    """Return curated task templates with pre-filled inputs.

    Templates help users quickly create tasks without filling in every field.
    """
    return {
        "templates": [
            # ── Human task templates ──────────────────────────────────────
            {
                "id": "image_classification",
                "name": "Image Classification",
                "description": "Workers label images into your custom categories",
                "type": "label_image",
                "icon": "🖼️",
                "category": "human",
                "estimated_credits": 4,
                "default_input": {
                    "image_url": "",
                    "labels": ["Category A", "Category B", "Category C"],
                    "description": "Please classify this image.",
                },
                "default_settings": {
                    "assignments_required": 3,
                    "worker_reward_credits": 2,
                    "priority": "normal",
                    "task_instructions": "Select all labels that apply to this image.",
                },
            },
            {
                "id": "text_classification",
                "name": "Text Classification",
                "description": "Classify text into predefined categories",
                "type": "label_text",
                "icon": "📝",
                "category": "human",
                "estimated_credits": 3,
                "default_input": {
                    "text": "",
                    "categories": ["Positive", "Negative", "Neutral"],
                },
                "default_settings": {
                    "assignments_required": 3,
                    "worker_reward_credits": 1,
                    "priority": "normal",
                    "task_instructions": "Classify the sentiment of the text above.",
                },
            },
            {
                "id": "fact_verification",
                "name": "Fact Verification",
                "description": "Have workers verify whether a claim is true or false",
                "type": "verify_fact",
                "icon": "✅",
                "category": "human",
                "estimated_credits": 4,
                "default_input": {
                    "claim": "",
                    "context": "",
                },
                "default_settings": {
                    "assignments_required": 3,
                    "worker_reward_credits": 2,
                    "priority": "normal",
                    "task_instructions": "Research this claim and determine if it is true, false, or unverifiable. Provide a citation if possible.",
                },
            },
            {
                "id": "content_moderation",
                "name": "Content Moderation",
                "description": "Review content for policy compliance",
                "type": "moderate_content",
                "icon": "🛡️",
                "category": "human",
                "estimated_credits": 3,
                "default_input": {
                    "content": "",
                    "content_type": "text",
                    "policy_context": "Flag content that is harmful, offensive, or violates community guidelines.",
                },
                "default_settings": {
                    "assignments_required": 1,
                    "worker_reward_credits": 2,
                    "priority": "normal",
                },
            },
            {
                "id": "ab_comparison",
                "name": "A/B Content Comparison",
                "description": "Workers pick the better of two options",
                "type": "compare_rank",
                "icon": "⚖️",
                "category": "human",
                "estimated_credits": 3,
                "default_input": {
                    "option_a": "",
                    "option_b": "",
                    "comparison_criteria": "Which option is higher quality?",
                },
                "default_settings": {
                    "assignments_required": 3,
                    "worker_reward_credits": 1,
                    "priority": "normal",
                    "task_instructions": "Read both options carefully and select the one that better meets the comparison criteria.",
                },
            },
            {
                "id": "transcript_correction",
                "name": "Transcript Review",
                "description": "Workers correct AI-generated transcripts",
                "type": "transcription_review",
                "icon": "🎙️",
                "category": "human",
                "estimated_credits": 7,
                "default_input": {
                    "audio_url": "",
                    "ai_transcript": "",
                },
                "default_settings": {
                    "assignments_required": 1,
                    "worker_reward_credits": 5,
                    "priority": "normal",
                    "task_instructions": "Listen to the audio and correct any errors in the AI-generated transcript.",
                },
            },
            # ── AI task templates ─────────────────────────────────────────
            {
                "id": "web_research",
                "name": "Web Research",
                "description": "AI researches a topic and returns a summary",
                "type": "web_research",
                "icon": "🔍",
                "category": "ai",
                "estimated_credits": 10,
                "default_input": {
                    "query": "",
                    "max_sources": 5,
                },
            },
            {
                "id": "llm_generate",
                "name": "LLM Text Generation",
                "description": "Generate text using Claude or other LLMs",
                "type": "llm_generate",
                "icon": "✨",
                "category": "ai",
                "estimated_credits": 1,
                "default_input": {
                    "prompt": "",
                    "model": "claude-3-haiku-20240307",
                    "max_tokens": 1024,
                },
            },
            {
                "id": "entity_lookup",
                "name": "Entity Enrichment",
                "description": "Look up company or person information",
                "type": "entity_lookup",
                "icon": "🏢",
                "category": "ai",
                "estimated_credits": 5,
                "default_input": {
                    "name": "",
                    "type": "company",
                },
            },
            {
                "id": "pii_detection",
                "name": "PII Detection",
                "description": "Detect and mask personal information in text",
                "type": "pii_detect",
                "icon": "🔒",
                "category": "ai",
                "estimated_credits": 2,
                "default_input": {
                    "text": "",
                    "mask": True,
                },
            },
        ]
    }


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


@router.get("/tags", response_model=list[TagStats])
async def list_task_tags(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """Return all unique tags used by this user's tasks with usage counts."""
    from sqlalchemy import cast, String, func as sqlfunc
    from sqlalchemy.dialects.postgresql import JSONB
    # PostgreSQL: unnest JSON array of tags, group + count
    result = await db.execute(
        select(TaskDB.tags, TaskDB.id).where(
            TaskDB.user_id == user_id,
            TaskDB.tags.isnot(None),
        )
    )
    rows = result.all()
    tag_counts: dict[str, int] = {}
    for row in rows:
        tags_list = row[0] or []
        for tag in tags_list:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    return [TagStats(tag=t, count=c) for t, c in sorted(tag_counts.items(), key=lambda x: -x[1])]


@router.get("", response_model=PaginatedTasks)
async def list_tasks(
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    execution_mode: Optional[str] = Query(None),
    has_submissions: Optional[bool] = Query(None, description="Filter for human tasks that have worker submissions pending review"),
    tag: Optional[str] = Query(None, description="Filter tasks by tag label"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    q = select(TaskDB).where(TaskDB.user_id == user_id)
    if status:
        q = q.where(TaskDB.status == status)
    if type:
        q = q.where(TaskDB.type == type)
    if execution_mode:
        q = q.where(TaskDB.execution_mode == execution_mode)
    if tag:
        # JSON array contains this tag
        q = q.where(TaskDB.tags.contains([tag]))
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


@router.post("/bulk-action", response_model=BulkActionResult)
async def bulk_task_action(
    req: BulkActionRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Perform a bulk action (cancel or retry) on multiple tasks at once.

    - cancel: sets cancellable tasks (pending/queued/open) to 'cancelled'
    - retry: re-queues failed AI tasks back to 'queued' status
    """
    succeeded: list[str] = []
    failed: list[dict] = []

    for task_id in req.task_ids:
        result = await db.execute(
            select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
        )
        task = result.scalar_one_or_none()
        if not task:
            failed.append({"task_id": str(task_id), "reason": "not found or not owned"})
            continue

        if req.action == "cancel":
            if task.status not in ("pending", "queued", "open"):
                failed.append({"task_id": str(task_id), "reason": f"cannot cancel task with status '{task.status}'"})
                continue
            task.status = "cancelled"
            succeeded.append(str(task_id))

        elif req.action == "retry":
            if task.status != "failed":
                failed.append({"task_id": str(task_id), "reason": f"cannot retry task with status '{task.status}'"})
                continue
            if task.execution_mode != "ai":
                failed.append({"task_id": str(task_id), "reason": "retry is only supported for AI tasks"})
                continue
            task.status = "queued"
            task.error = None
            succeeded.append(str(task_id))

    await db.commit()

    # For retried tasks, kick off background execution
    if req.action == "retry" and succeeded:
        for task_id_str in succeeded:
            background_tasks.add_task(_run_task, task_id_str, user_id)

    logger.info(
        "bulk_task_action",
        action=req.action,
        succeeded=len(succeeded),
        failed=len(failed),
        user_id=user_id,
    )
    return BulkActionResult(succeeded=succeeded, failed=failed, action=req.action)


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/{task_id}/duplicate-params")
async def get_duplicate_params(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """
    Return the task's parameters in a form-friendly format for pre-filling
    a new-task form.  Does NOT create a new task — just returns params.
    """
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    params: dict = {
        "source_task_id": str(task.id),
        "type": task.type,
        "title": task.title if hasattr(task, "title") else None,
        "input": task.input,
        "priority": task.priority or "normal",
        "tags": task.tags or [],
        "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
    }

    # Human task params
    if task.execution_mode == "human":
        params["worker_reward_credits"] = task.worker_reward_credits
        params["assignments_required"] = task.assignments_required or 1
        params["claim_timeout_minutes"] = task.claim_timeout_minutes or 30
        params["task_instructions"] = task.task_instructions
        params["consensus_strategy"] = task.consensus_strategy or "any_first"
        params["min_skill_level"] = task.min_skill_level

    # Webhook params
    if task.webhook_url:
        params["webhook_url"] = task.webhook_url
        params["webhook_events"] = task.webhook_events

    # Remove None values for a clean response
    params = {k: v for k, v in params.items() if v is not None}

    return params


@router.put("/{task_id}/tags", response_model=TaskOut)
async def update_task_tags(
    task_id: UUID,
    req: TaskTagsUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Replace the labels/tags on a task (max 20 tags, max 50 chars each)."""
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    cleaned = [t.strip()[:50] for t in req.tags if t.strip()]
    task.tags = list(dict.fromkeys(cleaned))[:20]
    await db.commit()
    await db.refresh(task)
    return task


@router.post("/{task_id}/cancel", status_code=204)
async def cancel_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
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

    # Update worker skill profile
    try:
        from routers.skills import update_worker_skill
        resp_minutes: float | None = None
        if assignment.submitted_at and assignment.claimed_at:
            delta = assignment.submitted_at - assignment.claimed_at
            resp_minutes = delta.total_seconds() / 60
        await update_worker_skill(
            db,
            worker_id=assignment.worker_id,
            task_type=task.type,
            outcome="approved",
            response_minutes=resp_minutes,
            credits_earned=assignment.earnings_credits,
        )
    except Exception:
        pass

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

    # If task is now completed (requester_review strategy), resume any waiting pipeline
    if task.status == "completed" and task.output is not None:
        try:
            from routers.pipelines import resume_pipeline_after_human_step
            await resume_pipeline_after_human_step(task.id, task.output, db)
        except Exception:
            pass

    # Webhook: task.approved / task.completed (per-task + persistent endpoints)
    _wh_approved_extra = {"type": task.type, "assignment_id": str(assignment_id),
                          "worker_id": str(assignment.worker_id)}
    if task.webhook_url:
        asyncio.create_task(fire_webhook_for_task(
            task=task,
            event_type="task.approved",
            extra=_wh_approved_extra,
        ))
    asyncio.create_task(fire_persistent_endpoints(
        user_id=str(task.user_id),
        task_id=str(task.id),
        event_type="task.approved",
        extra=_wh_approved_extra,
    ))
    if task.status == "completed":
        _wh_complete_extra = {"type": task.type, "execution_mode": "human"}
        if task.webhook_url:
            asyncio.create_task(fire_webhook_for_task(
                task=task,
                event_type="task.completed",
                extra=_wh_complete_extra,
            ))
        asyncio.create_task(fire_persistent_endpoints(
            user_id=str(task.user_id),
            task_id=str(task.id),
            event_type="task.completed",
            extra=_wh_complete_extra,
        ))

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

    # Update worker skill profile
    try:
        from routers.skills import update_worker_skill
        resp_minutes: float | None = None
        if assignment.submitted_at and assignment.claimed_at:
            delta = assignment.submitted_at - assignment.claimed_at
            resp_minutes = delta.total_seconds() / 60
        await update_worker_skill(
            db,
            worker_id=assignment.worker_id,
            task_type=task.type,
            outcome="rejected",
            response_minutes=resp_minutes,
            credits_earned=0,
        )
    except Exception:
        pass

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

    # Webhook: task.rejected (per-task + persistent endpoints)
    _wh_rejected_extra = {"type": task.type, "assignment_id": str(assignment_id),
                          "worker_id": str(assignment.worker_id), "refund_credits": refund_amount}
    if task.webhook_url:
        asyncio.create_task(fire_webhook_for_task(
            task=task,
            event_type="task.rejected",
            extra=_wh_rejected_extra,
        ))
    asyncio.create_task(fire_persistent_endpoints(
        user_id=str(task.user_id),
        task_id=str(task.id),
        event_type="task.rejected",
        extra=_wh_rejected_extra,
    ))

    return SubmissionReviewResponse(
        assignment_id=assignment.id,
        status="rejected",
        message=f"Submission rejected. {refund_amount} credits refunded to your account.",
    )


# ─── Task Analytics ────────────────────────────────────────────────────────

@router.get("/{task_id}/analytics", response_model=TaskAnalyticsOut)
async def get_task_analytics(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return aggregated analytics for a specific task (requester only)."""
    task_result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Load all assignments with worker info
    a_result = await db.execute(
        select(TaskAssignmentDB, UserDB.name)
        .outerjoin(UserDB, TaskAssignmentDB.worker_id == UserDB.id)
        .where(TaskAssignmentDB.task_id == task_id)
        .order_by(TaskAssignmentDB.submitted_at.desc().nullslast())
    )
    rows = a_result.all()

    approved_count = 0
    rejected_count = 0
    pending_count = 0
    total_credits = 0
    response_times: list[float] = []
    response_distribution: dict = {}
    assignment_rows: list[AssignmentAnalyticsRow] = []

    for assignment, worker_name in rows:
        if assignment.status == "approved":
            approved_count += 1
        elif assignment.status == "rejected":
            rejected_count += 1
        else:
            pending_count += 1

        total_credits += assignment.earnings_credits

        resp_minutes: float | None = None
        if assignment.submitted_at and assignment.claimed_at:
            delta = assignment.submitted_at - assignment.claimed_at
            resp_minutes = delta.total_seconds() / 60
            response_times.append(resp_minutes)

        # Build response distribution
        if assignment.response and isinstance(assignment.response, dict):
            # Try common keys: label, answer, rating, verdict
            for key in ("label", "answer", "rating", "verdict", "choice"):
                val = assignment.response.get(key)
                if val is not None:
                    val_str = str(val)
                    response_distribution[val_str] = response_distribution.get(val_str, 0) + 1
                    break

        # Gold standard accuracy check
        is_accurate: bool | None = None
        if task.is_gold_standard and task.gold_answer and assignment.response:
            # Simple equality check — works for label/answer style tasks
            gold_label = task.gold_answer.get("label") or task.gold_answer.get("answer")
            resp_label = assignment.response.get("label") or assignment.response.get("answer")
            if gold_label is not None and resp_label is not None:
                is_accurate = str(gold_label).lower() == str(resp_label).lower()

        assignment_rows.append(
            AssignmentAnalyticsRow(
                worker_id=assignment.worker_id,
                worker_name=worker_name,
                status=assignment.status,
                submitted_at=assignment.submitted_at,
                response_minutes=resp_minutes,
                earnings_credits=assignment.earnings_credits,
                is_accurate=is_accurate,
            )
        )

    avg_response_minutes = (
        sum(response_times) / len(response_times) if response_times else None
    )

    # Accuracy rate for gold standard tasks
    accuracy_rate: float | None = None
    if task.is_gold_standard:
        graded = [r for r in assignment_rows if r.is_accurate is not None]
        if graded:
            accuracy_rate = sum(1 for r in graded if r.is_accurate) / len(graded)

    title = (
        task.input.get("title", task.type.replace("_", " ").title())
        if isinstance(task.input, dict)
        else task.type.replace("_", " ").title()
    )

    return TaskAnalyticsOut(
        task_id=task.id,
        task_type=task.type,
        title=title,
        status=task.status,
        execution_mode=task.execution_mode,
        total_assignments=len(rows),
        approved_count=approved_count,
        rejected_count=rejected_count,
        pending_count=pending_count,
        avg_response_minutes=avg_response_minutes,
        total_credits_paid=total_credits,
        is_gold_standard=task.is_gold_standard,
        accuracy_rate=accuracy_rate,
        response_distribution=response_distribution,
        assignments=assignment_rows,
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

            # Fire webhook if set (per-task + persistent endpoints)
            _wh_done_extra = {"type": task.type, "duration_ms": duration_ms,
                              "credits_used": task.credits_used}
            if task.webhook_url:
                asyncio.create_task(fire_webhook_for_task(
                    task=task,
                    event_type="task.completed",
                    extra=_wh_done_extra,
                ))
            asyncio.create_task(fire_persistent_endpoints(
                user_id=str(task.user_id),
                task_id=str(task_id),
                event_type="task.completed",
                extra=_wh_done_extra,
            ))

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

                    # Email notification + webhook
                    user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
                    owner = user_result.scalar_one_or_none()
                    if owner and owner.email:
                        asyncio.create_task(notify_task_failed(
                            owner.email, task_id, task.type, str(e)
                        ))
                    _wh_fail_extra = {"type": task.type, "error": str(e)}
                    if task.webhook_url:
                        asyncio.create_task(fire_webhook_for_task(
                            task=task,
                            event_type="task.failed",
                            extra=_wh_fail_extra,
                        ))
                    asyncio.create_task(fire_persistent_endpoints(
                        user_id=str(user_id),
                        task_id=str(task_id),
                        event_type="task.failed",
                        extra=_wh_fail_extra,
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

                    # Email notification + webhook
                    user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
                    owner = user_result.scalar_one_or_none()
                    if owner and owner.email:
                        asyncio.create_task(notify_task_failed(
                            owner.email, task_id, task.type, f"Unexpected error: {type(e).__name__}"
                        ))
                    _wh_unexp_extra = {"type": task.type, "error": f"Unexpected error: {type(e).__name__}"}
                    if task.webhook_url:
                        asyncio.create_task(fire_webhook_for_task(
                            task=task,
                            event_type="task.failed",
                            extra=_wh_unexp_extra,
                        ))
                    asyncio.create_task(fire_persistent_endpoints(
                        user_id=str(user_id),
                        task_id=str(task_id),
                        event_type="task.failed",
                        extra=_wh_unexp_extra,
                    ))
            except Exception:
                pass


# _send_webhook / _log_webhook removed — logic moved to core/webhooks.py


# ─── Task Rerun ──────────────────────────────────────────────────────────────

@router.post("/{task_id}/rerun", response_model=TaskCreateResponse, status_code=201)
async def rerun_task(
    task_id: UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Clone an existing task and immediately queue it for execution.

    Copies all settings from the original task (type, input, priority,
    metadata, webhook config, human-task options, etc.) into a brand-new
    task owned by the same user.  Credits are deducted from the requester's
    balance exactly as for a new task.

    Returns the new task_id together with the credit cost.
    """
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Task not found")

    is_human = original.execution_mode == "human"
    if is_human:
        worker_reward = original.worker_reward_credits or HUMAN_TASK_BASE_CREDITS.get(original.type, 2)
        platform_fee = max(1, int(worker_reward * original.assignments_required * 0.2))
        estimated_credits = worker_reward * original.assignments_required + platform_fee
    else:
        estimated_credits = TASK_CREDITS.get(original.type, 5)

    # Deduct credits
    user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Use org pool if original task had an org
    if original.org_id:
        from models.db import OrganizationDB
        org_result = await db.execute(
            select(OrganizationDB).where(OrganizationDB.id == original.org_id)
        )
        org = org_result.scalar_one_or_none()
        if org and org.credits >= estimated_credits:
            org.credits -= estimated_credits
        elif user.credits >= estimated_credits:
            user.credits -= estimated_credits
        else:
            raise HTTPException(
                status_code=402,
                detail=f"Insufficient credits. Need {estimated_credits}, have {user.credits}.",
            )
    else:
        if user.credits < estimated_credits:
            raise HTTPException(
                status_code=402,
                detail=f"Insufficient credits. Need {estimated_credits}, have {user.credits}.",
            )
        user.credits -= estimated_credits

    new_task = TaskDB(
        user_id=user_id,
        org_id=original.org_id,
        type=original.type,
        status="queued" if not is_human else "open",
        execution_mode=original.execution_mode,
        input=original.input,
        priority=original.priority,
        metadata=original.metadata,
        webhook_url=original.webhook_url,
        webhook_events=original.webhook_events,
        credits_used=estimated_credits,
        # Human-task fields
        worker_reward_credits=original.worker_reward_credits,
        assignments_required=original.assignments_required,
        assignments_completed=0,
        claim_timeout_minutes=original.claim_timeout_minutes,
        task_instructions=original.task_instructions,
        consensus_strategy=original.consensus_strategy,
        min_skill_level=original.min_skill_level,
        min_reputation_score=original.min_reputation_score,
    )
    db.add(new_task)

    # Credit transaction log
    txn = CreditTransactionDB(
        user_id=user_id,
        amount=-estimated_credits,
        description=f"Task rerun: {original.type} (from {original.id})",
        task_id=new_task.id,
    )
    db.add(txn)

    await db.commit()
    await db.refresh(new_task)

    logger.info(
        "task_rerun",
        original_id=str(task_id),
        new_task_id=str(new_task.id),
        user_id=user_id,
        credits=estimated_credits,
    )

    # Fire webhook notification if configured
    try:
        if new_task.webhook_url or new_task.webhook_events:
            background_tasks.add_task(
                fire_webhook_for_task,
                task=new_task,
                event_type="task.created",
                extra={"rerun_of": str(task_id)},
            )
        background_tasks.add_task(
            fire_persistent_endpoints,
            user_id=user_id,
            task_id=str(new_task.id),
            event_type="task.created",
            extra={"rerun_of": str(task_id)},
        )
    except Exception:
        pass

    # Queue for AI execution immediately
    if not is_human:
        background_tasks.add_task(_run_task, str(new_task.id), user_id)

    return TaskCreateResponse(
        task_id=new_task.id,
        status=new_task.status,
        estimated_credits=estimated_credits,
    )
