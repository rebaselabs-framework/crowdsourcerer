"""Task CRUD + execution endpoints."""
from __future__ import annotations
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.background import safe_create_task
from core.scopes import require_scope, SCOPE_TASKS_READ, SCOPE_TASKS_WRITE
from core.database import get_db
from core.webhooks import fire_webhook_for_task, fire_persistent_endpoints, ALL_EVENTS, DEFAULT_EVENTS
from models.db import TaskDB, UserDB, CreditTransactionDB, TaskAssignmentDB, WebhookLogDB, WorkerSkillDB
from models.schemas import (
    TaskCreateRequest, TaskCreateResponse, TaskOut, PaginatedTasks,
    BatchTaskCreateRequest, BatchTaskCreateResponse,
    BulkActionRequest, BulkActionResult,
    BulkCancelRequest, BulkCancelResult,
    BulkArchiveRequest, BulkArchiveResult,
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
    # with_for_update() serialises concurrent task-creation requests so two
    # requests with the same user can't both pass the balance check and both
    # deduct credits before either commits (classic lost-update race condition).
    result = await db.execute(
        select(UserDB).where(UserDB.id == user_id).with_for_update()
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Enforce plan quota limits (daily + per-minute burst)
    from core.quotas import enforce_task_creation_quota, enforce_task_burst_limit, record_task_creation, record_task_burst
    await enforce_task_creation_quota(db, user_id, user.plan)
    await enforce_task_burst_limit(db, user_id, user.plan)

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
            task_metadata=req.metadata,
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
            task_metadata=req.metadata,
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

    # Record quota usage (daily + burst)
    await record_task_creation(db, user_id)
    await record_task_burst(db, user_id)

    # Hook requester onboarding: create_task step
    safe_create_task(_mark_requester_onboarding(user_id, "create_task"))

    if not is_human and not is_scheduled:
        # Run AI task in background (scheduled tasks wait for the sweeper)
        background_tasks.add_task(_run_task, str(task.id), str(user_id))

    # Fire task.created webhook (per-task + persistent endpoints)
    _wh_extra = {"type": task.type, "status": task.status, "priority": task.priority}
    if task.webhook_url:
        safe_create_task(fire_webhook_for_task(
            task=task,
            event_type="task.created",
            extra=_wh_extra,
        ))
    safe_create_task(fire_persistent_endpoints(
        user_id=str(task.user_id),
        task_id=str(task.id),
        event_type="task.created",
        extra=_wh_extra,
    ))

    # Notify workers whose saved-search alerts match this new task
    if is_human:
        safe_create_task(_trigger_saved_search_alerts(
            task_type=task.type,
            priority=task.priority,
            reward_credits=task.worker_reward_credits,
            task_id=str(task.id),
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
    task_id: str = "",
    task_title: Optional[str] = None,
) -> None:
    """Background task: notify workers whose saved searches match a new human task."""
    from core.database import AsyncSessionLocal
    from routers.saved_searches import notify_matching_saved_searches
    from core.email import notify_matched_workers_of_task
    async with AsyncSessionLocal() as db:
        try:
            await notify_matching_saved_searches(task_type, priority, reward_credits, db)
        except Exception:
            logger.warning(
                "background_task.saved_search_alerts_failed",
                task_id=task_id,
                task_type=task_type,
                exc_info=True,
            )
        try:
            await notify_matched_workers_of_task(
                task_id=task_id,
                task_type=task_type,
                reward_credits=reward_credits or 0,
                task_title=task_title,
                db=db,
            )
        except Exception:
            logger.warning(
                "background_task.worker_email_notification_failed",
                task_id=task_id,
                task_type=task_type,
                exc_info=True,
            )


async def _mark_requester_onboarding(user_id: str, step: str) -> None:
    """Background task: advance requester onboarding step."""
    from core.database import AsyncSessionLocal
    from routers.requester_onboarding import complete_step_internal
    async with AsyncSessionLocal() as db:
        try:
            await complete_step_internal(user_id, step, db)
        except Exception:
            logger.warning(
                "background_task.requester_onboarding_failed",
                user_id=user_id,
                step=step,
                exc_info=True,
            )


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

    # Lock user row to prevent concurrent batch requests from racing on balance.
    result = await db.execute(
        select(UserDB).where(UserDB.id == user_id).with_for_update()
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Enforce plan quota limits (batch size + daily limit + burst)
    from core.quotas import enforce_task_creation_quota, enforce_task_burst_limit, record_task_creation, record_task_burst, enforce_batch_size
    enforce_batch_size(user.plan, len(req.tasks))
    await enforce_task_creation_quota(db, user_id, user.plan, task_count=len(req.tasks))
    await enforce_task_burst_limit(db, user_id, user.plan, task_count=len(req.tasks))

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

    # Deduct all credits upfront (may be partially refunded below if any tasks fail)
    user.credits -= total_credits

    created = []
    failed = []
    ai_task_ids: list[str] = []
    actual_credits_charged = 0

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
                    task_metadata=task_req.metadata,
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
                    task_metadata=task_req.metadata,
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
            actual_credits_charged += est
            created.append((task, est, is_human))
        except Exception as e:
            failed.append({"index": i, "type": task_req.type, "error": str(e)})

    # Refund credits for any tasks that failed to create
    overcharged = total_credits - actual_credits_charged
    if overcharged > 0:
        user.credits += overcharged
        logger.warning(
            "batch_tasks.partial_credit_refund",
            user_id=user_id,
            overcharged=overcharged,
            failed_count=len(failed),
        )

    await db.commit()

    # Record quota usage for successfully created tasks (daily + burst)
    if created:
        await record_task_creation(db, user_id, task_count=len(created))
        await record_task_burst(db, user_id, task_count=len(created))

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
    q: Optional[str] = Query(None, description="Text search across task instructions/title"),
    sort: str = Query("newest", description="Sort order: newest | reward_high | reward_low | urgent"),
    min_reward: Optional[int] = Query(None, description="Minimum worker reward (credits)"),
    max_reward: Optional[int] = Query(None, description="Maximum worker reward (credits)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Public task feed — returns open human tasks (no auth required).

    Only exposes safe, non-sensitive fields: id, type, title (from instructions),
    worker_reward_credits, assignments_required, assignments_completed, created_at.
    Supports text search (q), type filter, reward range filter, and sort options.
    """
    base_q = select(TaskDB).where(
        TaskDB.status == "open",
        TaskDB.execution_mode == "human",
    )
    if type:
        base_q = base_q.where(TaskDB.type == type)
    if q and q.strip():
        search_term = f"%{q.strip().lower()}%"
        base_q = base_q.where(
            func.lower(TaskDB.task_instructions).like(search_term)
        )
    if min_reward is not None:
        base_q = base_q.where(TaskDB.worker_reward_credits >= min_reward)
    if max_reward is not None:
        base_q = base_q.where(TaskDB.worker_reward_credits <= max_reward)

    total_result = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_result.scalar() or 0

    # Apply sort order
    if sort == "reward_high":
        base_q = base_q.order_by(TaskDB.worker_reward_credits.desc(), TaskDB.created_at.desc())
    elif sort == "reward_low":
        base_q = base_q.order_by(TaskDB.worker_reward_credits.asc(), TaskDB.created_at.desc())
    elif sort == "urgent":
        # Urgent = high priority first, then newest
        from sqlalchemy import case as sa_case
        priority_order = sa_case(
            (TaskDB.priority == "urgent", 0),
            (TaskDB.priority == "high", 1),
            (TaskDB.priority == "normal", 2),
            (TaskDB.priority == "low", 3),
            else_=4,
        )
        base_q = base_q.order_by(priority_order, TaskDB.created_at.desc())
    else:
        # newest (default)
        base_q = base_q.order_by(TaskDB.created_at.desc())

    base_q = base_q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(base_q)
    tasks = result.scalars().all()

    # Return sanitised subset of fields only
    items = [
        {
            "id": str(t.id),
            "type": t.type,
            "title": (t.task_instructions[:80] + "…") if t.task_instructions and len(t.task_instructions) > 80 else (t.task_instructions or t.type.replace("_", " ").title()),
            "worker_reward_credits": t.worker_reward_credits,
            "assignments_required": t.assignments_required,
            "assignments_completed": t.assignments_completed,
            "priority": t.priority,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tasks
    ]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "query": q or "",
        "sort": sort,
    }


@router.get("/scheduled")
async def list_scheduled_tasks(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """Return tasks with a scheduled_at in the future, ordered by schedule time ascending."""
    now_utc = datetime.now(timezone.utc)
    result = await db.execute(
        select(TaskDB).where(
            TaskDB.user_id == user_id,
            TaskDB.scheduled_at.isnot(None),
            TaskDB.scheduled_at > now_utc,
            TaskDB.status == "pending",
        ).order_by(TaskDB.scheduled_at.asc()).limit(limit)
    )
    tasks = result.scalars().all()
    return {
        "items": [
            {
                "id": str(t.id),
                "type": t.type,
                "status": t.status,
                "execution_mode": t.execution_mode,
                "priority": t.priority,
                "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
                "created_at": t.created_at.isoformat(),
                "tags": t.tags or [],
            }
            for t in tasks
        ],
        "total": len(tasks),
    }


@router.get("/review-summary")
async def get_review_summary(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """Return count of pending worker submissions awaiting requester review."""
    from models.db import TaskAssignmentDB
    stmt = (
        select(func.count())
        .select_from(TaskAssignmentDB)
        .join(TaskDB, TaskAssignmentDB.task_id == TaskDB.id)
        .where(
            TaskDB.user_id == user_id,
            TaskAssignmentDB.status == "submitted",
        )
    )
    result = await db.execute(stmt)
    count = result.scalar() or 0
    return {"pending_count": count}


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
    q: Optional[str] = Query(None, description="Free-text search on task instructions (case-insensitive)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    query = select(TaskDB).where(TaskDB.user_id == user_id)
    if status:
        query = query.where(TaskDB.status == status)
    if type:
        query = query.where(TaskDB.type == type)
    if execution_mode:
        query = query.where(TaskDB.execution_mode == execution_mode)
    if tag:
        # JSON array contains this tag
        query = query.where(TaskDB.tags.contains([tag]))
    if q and q.strip():
        # Case-insensitive search on task_instructions; falls back to type match
        search_term = f"%{q.strip()}%"
        from sqlalchemy import or_, cast, Text
        query = query.where(
            or_(
                TaskDB.task_instructions.ilike(search_term),
                cast(TaskDB.input, Text).ilike(search_term),
            )
        )
    if has_submissions is True:
        # Only tasks that have at least one submitted/approved assignment
        query = query.where(
            TaskDB.id.in_(
                select(TaskAssignmentDB.task_id).where(
                    TaskAssignmentDB.status.in_(["submitted", "approved", "rejected"])
                ).distinct()
            )
        )

    total_result = await db.execute(
        select(func.count()).select_from(query.subquery())
    )
    total = total_result.scalar() or 0

    query = query.order_by(TaskDB.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
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

    # Bulk-load all requested tasks in one query
    tasks_result = await db.execute(
        select(TaskDB).where(
            TaskDB.id.in_(req.task_ids),
            TaskDB.user_id == user_id,
        )
    )
    tasks_by_id: dict = {str(t.id): t for t in tasks_result.scalars()}

    for task_id in req.task_ids:
        task = tasks_by_id.get(str(task_id))
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


@router.post("/bulk-cancel", response_model=BulkCancelResult)
async def bulk_cancel_tasks(
    req: BulkCancelRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Cancel multiple tasks at once.

    Only tasks in `open`, `pending`, or `running` state are cancelled; others are skipped.
    """
    CANCELLABLE = {"open", "pending", "running"}
    cancelled_ids: list[str] = []
    skipped = 0

    # Bulk-load all requested tasks in one query
    tasks_result = await db.execute(
        select(TaskDB).where(
            TaskDB.id.in_(req.task_ids),
            TaskDB.user_id == user_id,
        )
    )
    tasks_by_id: dict = {str(t.id): t for t in tasks_result.scalars()}

    for task_id in req.task_ids:
        task = tasks_by_id.get(str(task_id))
        if not task or task.status not in CANCELLABLE:
            skipped += 1
            continue
        task.status = "cancelled"
        cancelled_ids.append(str(task_id))

    await db.commit()

    logger.info(
        "bulk_cancel",
        cancelled=len(cancelled_ids),
        skipped=skipped,
        user_id=user_id,
    )
    return BulkCancelResult(
        cancelled=len(cancelled_ids),
        skipped=skipped,
        task_ids=cancelled_ids,
    )


@router.post("/bulk-archive", response_model=BulkArchiveResult)
async def bulk_archive_tasks(
    req: BulkArchiveRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Archive multiple tasks at once.

    Only tasks in a terminal state (completed, failed, cancelled) are archived; others are skipped.
    """
    ARCHIVABLE = {"completed", "failed", "cancelled"}
    archived_ids: list[str] = []
    skipped = 0

    # Bulk-load all requested tasks in one query
    tasks_result = await db.execute(
        select(TaskDB).where(
            TaskDB.id.in_(req.task_ids),
            TaskDB.user_id == user_id,
        )
    )
    tasks_by_id: dict = {str(t.id): t for t in tasks_result.scalars()}

    for task_id in req.task_ids:
        task = tasks_by_id.get(str(task_id))
        if not task or task.status not in ARCHIVABLE:
            skipped += 1
            continue
        task.status = "archived"
        archived_ids.append(str(task_id))

    await db.commit()

    logger.info(
        "bulk_archive",
        archived=len(archived_ids),
        skipped=skipped,
        user_id=user_id,
    )
    return BulkArchiveResult(
        archived=len(archived_ids),
        skipped=skipped,
        task_ids=archived_ids,
    )


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


@router.post("/{task_id}/cancel", status_code=204, response_model=None)
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

    # Bulk-load all workers in a single query instead of N per-assignment queries
    worker_ids = [a.worker_id for a in assignments]
    workers_by_id: dict = {}
    if worker_ids:
        w_result = await db.execute(select(UserDB).where(UserDB.id.in_(worker_ids)))
        workers_by_id = {str(w.id): w for w in w_result.scalars()}

    out: list[SubmissionOut] = []
    for a in assignments:
        worker = workers_by_id.get(str(a.worker_id))
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
        logger.warning(
            "approve_submission.skill_update_failed",
            task_id=str(task_id),
            assignment_id=str(assignment_id),
            exc_info=True,
        )

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
        logger.warning(
            "approve_submission.notification_failed",
            task_id=str(task_id),
            assignment_id=str(assignment_id),
            exc_info=True,
        )

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
            safe_create_task(notify_worker_approved(
                worker_user.email,
                task_type=task.type,
                earnings=assignment.earnings_credits,
                xp=assignment.xp_earned,
            ))
    except Exception:
        logger.warning(
            "approve_submission.email_notification_failed",
            task_id=str(task_id),
            assignment_id=str(assignment_id),
            exc_info=True,
        )

    # If task is now completed (requester_review strategy), resume any waiting pipeline
    if task.status == "completed" and task.output is not None:
        try:
            from routers.pipelines import resume_pipeline_after_human_step
            await resume_pipeline_after_human_step(task.id, task.output, db)
        except Exception:
            logger.warning(
                "approve_submission.pipeline_resume_failed",
                task_id=str(task_id),
                exc_info=True,
            )

    # Webhook: task.approved / task.completed (per-task + persistent endpoints)
    _wh_approved_extra = {"type": task.type, "assignment_id": str(assignment_id),
                          "worker_id": str(assignment.worker_id)}
    if task.webhook_url:
        safe_create_task(fire_webhook_for_task(
            task=task,
            event_type="task.approved",
            extra=_wh_approved_extra,
        ))
    safe_create_task(fire_persistent_endpoints(
        user_id=str(task.user_id),
        task_id=str(task.id),
        event_type="task.approved",
        extra=_wh_approved_extra,
    ))
    if task.status == "completed":
        _wh_complete_extra = {"type": task.type, "execution_mode": "human"}
        if task.webhook_url:
            safe_create_task(fire_webhook_for_task(
                task=task,
                event_type="task.completed",
                extra=_wh_complete_extra,
            ))
        safe_create_task(fire_persistent_endpoints(
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
        logger.warning(
            "reject_submission.skill_update_failed",
            task_id=str(task_id),
            assignment_id=str(assignment_id),
            exc_info=True,
        )

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
        logger.warning(
            "reject_submission.notification_failed",
            task_id=str(task_id),
            assignment_id=str(assignment_id),
            exc_info=True,
        )

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
        safe_create_task(fire_webhook_for_task(
            task=task,
            event_type="task.rejected",
            extra=_wh_rejected_extra,
        ))
    safe_create_task(fire_persistent_endpoints(
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


# ─── SSE — dashboard live stream (all active tasks) ─────────────────────────

@router.get("/stream")
async def dashboard_task_stream(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Server-Sent Events stream for the task dashboard.

    Polls all of the current user's non-terminal tasks and emits events
    whenever any status, progress, or priority changes.  Closes automatically
    once no active tasks remain (or after 10 minutes).

    Event format::

        data: {"task_id": "...", "status": "...", "assignments_completed": N,
               "assignments_required": N, "priority": "...",
               "completed_at": "ISO-or-null", "error": "or-null"}

    A ``{"event": "heartbeat"}`` is sent every 5 s to keep connections alive.
    A ``{"event": "stream_end"}`` is sent before the connection closes.
    """
    TERMINAL = {"completed", "failed", "cancelled", "archived"}

    async def event_generator() -> AsyncIterator[str]:
        from core.database import AsyncSessionLocal

        poll_interval = 2.0   # seconds between DB checks
        max_polls = 300        # 10 minutes at 2s intervals
        # last seen snapshot: task_id → (status, assignments_completed, priority)
        snapshot: dict[str, tuple[str, int, str]] = {}
        heartbeat_counter = 0

        for _ in range(max_polls):
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(TaskDB).where(
                        TaskDB.user_id == user_id,
                        TaskDB.status.notin_(list(TERMINAL)),
                    )
                )
                active_tasks = result.scalars().all()

            for t in active_tasks:
                tid = str(t.id)
                current = (t.status, t.assignments_completed or 0, t.priority or "normal")
                if snapshot.get(tid) != current:
                    snapshot[tid] = current
                    payload = {
                        "task_id": tid,
                        "status": t.status,
                        "assignments_completed": t.assignments_completed or 0,
                        "assignments_required": t.assignments_required or 1,
                        "priority": t.priority or "normal",
                        "priority_escalated_at": (
                            t.priority_escalated_at.isoformat()
                            if getattr(t, "priority_escalated_at", None)
                            else None
                        ),
                        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                        "error": t.error,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"

            # If nothing active, we're done
            if not active_tasks:
                break

            # Heartbeat every ~5 s (every 2-3 polls)
            heartbeat_counter += 1
            if heartbeat_counter % 3 == 0:
                yield 'data: {"event": "heartbeat"}\n\n'

            await asyncio.sleep(poll_interval)

        yield 'data: {"event": "stream_end"}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── SSE — LLM output token stream ─────────────────────────────────────────

@router.get("/{task_id}/output-stream")
async def task_output_stream(
    task_id: UUID,
    speed: float = Query(default=40.0, ge=1.0, le=500.0, description="Characters per second"),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Server-Sent Events stream that delivers the LLM output of a completed task
    character-by-character at the requested speed (default 40 chars/sec).

    For ``llm_generate`` (and any AI task with a text output), this creates
    the effect of live token streaming even for already-completed tasks.

    While the task is still running, emits buffering heartbeats and waits up
    to 10 minutes for completion before closing the stream.

    Events:
    - ``{"event": "token", "char": "x", "position": N}``  — one character
    - ``{"event": "done"}``  — all characters delivered
    - ``{"event": "buffering"}``  — task still running, waiting
    - ``{"event": "error", "detail": "..."}``  — task failed
    """
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    delay = 1.0 / speed  # seconds per character

    async def event_generator() -> AsyncIterator[str]:
        from core.database import AsyncSessionLocal
        TERMINAL = {"completed", "failed", "cancelled"}
        max_wait_polls = 300  # 10 minutes at 2s intervals

        # ── Phase 1: wait for task to reach a terminal state ─────────────
        polls = 0
        while polls < max_wait_polls:
            async with AsyncSessionLocal() as session:
                r = await session.execute(select(TaskDB).where(TaskDB.id == task_id))
                t = r.scalar_one_or_none()
                if t is None:
                    yield 'data: {"event": "error", "detail": "Task not found"}\n\n'
                    return
                if t.status in TERMINAL:
                    break
            yield 'data: {"event": "buffering", "status": "' + (t.status or "pending") + '"}\n\n'
            await asyncio.sleep(2.0)
            polls += 1
        else:
            yield 'data: {"event": "error", "detail": "Timed out waiting for task completion"}\n\n'
            return

        # ── Phase 2: load output text ────────────────────────────────────
        async with AsyncSessionLocal() as session:
            r = await session.execute(select(TaskDB).where(TaskDB.id == task_id))
            t = r.scalar_one_or_none()

        if not t or t.status != "completed":
            error_detail = (t.error or "Task did not complete successfully") if t else "Task not found"
            yield f'data: {{"event": "error", "detail": {json.dumps(error_detail)}}}\n\n'
            return

        # Extract text from output — try common LLM output keys
        output_text = ""
        if t.output:
            for key in ("result", "text", "content", "answer", "response", "output", "summary"):
                val = t.output.get(key)
                if isinstance(val, str) and val.strip():
                    output_text = val
                    break
            if not output_text:
                output_text = json.dumps(t.output, ensure_ascii=False, indent=2)

        if not output_text:
            yield 'data: {"event": "done", "total_chars": 0}\n\n'
            return

        # ── Phase 3: stream characters ───────────────────────────────────
        for pos, char in enumerate(output_text):
            payload = json.dumps({"event": "token", "char": char, "position": pos})
            yield f"data: {payload}\n\n"
            await asyncio.sleep(delay)

        yield f'data: {{"event": "done", "total_chars": {len(output_text)}}}\n\n'

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

def _result_preview(output: dict | None, max_chars: int = 200) -> str:
    """Extract a short human-readable snippet from a task output blob.

    Tries common keys (``result``, ``text``, ``summary``, ``output``,
    ``content``, ``answer``) in order, falls back to the raw JSON repr.
    Returns at most *max_chars* characters, ellipsised.
    """
    if not output:
        return ""
    for key in ("result", "text", "summary", "output", "content", "answer", "response"):
        val = output.get(key)
        if isinstance(val, str) and val.strip():
            snippet = val.strip()
            return snippet[:max_chars] + ("…" if len(snippet) > max_chars else "")
    # Fallback: render whole dict as JSON and truncate
    raw = json.dumps(output, ensure_ascii=False)
    return raw[:max_chars] + ("…" if len(raw) > max_chars else "")


async def _store_cache_entry(
    task_type: str,
    task_input: dict,
    output: dict,
    full_credits_cost: int,
    duration_ms: int | None,
) -> None:
    """Background coroutine: persist a task result to the cache with its own DB session."""
    from core.database import AsyncSessionLocal
    from core.result_cache import cache_store
    async with AsyncSessionLocal() as db:
        await cache_store(db, task_type, task_input, output, full_credits_cost, duration_ms)


async def _run_task(task_id: str, user_id: str):
    """Execute an AI task against RebaseKit (or return a cached result) and store the outcome."""
    from core.database import AsyncSessionLocal  # avoid circular import at module level
    from core.email import notify_task_completed, notify_task_failed
    from core.notify import create_notification, NotifType
    from core.result_cache import (
        cache_lookup, cache_store, CACHE_HIT_FEE_CREDITS,
    )

    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
            task = result.scalar_one_or_none()
            if not task:
                return
            # Guard against double-invocation: only execute if still queued.
            # If the task was already picked up by another invocation (or is in
            # any other state), silently skip — this prevents duplicate execution
            # and double-processing of webhooks/notifications.
            if task.status != "queued":
                logger.warning(
                    "task_run.skipped_non_queued",
                    task_id=task_id,
                    status=task.status,
                )
                return

            task.status = "running"
            task.started_at = datetime.now(timezone.utc)
            await db.commit()

            full_cost = TASK_CREDITS.get(task.type, 5)
            cache_entry = await cache_lookup(db, task.type, task.input)
            cache_hit = cache_entry is not None

            if cache_hit:
                # ── Cache hit: use stored result, skip external API call ──
                output = cache_entry.output
                duration_ms = 0  # instant from cache
                credits_used = CACHE_HIT_FEE_CREDITS

                # Refund the difference so the requester only pays the nominal fee
                refund_amount = full_cost - CACHE_HIT_FEE_CREDITS
                if refund_amount > 0:
                    user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
                    owner = user_result.scalar_one_or_none()
                    if owner:
                        owner.credits += refund_amount
                        refund_txn = CreditTransactionDB(
                            user_id=user_id,
                            task_id=task.id,
                            amount=refund_amount,
                            type="refund",
                            description=f"Cache hit refund: {task.type}",
                        )
                        db.add(refund_txn)

                logger.info(
                    "task_cache_hit",
                    task_id=task_id,
                    type=task.type,
                    refunded_credits=refund_amount if refund_amount > 0 else 0,
                )
            else:
                # ── Cache miss: call RebaseKit ──
                t0 = time.perf_counter()
                client = get_rebasekit_client()
                output = await execute_task(task.type, task.input, client)
                duration_ms = int((time.perf_counter() - t0) * 1000)
                credits_used = full_cost

                # Store result for future cache hits (fire-and-forget, own DB session)
                safe_create_task(
                    _store_cache_entry(task.type, task.input, output, full_cost, duration_ms)
                )

            task.status = "completed"
            task.output = output
            task.cached = cache_hit
            task.duration_ms = duration_ms
            task.completed_at = datetime.now(timezone.utc)
            task.credits_used = credits_used

            # Build result preview snippet for notifications + webhooks
            preview = _result_preview(output)
            task_label = task.type.replace("_", " ")
            if cache_hit:
                notif_body = (
                    f"Your {task_label} task completed instantly from cache"
                    f" (saved {full_cost - CACHE_HIT_FEE_CREDITS} credits)."
                    + (f" — {preview}" if preview else "")
                )
            else:
                notif_body = (
                    f"Your {task_label} task finished in {duration_ms}ms."
                    + (f" — {preview}" if preview else "")
                )

            # In-app notification: task completed (with preview)
            await create_notification(
                db, user_id,
                NotifType.TASK_COMPLETED,
                "Task completed ✅",
                notif_body,
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

            # Fire webhook if set — include result_preview and cache flag in payload
            _wh_done_extra = {
                "type": task.type,
                "duration_ms": duration_ms,
                "credits_used": task.credits_used,
                "cached": cache_hit,
                **({"result_preview": preview} if preview else {}),
            }
            if task.webhook_url:
                safe_create_task(fire_webhook_for_task(
                    task=task,
                    event_type="task.completed",
                    extra=_wh_done_extra,
                ))
            safe_create_task(fire_persistent_endpoints(
                user_id=str(task.user_id),
                task_id=str(task_id),
                event_type="task.completed",
                extra=_wh_done_extra,
            ))

            # Email notification
            if owner and owner.email:
                safe_create_task(notify_task_completed(
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
                        safe_create_task(notify_task_failed(
                            owner.email, task_id, task.type, str(e)
                        ))
                    _wh_fail_extra = {"type": task.type, "error": str(e)}
                    if task.webhook_url:
                        safe_create_task(fire_webhook_for_task(
                            task=task,
                            event_type="task.failed",
                            extra=_wh_fail_extra,
                        ))
                    safe_create_task(fire_persistent_endpoints(
                        user_id=str(user_id),
                        task_id=str(task_id),
                        event_type="task.failed",
                        extra=_wh_fail_extra,
                    ))
            except Exception:
                logger.error(
                    "task_run.error_recovery_failed",
                    task_id=task_id,
                    exc_info=True,
                )

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
                        safe_create_task(notify_task_failed(
                            owner.email, task_id, task.type, f"Unexpected error: {type(e).__name__}"
                        ))
                    _wh_unexp_extra = {"type": task.type, "error": f"Unexpected error: {type(e).__name__}"}
                    if task.webhook_url:
                        safe_create_task(fire_webhook_for_task(
                            task=task,
                            event_type="task.failed",
                            extra=_wh_unexp_extra,
                        ))
                    safe_create_task(fire_persistent_endpoints(
                        user_id=str(user_id),
                        task_id=str(task_id),
                        event_type="task.failed",
                        extra=_wh_unexp_extra,
                    ))
            except Exception:
                logger.error(
                    "task_run.error_recovery_failed",
                    task_id=task_id,
                    exc_info=True,
                )


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

    # Deduct credits — lock user row to prevent concurrent rerun races.
    user_result = await db.execute(
        select(UserDB).where(UserDB.id == user_id).with_for_update()
    )
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
        task_metadata=original.task_metadata,
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
        logger.warning(
            "task_rerun.webhook_schedule_failed",
            task_id=str(task_id),
            new_task_id=str(new_task.id),
            exc_info=True,
        )

    # Queue for AI execution immediately
    if not is_human:
        background_tasks.add_task(_run_task, str(new_task.id), user_id)

    return TaskCreateResponse(
        task_id=new_task.id,
        status=new_task.status,
        estimated_credits=estimated_credits,
    )


# ── Related tasks ──────────────────────────────────────────────────────────────

@router.get("/{task_id}/related")
async def get_related_tasks(
    task_id: UUID,
    limit: int = Query(6, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """Return up to `limit` other tasks by the same requester with the same type.

    Ordered newest-first.  Useful for the 'Related Tasks' sidebar on the task
    detail page.  Only returns tasks owned by the authenticated user.
    """
    result = await db.execute(
        select(TaskDB)
        .where(
            TaskDB.user_id == user_id,
            TaskDB.id != task_id,
            # Join-less: filter by the same type as the target task via subquery
            TaskDB.type == select(TaskDB.type).where(TaskDB.id == task_id).scalar_subquery(),
        )
        .order_by(TaskDB.created_at.desc())
        .limit(limit)
    )
    tasks = result.scalars().all()
    return [
        {
            "id": str(t.id),
            "type": t.type,
            "status": t.status,
            "execution_mode": t.execution_mode,
            "credits_used": t.credits_used,
            "priority": t.priority,
            "tags": t.tags or [],
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }
        for t in tasks
    ]


# ── Suggested workers ──────────────────────────────────────────────────────────

@router.get("/{task_id}/suggested-workers")
async def get_suggested_workers(
    task_id: UUID,
    limit: int = Query(8, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """Return top workers for the task type, ranked by proficiency + accuracy + reputation.

    Only accessible by the task's owner (requester).  Useful for the
    'Suggested Workers' sidebar on the task detail page.
    """
    # Verify ownership
    task_result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Fetch top skilled workers for this task type (join with users for name/rep)
    result = await db.execute(
        select(WorkerSkillDB, UserDB)
        .join(UserDB, WorkerSkillDB.worker_id == UserDB.id)
        .where(
            WorkerSkillDB.task_type == task.type,
            WorkerSkillDB.tasks_completed >= 1,
            UserDB.is_banned.isnot(True),
            UserDB.availability_status.in_(["available", "busy"]),
        )
        .order_by(
            WorkerSkillDB.proficiency_level.desc(),
            WorkerSkillDB.accuracy.desc().nulls_last(),
            UserDB.reputation_score.desc(),
            WorkerSkillDB.tasks_completed.desc(),
        )
        .limit(limit)
    )
    rows = result.all()
    return [
        {
            "worker_id": str(skill.worker_id),
            "display_name": user.name or user.email.split("@")[0],
            "avatar_url": user.avatar_url,
            "proficiency_level": skill.proficiency_level,
            "accuracy": round(skill.accuracy, 2) if skill.accuracy is not None else None,
            "tasks_completed": skill.tasks_completed,
            "verified": skill.verified,
            "reputation_score": round(user.reputation_score, 1) if user.reputation_score else 50.0,
            "availability_status": user.availability_status,
            "last_task_at": skill.last_task_at.isoformat() if skill.last_task_at else None,
        }
        for skill, user in rows
    ]
