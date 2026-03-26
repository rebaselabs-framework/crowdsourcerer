"""Worker marketplace API — browse tasks, claim, submit, release."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal
import uuid as uuid_mod
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_

import asyncio as _asyncio_wh
from core.auth import get_current_user_id
from core.background import safe_create_task
from core.database import get_db
from core.reputation import refresh_worker_reputation
from core.webhooks import fire_webhook_for_task, fire_persistent_endpoints
from models.db import (
    TaskDB, UserDB, TaskAssignmentDB, CreditTransactionDB,
    DailyChallengeDB, DailyChallengeProgressDB,
)
from models.schemas import (
    BecomeWorkerRequest,
    WorkerProfileOut,
    WorkerSkillInterestsUpdate,
    WorkerStatsOut,
    MarketplaceTaskOut,
    PaginatedMarketplaceTasks,
    TaskAssignmentOut,
    TaskAssignmentWithTaskOut,
    TaskOut,
    WorkerTaskClaimResponse,
    WorkerTaskSubmitRequest,
    WorkerTaskSubmitResponse,
    WorkerRecommendationsOut,
    TaskTypeRecommendation,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/worker", tags=["worker"])

# ─── XP + Level system ────────────────────────────────────────────────────

# XP thresholds for each level (cumulative XP needed to reach that level)
LEVEL_THRESHOLDS = [
    0,      # Level 1
    100,    # Level 2
    250,    # Level 3
    500,    # Level 4
    1000,   # Level 5
    2000,   # Level 6
    3500,   # Level 7
    5500,   # Level 8
    8000,   # Level 9
    11000,  # Level 10
    15000,  # Level 11
    20000,  # Level 12
    26000,  # Level 13
    33000,  # Level 14
    41000,  # Level 15
    50000,  # Level 16
    60000,  # Level 17
    71000,  # Level 18
    83000,  # Level 19
    96000,  # Level 20
]

LEVEL_NAMES = [
    "", "Apprentice", "Novice", "Learner", "Explorer", "Contributor",
    "Analyst", "Specialist", "Expert", "Veteran", "Elite",
    "Master", "Grand Master", "Champion", "Legend", "Mythic",
    "Transcendent", "Ascendant", "Radiant", "Immortal", "Divine",
]

# Base XP earned per task completion
TASK_XP_BASE: dict[str, int] = {
    "label_image": 10,
    "label_text": 8,
    "rate_quality": 8,
    "verify_fact": 12,
    "moderate_content": 8,
    "compare_rank": 8,
    "answer_question": 15,
    "transcription_review": 15,
}

# Estimated completion times (minutes) per task type
TASK_ESTIMATED_MINUTES: dict[str, int] = {
    "label_image": 1,
    "label_text": 1,
    "rate_quality": 2,
    "verify_fact": 3,
    "moderate_content": 1,
    "compare_rank": 2,
    "answer_question": 5,
    "transcription_review": 5,
}


def compute_level(xp: int) -> tuple[int, int]:
    """Return (level, xp_to_next_level) for a given total XP."""
    level = 1
    for i, threshold in enumerate(LEVEL_THRESHOLDS):
        if xp >= threshold:
            level = i + 1
        else:
            break
    level = min(level, len(LEVEL_THRESHOLDS))
    if level < len(LEVEL_THRESHOLDS):
        xp_to_next = LEVEL_THRESHOLDS[level] - xp
    else:
        xp_to_next = 0  # Max level
    return level, max(0, xp_to_next)


# Streak multiplier tiers: (min_streak_days, multiplier)
# The UI on /worker/submitted promises "daily streak bonus multipliers".
STREAK_MULTIPLIER_TIERS: list[tuple[int, float]] = [
    (30, 2.0),   # 30+ consecutive days → 2× XP
    (14, 1.5),   # 14+ days → 1.5×
    (7,  1.25),  # 7+ days → 1.25×
    (3,  1.1),   # 3+ days → 1.1×
    (0,  1.0),   # no streak → 1×
]


def streak_xp_multiplier(streak_days: int) -> float:
    """Return the XP multiplier for a given streak length."""
    for threshold, multiplier in STREAK_MULTIPLIER_TIERS:
        if streak_days >= threshold:
            return multiplier
    return 1.0


def compute_xp_for_task(task_type: str, accurate: bool = True, streak_days: int = 0) -> int:
    """XP earned for completing a task.

    Bonuses applied in order:
    - Accuracy bonus: inaccurate submissions get half XP (minimum 1)
    - Streak bonus: consecutive daily completions multiply XP (see STREAK_MULTIPLIER_TIERS)
    """
    base = TASK_XP_BASE.get(task_type, 10)
    raw = base if accurate else max(1, base // 2)
    multiplier = streak_xp_multiplier(streak_days)
    return round(raw * multiplier)


# ─── Become a worker ──────────────────────────────────────────────────────

@router.post("/enroll", response_model=WorkerProfileOut)
async def enroll_as_worker(
    req: BecomeWorkerRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Enable worker mode for the current user account.

    Optionally accepts ``skills`` (a list of task-type strings) which are stored
    as the worker's initial skill interests to seed their personalised task feed.
    """
    from models.schemas import HUMAN_TASK_TYPES_SET

    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.role == "requester":
        user.role = "both"
    # If already 'worker' or 'both', no change needed

    # Save declared skill interests (filter to valid human task types only)
    if req.skills:
        valid_interests = [s for s in req.skills if s in HUMAN_TASK_TYPES_SET]
        if valid_interests:
            user.worker_skill_interests = valid_interests

    await db.commit()
    await db.refresh(user)
    return WorkerProfileOut.model_validate(user)


# ─── Worker profile ────────────────────────────────────────────────────────

