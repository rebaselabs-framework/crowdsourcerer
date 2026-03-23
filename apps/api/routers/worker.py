"""Worker marketplace API — browse tasks, claim, submit, release."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional
import uuid as uuid_mod
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_

from core.auth import get_current_user_id
from core.database import get_db
from models.db import (
    TaskDB, UserDB, TaskAssignmentDB, CreditTransactionDB,
    DailyChallengeDB, DailyChallengeProgressDB,
)
from models.schemas import (
    BecomeWorkerRequest,
    WorkerProfileOut,
    WorkerStatsOut,
    MarketplaceTaskOut,
    PaginatedMarketplaceTasks,
    TaskAssignmentOut,
    TaskAssignmentWithTaskOut,
    TaskOut,
    WorkerTaskClaimResponse,
    WorkerTaskSubmitRequest,
    WorkerTaskSubmitResponse,
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


def compute_xp_for_task(task_type: str, accurate: bool = True) -> int:
    """XP earned for completing a task. Bonus for accuracy."""
    base = TASK_XP_BASE.get(task_type, 10)
    return base if accurate else max(1, base // 2)


# ─── Become a worker ──────────────────────────────────────────────────────

@router.post("/enroll", response_model=WorkerProfileOut)
async def enroll_as_worker(
    req: BecomeWorkerRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Enable worker mode for the current user account."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.role == "requester":
        user.role = "both"
    # If already 'worker' or 'both', no change needed

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
    )


# ─── Marketplace ──────────────────────────────────────────────────────────

@router.get("/tasks", response_model=PaginatedMarketplaceTasks)
async def list_marketplace_tasks(
    type: Optional[str] = Query(None, description="Filter by task type"),
    priority: Optional[str] = Query(None),
    min_reward: Optional[int] = Query(None, ge=1),
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
    )

    if type:
        q = q.where(TaskDB.type == type)
    if priority:
        q = q.where(TaskDB.priority == priority)
    if min_reward is not None:
        q = q.where(TaskDB.worker_reward_credits >= min_reward)

    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0

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

    result = await db.execute(
        select(TaskDB).where(
            TaskDB.id == task_id,
            TaskDB.execution_mode == "human",
            TaskDB.status == "open",
        )
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or not available")

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

    # Compute XP
    xp = compute_xp_for_task(assignment.task.type if hasattr(assignment, "task") else "label_image")

    # Track whether this submission completes a pipeline step
    _pipeline_task_completed: Optional[tuple] = None  # (task_id, output)

    # Load task to get type
    task_result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_result.scalar_one_or_none()
    if task:
        xp = compute_xp_for_task(task.type)
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

    assignment.xp_earned = xp

    # Update worker stats
    worker_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    worker = worker_result.scalar_one_or_none()
    if worker:
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
            # days_since == 0: same day, no change
        else:
            worker.worker_streak_days = 1
        worker.worker_last_active_date = now

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
            pass

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
                    pass
                await db.commit()
        except Exception:
            pass  # Badge errors never block task submission

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
                _asyncio.create_task(notify_submission_received(
                    requester.email,
                    str(task_id),
                    task.type,
                    worker_name=worker_display,
                ))
        except Exception:
            pass  # Notification errors never block submission response

    # ── Pay referral bonus on first task completion ───────────────────────
    if worker and worker.worker_tasks_completed == 1:
        try:
            from routers.referrals import pay_referral_bonus_on_first_task
            await pay_referral_bonus_on_first_task(user_id, db)
        except Exception:
            pass  # Referral errors never block submission

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

    msg = f"Submitted! You earned {earnings} credits and {xp} XP."
    if new_badge_ids:
        msg += f" 🏆 New badge(s) earned: {', '.join(new_badge_ids)}"

    return WorkerTaskSubmitResponse(
        assignment_id=assignment.id,
        status="submitted",
        earnings_credits=earnings,
        xp_earned=xp,
        message=msg,
    )


# ─── Release a task ───────────────────────────────────────────────────────

@router.delete("/tasks/{task_id}/release", status_code=204)
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