@router.get("/profile", response_model=WorkerProfileOut)
async def get_worker_profile(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker. POST /v1/worker/enroll first.")
    return WorkerProfileOut.model_validate(user)


# ─── Skill interests ──────────────────────────────────────────────────────

@router.get("/interests", response_model=dict)
async def get_skill_interests(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return the worker's declared skill interests."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")
    return {"interests": user.worker_skill_interests or []}


@router.patch("/interests", response_model=dict)
async def update_skill_interests(
    req: WorkerSkillInterestsUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Set the worker's declared skill interests.

    Replaces the full list. Send an empty list to clear all interests.
    Valid values: label_image, label_text, rate_quality, verify_fact,
    moderate_content, compare_rank, answer_question, transcription_review.
    """
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    user.worker_skill_interests = req.interests
    await db.commit()

    logger.info(
        "worker.interests_updated",
        user_id=user_id,
        count=len(req.interests),
        interests=req.interests,
    )
    return {"interests": req.interests, "count": len(req.interests)}


# ─── Availability ─────────────────────────────────────────────────────────

class AvailabilityUpdate(BaseModel):
    status: Literal["available", "busy", "away"]


@router.patch("/availability")
async def set_availability(
    payload: AvailabilityUpdate,
    db: AsyncSession = Depends(get_db),
    current_user_id: str = Depends(get_current_user_id),
):
    """Set worker availability status."""
    user = await db.get(UserDB, UUID(current_user_id))
    if not user:
        raise HTTPException(404, "User not found")
    if user.role not in ("worker", "both"):
        raise HTTPException(403, "Only workers can set availability")
    user.availability_status = payload.status
    await db.commit()
    return {"availability_status": payload.status}


# ─── Worker stats ──────────────────────────────────────────────────────────

@router.get("/stats", response_model=WorkerStatsOut)
async def get_worker_stats(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    # Count assignments by status
    active_count = await db.scalar(
        select(func.count()).where(
            TaskAssignmentDB.worker_id == user_id,
            TaskAssignmentDB.status == "active",
        )
    ) or 0

    submitted_count = await db.scalar(
        select(func.count()).where(
            TaskAssignmentDB.worker_id == user_id,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
        )
    ) or 0

    released_count = await db.scalar(
        select(func.count()).where(
            TaskAssignmentDB.worker_id == user_id,
            TaskAssignmentDB.status.in_(["released", "timed_out"]),
        )
    ) or 0

    # Total earnings
    total_earnings = await db.scalar(
        select(func.sum(TaskAssignmentDB.earnings_credits)).where(
            TaskAssignmentDB.worker_id == user_id,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
        )
    ) or 0

    level, xp_to_next = compute_level(user.worker_xp)

    # Streak at-risk: True when the worker hasn't completed any task today (UTC).
    # A worker with streak_days > 0 and no activity today will lose their streak tomorrow.
    today_utc = datetime.now(timezone.utc).date()
    last_active = user.worker_last_active_date
    last_active_date_str: str | None = None
    streak_at_risk = False
    if last_active is not None:
        last_active_date = last_active.date() if hasattr(last_active, "date") else last_active
        last_active_date_str = str(last_active_date)
        # At risk when: has a streak AND last activity was before today
        streak_at_risk = user.worker_streak_days > 0 and last_active_date < today_utc

    return WorkerStatsOut(
        tasks_completed=user.worker_tasks_completed,
        tasks_active=active_count,
        tasks_released=released_count,
        total_earnings_credits=total_earnings,
        accuracy=user.worker_accuracy,
        reliability=user.worker_reliability,
        level=level,
        xp=user.worker_xp,
        xp_to_next_level=xp_to_next,
        streak_days=user.worker_streak_days,
        streak_at_risk=streak_at_risk,
        last_active_date=last_active_date_str,
    )


# ─── Activity calendar ────────────────────────────────────────────────────

@router.get("/activity/calendar")
async def get_activity_calendar(
    days: int = Query(14, ge=7, le=60),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return a list of dates (YYYY-MM-DD) on which this worker completed at
    least one task, covering the last ``days`` calendar days.

    Response: ``{"active_dates": ["2026-03-20", "2026-03-22", ...]}``
    """
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Find all approved assignments within the window
    rows = await db.execute(
        select(TaskAssignmentDB.submitted_at).where(
            TaskAssignmentDB.worker_id == user_id,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
            TaskAssignmentDB.submitted_at >= cutoff,
        )
    )
    dates_seen: set[str] = set()
    for (submitted_at,) in rows.fetchall():
        if submitted_at:
            dates_seen.add(submitted_at.date().isoformat())

    return {"active_dates": sorted(dates_seen)}


# ─── Skill-based task feed ────────────────────────────────────────────────

@router.get("/tasks/feed", response_model=PaginatedMarketplaceTasks)
async def skill_matched_task_feed(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return open tasks ranked by skill match for this worker.

    Tasks the worker is ineligible for (skill/reputation requirements not met)
    are excluded. Each item includes a ``match_score`` (0.0–1.0) field so the
    frontend can show a match indicator.
    """
    from core.matching import rank_tasks_for_worker

    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    if user.is_banned:
        raise HTTPException(status_code=403, detail="Your worker account has been suspended.")

    worker_rep = user.reputation_score or 0.0

    # Fetch a large pool of open tasks for scoring (up to 500 at a time)
    pool_size = min(500, page_size * 25)
    q = select(TaskDB).where(
        TaskDB.status == "open",
        TaskDB.execution_mode == "human",
        TaskDB.assignments_completed < TaskDB.assignments_required,
        ~TaskDB.id.in_(
            select(TaskAssignmentDB.task_id).where(
                TaskAssignmentDB.worker_id == user_id,
                TaskAssignmentDB.status.in_(["active", "submitted", "approved"]),
            )
        ),
        TaskDB.user_id != user_id,
        (TaskDB.min_reputation_score == None) | (TaskDB.min_reputation_score <= worker_rep),  # noqa: E711
    ).order_by(TaskDB.created_at.asc()).limit(pool_size)

    pool_tasks = list((await db.execute(q)).scalars().all())

    # Score and rank
    ranked = await rank_tasks_for_worker(db, worker=user, tasks=pool_tasks)

    total = len(ranked)
    offset = (page - 1) * page_size
    page_items = ranked[offset: offset + page_size]

    items = [
        MarketplaceTaskOut(
            id=t.id,
            type=t.type,
            priority=t.priority,
            reward_credits=t.worker_reward_credits or 2,
            estimated_minutes=TASK_ESTIMATED_MINUTES.get(t.type, 3),
            assignments_required=t.assignments_required,
            assignments_completed=t.assignments_completed,
            slots_available=t.assignments_required - t.assignments_completed,
            task_instructions=t.task_instructions,
            created_at=t.created_at,
            match_score=round(score, 3),
            min_skill_level=t.min_skill_level,
        )
        for t, score in page_items
    ]

    return PaginatedMarketplaceTasks(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_next=(offset + page_size) < total,
    )


# ─── Marketplace ──────────────────────────────────────────────────────────

@router.get("/tasks", response_model=PaginatedMarketplaceTasks)
async def list_marketplace_tasks(
    type: Optional[str] = Query(None, description="Filter by task type"),
    priority: Optional[str] = Query(None),
    min_reward: Optional[int] = Query(None, ge=1),
    sort_by: Optional[str] = Query(
        None,
        description="Sort order: 'reward_desc' (highest reward first), "
                    "'newest' (recently posted first), "
                    "or omit for default priority+age ordering",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Browse open human tasks available for workers to claim."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    if user.is_banned:
        raise HTTPException(status_code=403, detail="Your worker account has been suspended.")

    worker_rep = user.reputation_score or 0.0

    # Base query: open human tasks where there are still slots available
    q = select(TaskDB).where(
        TaskDB.status == "open",
        TaskDB.execution_mode == "human",
        # Task must still need more workers
        TaskDB.assignments_completed < TaskDB.assignments_required,
        # Worker must not have already claimed/submitted this task
        ~TaskDB.id.in_(
            select(TaskAssignmentDB.task_id).where(
                TaskAssignmentDB.worker_id == user_id,
                TaskAssignmentDB.status.in_(["active", "submitted", "approved"]),
            )
        ),
        # Don't show requester their own tasks
        TaskDB.user_id != user_id,
        # Only show tasks worker has enough reputation to claim
        (TaskDB.min_reputation_score == None) | (TaskDB.min_reputation_score <= worker_rep),  # noqa: E711
    )

    if type:
        q = q.where(TaskDB.type == type)
    if priority:
        q = q.where(TaskDB.priority == priority)
    if min_reward is not None:
        q = q.where(TaskDB.worker_reward_credits >= min_reward)

    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0

    if sort_by == "reward_desc":
        q = q.order_by(TaskDB.worker_reward_credits.desc().nullslast(), TaskDB.priority.desc(), TaskDB.created_at.asc())
    elif sort_by == "newest":
        q = q.order_by(TaskDB.created_at.desc())
    else:
        # Default: urgency (priority desc) then age (oldest open tasks first)
        q = q.order_by(TaskDB.priority.desc(), TaskDB.created_at.asc())

    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    tasks = result.scalars().all()

    items = [
        MarketplaceTaskOut(
            id=t.id,
            type=t.type,
            priority=t.priority,
            reward_credits=t.worker_reward_credits or 2,
            estimated_minutes=TASK_ESTIMATED_MINUTES.get(t.type, 3),
            assignments_required=t.assignments_required,
            assignments_completed=t.assignments_completed,
            slots_available=t.assignments_required - t.assignments_completed,
            task_instructions=t.task_instructions,
            created_at=t.created_at,
            min_skill_level=t.min_skill_level,
        )
        for t in tasks
    ]

    return PaginatedMarketplaceTasks(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_next=(page * page_size) < total,
    )


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_marketplace_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get full details of a marketplace task (to preview before claiming)."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    result = await db.execute(
        select(TaskDB).where(
            TaskDB.id == task_id,
            TaskDB.execution_mode == "human",
        )
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskOut.model_validate(task)


# ─── Claim a task ─────────────────────────────────────────────────────────

@router.post("/tasks/{task_id}/claim", response_model=WorkerTaskClaimResponse)
async def claim_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Claim a task from the marketplace. Locks the task for you."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    # Ban check
    if user.is_banned:
        raise HTTPException(status_code=403, detail="Your worker account has been suspended.")

    # with_for_update() acquires a row-level lock on the task row for the
    # duration of this transaction. This serialises concurrent claim requests
    # so that two workers can't both pass the slot-availability check at the
    # same time and over-assign a task.
    result = await db.execute(
        select(TaskDB).where(
            TaskDB.id == task_id,
            TaskDB.execution_mode == "human",
            TaskDB.status == "open",
        ).with_for_update()
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or not available")

    # Application mode check — block direct claiming
    if task.application_mode:
        raise HTTPException(
            status_code=400,
            detail="This task requires an application. Please submit a proposal instead.",
        )

    # Team assignment check — only team members can claim
    if task.assigned_team_id is not None:
        from models.db import WorkerTeamMemberDB
        membership = await db.scalar(
            select(func.count()).where(
                WorkerTeamMemberDB.team_id == task.assigned_team_id,
                WorkerTeamMemberDB.user_id == user.id,
            )
        )
        if not membership:
            raise HTTPException(
                status_code=403,
                detail="This task is reserved for a specific worker team. You must be a team member to claim it.",
            )

    # Reputation gate check
    if task.min_reputation_score is not None:
        if (user.reputation_score or 0.0) < task.min_reputation_score:
            raise HTTPException(
                status_code=403,
                detail=f"This task requires a reputation score of {task.min_reputation_score:.0f}+. "
                       f"Your current score is {user.reputation_score:.0f}.",
            )

    # Skill level gate check
    if task.min_skill_level is not None:
        from models.db import WorkerSkillDB
        skill_res = await db.execute(
            select(WorkerSkillDB).where(
                WorkerSkillDB.worker_id == user_id,
                WorkerSkillDB.task_type == task.type,
            )
        )
        skill = skill_res.scalar_one_or_none()
        worker_prof = skill.proficiency_level if skill else 1
        if worker_prof < task.min_skill_level:
            raise HTTPException(
                status_code=403,
                detail=f"This task requires proficiency level {task.min_skill_level}+ "
                       f"in {task.type}. Your level: {worker_prof}.",
            )

    # Check worker hasn't already claimed this task
    existing = await db.scalar(
        select(func.count()).where(
            TaskAssignmentDB.task_id == task_id,
            TaskAssignmentDB.worker_id == user_id,
            TaskAssignmentDB.status.in_(["active", "submitted", "approved"]),
        )
    )
    if existing and existing > 0:
        raise HTTPException(status_code=409, detail="You already have this task claimed")

    # Check slots still available
    if task.assignments_completed >= task.assignments_required:
        raise HTTPException(status_code=409, detail="No more slots available for this task")

    # Check how many active assignments exist (claimed but not submitted)
    active_assignments = await db.scalar(
        select(func.count()).where(
            TaskAssignmentDB.task_id == task_id,
            TaskAssignmentDB.status == "active",
        )
    ) or 0

    slots_in_progress = task.assignments_completed + active_assignments
    if slots_in_progress >= task.assignments_required:
        raise HTTPException(status_code=409, detail="All slots currently claimed. Try again later.")

    # Check worker doesn't have too many active assignments (cap at 5)
    worker_active = await db.scalar(
        select(func.count()).where(
            TaskAssignmentDB.worker_id == user_id,
            TaskAssignmentDB.status == "active",
        )
    ) or 0
    if worker_active >= 5:
        raise HTTPException(
            status_code=429,
            detail="You have 5 active tasks. Submit or release some before claiming more.",
        )

    timeout_at = datetime.now(timezone.utc) + timedelta(minutes=task.claim_timeout_minutes)

    assignment = TaskAssignmentDB(
        id=uuid4(),
        task_id=task.id,
        worker_id=user_id,
        status="active",
        timeout_at=timeout_at,
        earnings_credits=task.worker_reward_credits or 2,
    )
    db.add(assignment)

    # If this fills all slots, mark task as assigned
    if (task.assignments_completed + active_assignments + 1) >= task.assignments_required:
        task.status = "assigned"

    await db.commit()
    await db.refresh(assignment)

    logger.info("task_claimed", task_id=str(task_id), worker_id=user_id)

    # Fire task.assigned webhook to the task owner (per-task + persistent endpoints)
    _wh_assign_extra = {"type": task.type, "worker_id": str(user_id),
                        "assignment_id": str(assignment.id)}
    if task.webhook_url:
        safe_create_task(fire_webhook_for_task(
            task=task,
            event_type="task.assigned",
            extra=_wh_assign_extra,
        ))
    safe_create_task(fire_persistent_endpoints(
        user_id=str(task.user_id),
        task_id=str(task.id),
        event_type="task.assigned",
        extra=_wh_assign_extra,
    ))

    # ── Onboarding: mark explore step on first claim ───────────────────────
    try:
        from routers.onboarding import mark_onboarding_step
        import uuid as _uuid_onboard
        await mark_onboarding_step(_uuid_onboard.UUID(user_id), "explore", db)
        await db.flush()
    except Exception:
        logger.warning("claim_task.onboarding_step_failed", user_id=user_id)

    return WorkerTaskClaimResponse(
        assignment_id=assignment.id,
        task_id=task.id,
        timeout_at=timeout_at,
    )


# ─── Submit a task ────────────────────────────────────────────────────────

@router.post("/tasks/{task_id}/submit", response_model=WorkerTaskSubmitResponse)
async def submit_task(
    task_id: UUID,
    req: WorkerTaskSubmitRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Submit your completion of a claimed task."""
    result = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.task_id == task_id,
            TaskAssignmentDB.worker_id == user_id,
            TaskAssignmentDB.status == "active",
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="No active assignment found for this task")

    now = datetime.now(timezone.utc)

    # Check timeout
    if assignment.timeout_at and now > assignment.timeout_at:
        assignment.status = "timed_out"
        await db.commit()
        raise HTTPException(status_code=410, detail="Assignment expired. Claim the task again if slots are available.")

    # Record submission
    assignment.status = "submitted"
    assignment.response = req.response
    assignment.worker_note = req.worker_note
    assignment.submitted_at = now

    # Placeholder XP — will be recomputed with streak once worker is loaded
    task_type_for_xp = "label_image"  # fallback
    xp = 0

    # Track whether this submission completes a pipeline step
    _pipeline_task_completed: Optional[tuple] = None  # (task_id, output)

    # Load task to get type
    task_result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_result.scalar_one_or_none()
    if task:
        task_type_for_xp = task.type
        task.assignments_completed += 1

        # Determine task lifecycle based on consensus strategy
        if task.assignments_completed >= task.assignments_required:
            if task.consensus_strategy == "any_first":
                # any_first: first submission that fills the slot wins → auto-complete
                task.status = "completed"
                task.completed_at = now
                task.winning_assignment_id = assignment.id
                task.output = req.response
                _pipeline_task_completed = (task.id, req.response)  # trigger pipeline resume
            else:
                # For majority_vote / unanimous / requester_review:
                # All assignments are in — run consensus check below.
                # check_and_apply_consensus will set status and output.
                pass  # handled after commit via check_and_apply_consensus
        elif task.status == "assigned":
            # Reopen for more workers if needed
            task.status = "open"

    # Update worker stats
    applied_xp_multiplier: float = 1.0
    worker_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    worker = worker_result.scalar_one_or_none()
    if worker:
        # Compute XP with streak multiplier using the streak BEFORE today's update
        xp = compute_xp_for_task(task_type_for_xp, streak_days=worker.worker_streak_days)
        applied_xp_multiplier = streak_xp_multiplier(worker.worker_streak_days)

        worker.worker_xp += xp
        worker.worker_tasks_completed += 1
        new_level, _ = compute_level(worker.worker_xp)
        worker.worker_level = new_level

        # Update streak
        today = now.date()
        if worker.worker_last_active_date:
            last_date = worker.worker_last_active_date.date()
            days_since = (today - last_date).days
            if days_since == 1:
                worker.worker_streak_days += 1
            elif days_since > 1:
                worker.worker_streak_days = 1  # Streak broken
            # days_since == 0: same day, no change to streak
        else:
            worker.worker_streak_days = 1
        worker.worker_last_active_date = now
    else:
        xp = compute_xp_for_task(task_type_for_xp)

    assignment.xp_earned = xp

    if worker:
        # Compute reliability: ratio of submitted/(submitted+released+timed_out)
        completed = worker.worker_tasks_completed
        released = await db.scalar(
            select(func.count()).where(
                TaskAssignmentDB.worker_id == user_id,
                TaskAssignmentDB.status.in_(["released", "timed_out"]),
            )
        ) or 0
        total_attempts = completed + released
        if total_attempts > 0:
            worker.worker_reliability = completed / total_attempts

    # Credit earnings to worker
    earnings = assignment.earnings_credits
    if worker:
        worker.credits += earnings

    txn = CreditTransactionDB(
        user_id=user_id,
        task_id=task_id,
        amount=earnings,
        type="earning",
        description=f"Task completion: {task.type if task else 'unknown'}",
    )
    db.add(txn)

    # Update worker skill profile (outcome = "completed" — will be upgraded to approved/rejected later)
    if task:
        try:
            from routers.skills import update_worker_skill
            resp_minutes: float | None = None
            if assignment.submitted_at and assignment.claimed_at:
                delta = assignment.submitted_at - assignment.claimed_at
                resp_minutes = delta.total_seconds() / 60
            await update_worker_skill(
                db,
                worker_id=uuid_mod.UUID(user_id),
                task_type=task.type,
                outcome="completed",
                response_minutes=resp_minutes,
                credits_earned=earnings,
            )
        except Exception:
            logger.warning("submit_task.skill_update_failed", user_id=user_id, task_id=str(task_id))

    # ── Daily challenge progress ───────────────────────────────────────────
    if task and worker:
        today = now.date()
        challenge_result = await db.execute(
            select(DailyChallengeDB).where(DailyChallengeDB.challenge_date == today)
        )
        daily = challenge_result.scalar_one_or_none()
        if daily and task.type == daily.task_type:
            # Find or create progress record
            prog_result = await db.execute(
                select(DailyChallengeProgressDB).where(
                    DailyChallengeProgressDB.user_id == user_id,
                    DailyChallengeProgressDB.challenge_id == daily.id,
                )
            )
            progress = prog_result.scalar_one_or_none()
            if progress is None:
                from uuid import uuid4 as _uuid4
                progress = DailyChallengeProgressDB(
                    id=_uuid4(),
                    user_id=user_id,
                    challenge_id=daily.id,
                    tasks_completed=0,
                    bonus_claimed=False,
                )
                db.add(progress)
            progress.tasks_completed += 1

    await db.commit()

    # ── Award badges (after commit so stats are accurate) ─────────────────
    new_badge_ids: list[str] = []
    if worker:
        try:
            from routers.badges import award_new_badges
            # Count challenge completions for badge check
            chall_count_result = await db.execute(
                select(DailyChallengeProgressDB).where(
                    DailyChallengeProgressDB.user_id == user_id,
                    DailyChallengeProgressDB.bonus_claimed == True,
                )
            )
            chall_count = len(chall_count_result.scalars().all())
            new_badge_ids = await award_new_badges(worker, db, challenge_completions=chall_count)
            if new_badge_ids:
                # In-app notification per badge
                try:
                    from core.notify import create_notification, NotifType
                    for bid in new_badge_ids:
                        badge_label = bid.replace("_", " ").title()
                        await create_notification(
                            db, user_id,
                            NotifType.BADGE_EARNED,
                            f"Badge unlocked: {badge_label} 🏅",
                            f"You earned the '{badge_label}' badge. Keep it up!",
                            link="/worker/achievements",
                        )
                except Exception:
                    logger.warning("submit_task.badge_notification_failed", user_id=user_id)
                await db.commit()
        except Exception:
            logger.warning("submit_task.badge_award_failed", user_id=user_id, task_id=str(task_id))

    logger.info(
        "task_submitted",
        task_id=str(task_id),
        worker_id=user_id,
        xp=xp,
        earnings=earnings,
        new_badges=new_badge_ids,
    )

    # ── Notify the task requester about the new submission ────────────────
    if task:
        try:
            requester_result = await db.execute(select(UserDB).where(UserDB.id == task.user_id))
            requester = requester_result.scalar_one_or_none()
            worker_display = worker.name or worker.email if worker else "A worker"

            # In-app notification to requester
            if requester and str(requester.id) != user_id:
                from core.notify import create_notification, NotifType
                await create_notification(
                    db, task.user_id,
                    NotifType.SUBMISSION_RECEIVED,
                    "New submission received 📬",
                    f"{worker_display} submitted a {task.type.replace('_', ' ')} response. Review it now.",
                    link=f"/dashboard/tasks/{task_id}",
                )
                await db.commit()

            # Email notification to requester
            if requester and requester.email and str(requester.id) != user_id:
                from core.email import notify_submission_received
                import asyncio as _asyncio
                safe_create_task(notify_submission_received(
                    requester.email,
                    str(task_id),
                    task.type,
                    worker_name=worker_display,
                ))

            # Webhook: task.submission_received → requester (per-task + persistent endpoints)
            _wh_sub_extra = {"type": task.type, "worker_id": str(user_id),
                             "assignment_id": str(assignment.id)}
            if task.webhook_url:
                safe_create_task(fire_webhook_for_task(
                    task=task,
                    event_type="task.submission_received",
                    extra=_wh_sub_extra,
                ))
            safe_create_task(fire_persistent_endpoints(
                user_id=str(task.user_id),
                task_id=str(task.id),
                event_type="task.submission_received",
                extra=_wh_sub_extra,
            ))
        except Exception:
            logger.warning("submit_task.webhook_notification_failed", task_id=str(task_id))

    # ── Pay referral bonus on first task completion ───────────────────────
    if worker and worker.worker_tasks_completed == 1:
        try:
            from routers.referrals import pay_referral_bonus_on_first_task
            await pay_referral_bonus_on_first_task(user_id, db)
        except Exception:
            logger.warning("submit_task.referral_bonus_failed", user_id=user_id)

    # ── Onboarding: mark first_task step ──────────────────────────────────
    try:
        from routers.onboarding import mark_onboarding_step
        import uuid as _uuid_mod2
        await mark_onboarding_step(_uuid_mod2.UUID(user_id), "first_task", db)
        await db.flush()
    except Exception:
        logger.warning("submit_task.onboarding_step_failed", user_id=user_id)

    # ── Consensus check (for non-any_first strategies) ────────────────────
    if task and task.consensus_strategy != "any_first":
        if task.assignments_completed >= task.assignments_required:
            try:
                from routers.disputes import check_and_apply_consensus
                await check_and_apply_consensus(task, db)
                await db.commit()
                # After consensus, check if task is now completed to resume pipeline
                if task.status == "completed" and task.output is not None:
                    _pipeline_task_completed = (task.id, task.output)
            except Exception:
                logger.exception("consensus_check_failed", task_id=str(task_id))

    # ── Pipeline Resumption ───────────────────────────────────────────────
    if _pipeline_task_completed:
        try:
            from routers.pipelines import resume_pipeline_after_human_step
            ptask_id, poutput = _pipeline_task_completed
            await resume_pipeline_after_human_step(ptask_id, poutput, db)
        except Exception:
            logger.exception("pipeline_resume_failed", task_id=str(task_id))

    # ── Refresh reputation score ──────────────────────────────────────────
    try:
        import uuid as _uuid_mod
        await refresh_worker_reputation(_uuid_mod.UUID(user_id), db)
        await db.commit()
    except Exception:
        logger.warning("submit_task.reputation_refresh_failed", user_id=user_id)

    msg = f"Submitted! You earned {earnings} credits and {xp} XP."
    if new_badge_ids:
        msg += f" 🏆 New badge(s) earned: {', '.join(new_badge_ids)}"

    return WorkerTaskSubmitResponse(
        assignment_id=assignment.id,
        status="submitted",
        earnings_credits=earnings,
        xp_earned=xp,
        streak_multiplier=applied_xp_multiplier,
        streak_days=worker.worker_streak_days if worker else 0,
        message=msg,
    )


# ─── Release a task ───────────────────────────────────────────────────────

@router.delete("/tasks/{task_id}/release", status_code=204, response_model=None)
async def release_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Give up a claimed task. Returns it to the marketplace."""
    result = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.task_id == task_id,
            TaskAssignmentDB.worker_id == user_id,
            TaskAssignmentDB.status == "active",
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="No active assignment found")

    now = datetime.now(timezone.utc)
    assignment.status = "released"
    assignment.released_at = now

    # Put task back to open if it was assigned
    task_result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_result.scalar_one_or_none()
    if task and task.status == "assigned":
        task.status = "open"

    # Update reliability
    worker_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    worker = worker_result.scalar_one_or_none()
    if worker:
        completed = worker.worker_tasks_completed
        released = await db.scalar(
            select(func.count()).where(
                TaskAssignmentDB.worker_id == user_id,
                TaskAssignmentDB.status.in_(["released", "timed_out"]),
            )
        ) or 0
        # +1 for this release (not yet committed)
        total_attempts = completed + released + 1
        if total_attempts > 0:
            worker.worker_reliability = completed / total_attempts

    await db.commit()
    logger.info("task_released", task_id=str(task_id), worker_id=user_id)


# ─── Earnings analytics ───────────────────────────────────────────────────

@router.get("/earnings/analytics")
async def get_earnings_analytics(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return rich earnings analytics for the current worker:
    - by_task_type: credits earned per task type (all-time)
    - monthly_earnings: last 12 calendar months of credits earned
    - monthly_payouts: last 12 calendar months of paid-out USD
    - lifetime stats: total earned, total paid out, avg credits per task, best month
    """
    from models.db import PayoutRequestDB

    uid = UUID(user_id)

    # 1. Earnings by task type (join assignments → tasks)
    type_res = await db.execute(
        select(TaskDB.type, func.sum(TaskAssignmentDB.earnings_credits).label("credits"))
        .join(TaskDB, TaskDB.id == TaskAssignmentDB.task_id)
        .where(
            TaskAssignmentDB.worker_id == uid,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
        )
        .group_by(TaskDB.type)
        .order_by(func.sum(TaskAssignmentDB.earnings_credits).desc())
    )
    by_task_type = {row[0]: int(row[1] or 0) for row in type_res.all()}

    # 2. Monthly earnings (last 12 months)
    from sqlalchemy import extract
    monthly_res = await db.execute(
        select(
            extract("year", TaskAssignmentDB.submitted_at).label("yr"),
            extract("month", TaskAssignmentDB.submitted_at).label("mo"),
            func.sum(TaskAssignmentDB.earnings_credits).label("credits"),
        )
        .where(
            TaskAssignmentDB.worker_id == uid,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
            TaskAssignmentDB.submitted_at.isnot(None),
        )
        .group_by("yr", "mo")
        .order_by("yr", "mo")
    )
    raw_monthly = monthly_res.all()
    # Fill last 12 months
    now = datetime.now(timezone.utc)
    monthly_earnings: list[dict] = []
    for i in range(11, -1, -1):
        # Step back i months from current month
        mo_offset = (now.month - 1 - i) % 12 + 1
        yr_offset = now.year + ((now.month - 1 - i) // 12)
        credits = 0
        for row in raw_monthly:
            if int(row[0]) == yr_offset and int(row[1]) == mo_offset:
                credits = int(row[2] or 0)
                break
        monthly_earnings.append({"month": f"{yr_offset}-{mo_offset:02d}", "credits": credits})

    # 3. Monthly paid-out USD (last 12 months, status=paid)
    payout_res = await db.execute(
        select(
            extract("year", PayoutRequestDB.processed_at).label("yr"),
            extract("month", PayoutRequestDB.processed_at).label("mo"),
            func.sum(PayoutRequestDB.usd_amount).label("usd"),
        )
        .where(
            PayoutRequestDB.worker_id == uid,
            PayoutRequestDB.status == "paid",
            PayoutRequestDB.processed_at.isnot(None),
        )
        .group_by("yr", "mo")
        .order_by("yr", "mo")
    )
    raw_payouts = payout_res.all()
    monthly_payouts: list[dict] = []
    for i in range(11, -1, -1):
        mo_offset = (now.month - 1 - i) % 12 + 1
        yr_offset = now.year + ((now.month - 1 - i) // 12)
        usd = 0.0
        for row in raw_payouts:
            if int(row[0]) == yr_offset and int(row[1]) == mo_offset:
                usd = float(row[2] or 0)
                break
        monthly_payouts.append({"month": f"{yr_offset}-{mo_offset:02d}", "usd": round(usd, 2)})

    # 4. Lifetime stats
    lifetime_res = await db.execute(
        select(
            func.sum(TaskAssignmentDB.earnings_credits),
            func.count(TaskAssignmentDB.id),
        )
        .where(
            TaskAssignmentDB.worker_id == uid,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
        )
    )
    lt_row = lifetime_res.one()
    lifetime_credits = int(lt_row[0] or 0)
    lifetime_tasks = int(lt_row[1] or 0)
    avg_credits_per_task = round(lifetime_credits / lifetime_tasks, 1) if lifetime_tasks > 0 else 0

    payout_total_res = await db.execute(
        select(
            func.sum(PayoutRequestDB.usd_amount),
            func.count(PayoutRequestDB.id),
        )
        .where(
            PayoutRequestDB.worker_id == uid,
            PayoutRequestDB.status == "paid",
        )
    )
    pt_row = payout_total_res.one()
    lifetime_paid_out_usd = round(float(pt_row[0] or 0), 2)
    total_payouts = int(pt_row[1] or 0)

    best_month_credits = max((m["credits"] for m in monthly_earnings), default=0)

    # ── 5. Weekly earnings (last 12 ISO weeks) ────────────────────────────────
    from collections import defaultdict
    twelve_weeks_ago = now - timedelta(weeks=12)
    weekly_raw = await db.execute(
        select(
            TaskAssignmentDB.submitted_at,
            TaskAssignmentDB.earnings_credits,
        )
        .where(
            TaskAssignmentDB.worker_id == uid,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
            TaskAssignmentDB.submitted_at.isnot(None),
            TaskAssignmentDB.submitted_at >= twelve_weeks_ago,
        )
        .order_by(TaskAssignmentDB.submitted_at)
    )
    # Bucket by ISO week key "YYYY-Www"
    week_buckets: dict = defaultdict(lambda: {"credits": 0, "tasks": 0})
    for row_at, row_credits in weekly_raw.all():
        if row_at:
            iso = row_at.isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
            week_buckets[key]["credits"] += int(row_credits or 0)
            week_buckets[key]["tasks"] += 1

    weekly_earnings: list[dict] = []
    for i in range(11, -1, -1):
        target = now - timedelta(weeks=i)
        iso = target.isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        bucket = week_buckets.get(key, {"credits": 0, "tasks": 0})
        weekly_earnings.append({
            "week": key,
            "credits": bucket["credits"],
            "tasks": bucket["tasks"],
            "is_current": i == 0,
        })

    # ── 6. Projected earnings (linear trend of last 3 months) ─────────────────
    # Uses the last 3 calendar months to compute a simple slope, then extrapolates
    # next month's expected credits. Weekly = monthly / 4.33.
    recent = [m["credits"] for m in monthly_earnings[-3:]]  # last 3 months
    if len(recent) >= 2 and any(c > 0 for c in recent):
        # Simple least-squares slope over the window [0, 1, ..., n-1]
        n = len(recent)
        xs = list(range(n))
        mean_x = sum(xs) / n
        mean_y = sum(recent) / n
        num = sum((xs[i] - mean_x) * (recent[i] - mean_y) for i in range(n))
        den = sum((xs[i] - mean_x) ** 2 for i in range(n))
        slope = num / den if den != 0 else 0
        projected_next_month_credits = max(0, int(recent[-1] + slope))
    else:
        projected_next_month_credits = 0

    projected_next_week_credits = int(projected_next_month_credits / 4.33)
    projected_next_month_usd = round(projected_next_month_credits / 100, 2)
    projected_next_week_usd = round(projected_next_week_credits / 100, 2)
    # Trend direction: "up", "down", or "flat"
    if len(recent) >= 3 and recent[-1] > 0 and recent[0] > 0:
        pct_change = (recent[-1] - recent[0]) / max(recent[0], 1) * 100
        trend_direction = "up" if pct_change > 5 else ("down" if pct_change < -5 else "flat")
    else:
        trend_direction = "flat"

    return {
        "by_task_type": by_task_type,
        "weekly_earnings": weekly_earnings,
        "monthly_earnings": monthly_earnings,
        "monthly_payouts": monthly_payouts,
        "lifetime_credits_earned": lifetime_credits,
        "lifetime_usd_earned": round(lifetime_credits / 100, 2),
        "lifetime_usd_paid_out": lifetime_paid_out_usd,
        "total_payouts_completed": total_payouts,
        "total_tasks_completed": lifetime_tasks,
        "avg_credits_per_task": avg_credits_per_task,
        "best_month_credits": best_month_credits,
        # Projection fields
        "projected_next_month_credits": projected_next_month_credits,
        "projected_next_month_usd": projected_next_month_usd,
        "projected_next_week_credits": projected_next_week_credits,
        "projected_next_week_usd": projected_next_week_usd,
        "earnings_trend": trend_direction,  # "up" | "down" | "flat"
    }


# ─── My assignments ───────────────────────────────────────────────────────

@router.get("/assignments", response_model=list[TaskAssignmentOut])
async def list_my_assignments(
    status: Optional[str] = Query(None, description="Filter: active, submitted, approved, released, timed_out"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List all my task assignments."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    q = select(TaskAssignmentDB).where(TaskAssignmentDB.worker_id == user_id)
    if status:
        q = q.where(TaskAssignmentDB.status == status)
    q = q.order_by(TaskAssignmentDB.claimed_at.desc()).offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(q)
    assignments = result.scalars().all()
    return [TaskAssignmentOut.model_validate(a) for a in assignments]


# ─── Recent activity (completions with task type) ─────────────────────────

@router.get("/activity")
async def recent_worker_activity(
    page_size: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Recent submitted/approved assignments with embedded task type for the home page feed."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    stmt = (
        select(TaskAssignmentDB, TaskDB.type.label("task_type"))
        .join(TaskDB, TaskAssignmentDB.task_id == TaskDB.id)
        .where(
            TaskAssignmentDB.worker_id == user_id,
            TaskAssignmentDB.status.in_(["submitted", "approved", "released"]),
        )
        .order_by(TaskAssignmentDB.submitted_at.desc())
        .limit(page_size)
    )
    result = await db.execute(stmt)
    rows = result.all()

    return [
        {
            "task_id": str(a.task_id),
            "assignment_id": str(a.id),
            "task_type": task_type,
            "status": a.status,
            "earnings_credits": a.earnings_credits,
            "xp_earned": a.xp_earned,
            "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
        }
        for a, task_type in rows
    ]


# ─── Skill recommendations ─────────────────────────────────────────────────

# Human-readable labels and categories for all task types
_TASK_META: dict[str, dict] = {
    # AI tasks
    "web_research":       {"label": "Web Research",      "category": "ai",    "worker_reward": 5},
    "entity_lookup":      {"label": "Entity Lookup",     "category": "ai",    "worker_reward": 3},
    "document_parse":     {"label": "Document Parse",    "category": "ai",    "worker_reward": 2},
    "data_transform":     {"label": "Data Transform",    "category": "ai",    "worker_reward": 1},
    "llm_generate":       {"label": "LLM Generate",      "category": "ai",    "worker_reward": 1},
    "screenshot":         {"label": "Screenshot",        "category": "ai",    "worker_reward": 1},
    "audio_transcribe":   {"label": "Audio Transcribe",  "category": "ai",    "worker_reward": 4},
    "pii_detect":         {"label": "PII Detection",     "category": "ai",    "worker_reward": 1},
    "code_execute":       {"label": "Code Execute",      "category": "ai",    "worker_reward": 2},
    "web_intel":          {"label": "Web Intelligence",  "category": "ai",    "worker_reward": 3},
    # Human tasks
    "label_image":        {"label": "Label Image",       "category": "human", "worker_reward": 3},
    "label_text":         {"label": "Label Text",        "category": "human", "worker_reward": 2},
    "rate_quality":       {"label": "Rate Quality",      "category": "human", "worker_reward": 2},
    "verify_fact":        {"label": "Verify Fact",       "category": "human", "worker_reward": 2},
    "moderate_content":   {"label": "Moderate Content",  "category": "human", "worker_reward": 2},
    "compare_rank":       {"label": "Compare & Rank",    "category": "human", "worker_reward": 2},
    "answer_question":    {"label": "Answer Question",   "category": "human", "worker_reward": 3},
    "transcription_review": {"label": "Transcription Review", "category": "human", "worker_reward": 4},
}

# Proficiency thresholds: tasks_completed required to advance to level 1..5
_PROFICIENCY_THRESHOLDS = [0, 5, 15, 30, 60]  # tasks to reach level 1, 2, 3, 4, 5

# Verification requires proficiency ≥4 AND ≥15 approved tasks AND accuracy ≥90%
_VERIFICATION_MIN_APPROVED = 15
_VERIFICATION_MIN_ACCURACY = 0.90
_VERIFICATION_MIN_LEVEL = 4

# Assume worker does ~20 tasks/day × 5 working days = 100 tasks/week
_TASKS_PER_WEEK = 100

CREDITS_PER_USD = 100


def _next_proficiency_tasks(completed: int) -> int:
    """Tasks needed to reach the next proficiency level; 0 if maxed at 5."""
    for threshold in _PROFICIENCY_THRESHOLDS[1:]:  # levels 1→5
        if completed < threshold:
            return threshold - completed
    return 0  # already at level 5


def _tasks_to_verification(approved: int, accuracy: Optional[float], level: int) -> int:
    """Approved tasks still needed for skill verification (0 if already eligible/verified)."""
    if level < _VERIFICATION_MIN_LEVEL:
        return 0  # not eligible until level 4
    if accuracy is not None and accuracy < _VERIFICATION_MIN_ACCURACY:
        return 0  # accuracy too low — can't verify via task count alone
    needed = max(0, _VERIFICATION_MIN_APPROVED - approved)
    return needed


@router.get("/recommendations", response_model=WorkerRecommendationsOut)
async def get_worker_recommendations(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
) -> WorkerRecommendationsOut:
    """
    Personalised task-type recommendations based on the worker's skill history.

    Returns:
    - best_types: up to 5 task types ranked by earnings potential
    - try_next: up to 3 untried types likely to suit them
    - weekly earnings potential if focused on best types
    - actionable insights (level progress, verification proximity, etc.)
    """
    from models.db import WorkerSkillDB, CreditTransactionDB

    uid = UUID(user_id)

    # Verify worker
    user_res = await db.execute(select(UserDB).where(UserDB.id == uid))
    user = user_res.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    # Load all skill records for this worker
    skill_res = await db.execute(
        select(WorkerSkillDB).where(WorkerSkillDB.worker_id == uid)
    )
    skills: list[WorkerSkillDB] = list(skill_res.scalars().all())
    skills_by_type: dict[str, WorkerSkillDB] = {s.task_type: s for s in skills}

    # Credits earned in last 7 days
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    weekly_res = await db.execute(
        select(func.coalesce(func.sum(CreditTransactionDB.amount), 0))
        .where(
            CreditTransactionDB.user_id == uid,
            CreditTransactionDB.type == "earning",
            CreditTransactionDB.created_at >= week_ago,
        )
    )
    current_weekly_rate: int = int(weekly_res.scalar_one() or 0)

    # ── Build best_types: all tried types sorted by earnings potential ────────
    best_types: list[TaskTypeRecommendation] = []
    insights: list[str] = []

    for task_type, meta in _TASK_META.items():
        skill = skills_by_type.get(task_type)
        if skill is None or skill.tasks_completed == 0:
            continue  # only tried types here

        completed = skill.tasks_completed
        approved = skill.tasks_approved
        acc = skill.accuracy or 0.0
        credits_earned = skill.credits_earned
        avg_credits = credits_earned / max(1, completed)
        # Earnings potential = credits/task × acceptance rate
        weekly_credits = int(_TASKS_PER_WEEK * avg_credits * acc)
        weekly_usd = round(weekly_credits / CREDITS_PER_USD, 2)

        next_level_needed = _next_proficiency_tasks(completed)
        to_verification = (
            _tasks_to_verification(approved, skill.accuracy, skill.proficiency_level)
            if not skill.verified else 0
        )

        # Build reason string
        if skill.proficiency_level >= 4:
            reason = f"Level {skill.proficiency_level} proficiency — keep earning"
        elif acc >= 0.85:
            reason = f"{int(acc * 100)}% acceptance rate — strong performer"
        else:
            reason = f"{completed} tasks completed — building experience"

        best_types.append(TaskTypeRecommendation(
            task_type=task_type,
            label=meta["label"],
            category=meta["category"],
            reason=reason,
            proficiency_level=skill.proficiency_level,
            accuracy=skill.accuracy,
            tasks_completed=completed,
            avg_credits_per_task=round(avg_credits, 2),
            estimated_weekly_credits=weekly_credits,
            estimated_weekly_usd=weekly_usd,
            is_verified=skill.verified,
            next_level_tasks_needed=next_level_needed,
            tasks_to_verification=to_verification,
        ))

        # Build insights
        if next_level_needed > 0 and next_level_needed <= 5:
            insights.append(
                f"Only {next_level_needed} more {meta['label']} task"
                f"{'s' if next_level_needed > 1 else ''} to reach proficiency level "
                f"{min(skill.proficiency_level + 1, 5)}"
            )
        if to_verification > 0 and to_verification <= 5:
            insights.append(
                f"{to_verification} more approved {meta['label']} task"
                f"{'s' if to_verification > 1 else ''} until you earn the verified badge"
            )
        if skill.accuracy is not None and skill.accuracy < 0.70 and completed >= 5:
            insights.append(
                f"Your {meta['label']} acceptance rate is {int(skill.accuracy * 100)}% — "
                "review the task guidelines to improve"
            )

    # Sort best_types by weekly earnings potential (descending)
    best_types.sort(key=lambda r: r.estimated_weekly_credits, reverse=True)
    best_types = best_types[:5]

    # ── Build try_next: untried types most similar to what they're good at ───
    tried_types = set(skills_by_type.keys())
    # Similarity groups — if good at one, suggest related types
    _similarity_groups = [
        {"label_image", "label_text", "moderate_content"},
        {"verify_fact", "answer_question"},
        {"rate_quality", "compare_rank"},
        {"transcription_review", "audio_transcribe"},
    ]
    # Categories the worker has experience in
    worker_categories = {
        meta["category"]
        for t, meta in _TASK_META.items()
        if t in tried_types
    }

    try_next_types: list[str] = []
    # First pass: related to something they're good at
    for group in _similarity_groups:
        done_in_group = group & tried_types
        not_tried_in_group = group - tried_types
        if done_in_group and not_tried_in_group:
            try_next_types.extend(list(not_tried_in_group))

    # Second pass: any human task if they haven't tried the category
    if "human" not in worker_categories:
        for t in _TASK_META:
            if _TASK_META[t]["category"] == "human" and t not in tried_types:
                try_next_types.append(t)
                break

    # Deduplicate and limit
    seen = set()
    try_next_deduped: list[str] = []
    for t in try_next_types:
        if t not in seen and t not in tried_types:
            seen.add(t)
            try_next_deduped.append(t)
    try_next_deduped = try_next_deduped[:3]

    try_next: list[TaskTypeRecommendation] = []
    for task_type in try_next_deduped:
        meta = _TASK_META[task_type]
        base_reward = meta["worker_reward"]
        try_next.append(TaskTypeRecommendation(
            task_type=task_type,
            label=meta["label"],
            category=meta["category"],
            reason="New to you — similar to tasks you've already done well",
            proficiency_level=0,
            accuracy=None,
            tasks_completed=0,
            avg_credits_per_task=float(base_reward),
            estimated_weekly_credits=int(_TASKS_PER_WEEK * base_reward * 0.80),
            estimated_weekly_usd=round(_TASKS_PER_WEEK * base_reward * 0.80 / CREDITS_PER_USD, 2),
            is_verified=False,
            next_level_tasks_needed=_PROFICIENCY_THRESHOLDS[1],
            tasks_to_verification=0,
        ))

    # ── Weekly earnings potential ─────────────────────────────────────────────
    if best_types:
        # Average of top 3 best types' estimates
        top_n = best_types[:3]
        weekly_potential = sum(r.estimated_weekly_credits for r in top_n) // len(top_n)
    elif try_next:
        weekly_potential = int(_TASKS_PER_WEEK * _TASK_META[try_next[0].task_type]["worker_reward"] * 0.80)
    else:
        weekly_potential = int(_TASKS_PER_WEEK * 2 * 0.80)  # generic fallback

    # Add global insight if no skills yet
    if not best_types and not insights:
        insights.append("Complete your first task to unlock personalised recommendations")
    elif best_types and current_weekly_rate < weekly_potential // 2:
        insights.append(
            f"Focusing on {best_types[0].label} could increase your weekly earnings "
            f"to ~{weekly_potential} credits (${round(weekly_potential / CREDITS_PER_USD, 2)})"
        )

    return WorkerRecommendationsOut(
        best_types=best_types,
        try_next=try_next,
        weekly_earnings_potential=weekly_potential,
        weekly_earnings_potential_usd=round(weekly_potential / CREDITS_PER_USD, 2),
        current_weekly_rate=current_weekly_rate,
        insights=insights[:5],  # cap at 5 insights
    )

