"""Worker skill profiling + personalized task recommendations."""

import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from sqlalchemy.exc import SQLAlchemyError

from core.auth import get_current_user_id
from core.database import get_db
from models.db import UserDB, TaskDB, TaskAssignmentDB, WorkerSkillDB, TaskApplicationDB
from models.schemas import (
    WorkerSkillOut,
    WorkerSkillsOut,
    PROFICIENCY_LABELS,
    MarketplaceTaskOut,
    PaginatedMarketplaceTasks,
)

# Auto-verify thresholds
_VERIFY_MIN_PROFICIENCY = 4
_VERIFY_MIN_ACCURACY = 0.90
_VERIFY_MIN_COMPLETED = 15

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/workers", tags=["skills"])

# ─── Human task types only (for marketplace) ──────────────────────────────
HUMAN_TASK_TYPES = {
    "label_image", "label_text", "rate_quality", "verify_fact",
    "moderate_content", "compare_rank", "answer_question", "transcription_review",
}

AI_TASK_TYPES = {
    "web_research", "entity_lookup", "document_parse", "data_transform",
    "llm_generate", "screenshot", "audio_transcribe", "pii_detect",
    "code_execute", "web_intel",
}


def _proficiency_for_completed(tasks_completed: int, accuracy: Optional[float]) -> int:
    """Compute 1–5 proficiency level from task count and accuracy."""
    if tasks_completed == 0:
        return 1
    # Base level from volume
    if tasks_completed >= 100:
        base = 5
    elif tasks_completed >= 50:
        base = 4
    elif tasks_completed >= 20:
        base = 3
    elif tasks_completed >= 5:
        base = 2
    else:
        base = 1

    # Accuracy modifier: excellent accuracy bumps up, poor accuracy bumps down
    if accuracy is not None:
        if accuracy >= 0.95 and base < 5:
            base = min(5, base + 1)
        elif accuracy < 0.70 and base > 1:
            base = max(1, base - 1)

    return base


async def update_worker_skill(
    db: AsyncSession,
    worker_id: uuid.UUID,
    task_type: str,
    outcome: str,  # "approved" | "rejected" | "completed"
    response_minutes: Optional[float],
    credits_earned: int,
) -> None:
    """Upsert the worker_skills row for (worker_id, task_type)."""
    # Lock the skill row so concurrent approvals/rejections for the same
    # (worker, task_type) don't race on Python-level counter increments.
    result = await db.execute(
        select(WorkerSkillDB).where(
            and_(
                WorkerSkillDB.worker_id == worker_id,
                WorkerSkillDB.task_type == task_type,
            )
        ).with_for_update()
    )
    skill = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)

    if skill is None:
        skill = WorkerSkillDB(
            id=uuid.uuid4(),
            worker_id=worker_id,
            task_type=task_type,
        )
        db.add(skill)

    skill.tasks_completed += 1
    if outcome == "approved":
        skill.tasks_approved += 1
    elif outcome == "rejected":
        skill.tasks_rejected += 1

    skill.credits_earned += credits_earned
    skill.last_task_at = now

    # Recompute accuracy
    graded = skill.tasks_approved + skill.tasks_rejected
    skill.accuracy = (skill.tasks_approved / graded) if graded > 0 else None

    # Recompute avg response time (rolling average)
    if response_minutes is not None:
        prev_avg = skill.avg_response_minutes or response_minutes
        skill.avg_response_minutes = (
            (prev_avg * (skill.tasks_completed - 1) + response_minutes)
            / skill.tasks_completed
        )

    skill.proficiency_level = _proficiency_for_completed(
        skill.tasks_completed, skill.accuracy
    )
    skill.updated_at = now

    # Auto-verify: upgrade to verified once thresholds are met
    if (
        not skill.verified
        and skill.proficiency_level >= _VERIFY_MIN_PROFICIENCY
        and skill.accuracy is not None
        and skill.accuracy >= _VERIFY_MIN_ACCURACY
        and skill.tasks_completed >= _VERIFY_MIN_COMPLETED
    ):
        skill.verified = True
        skill.verified_at = now
        logger.info(
            "skill_auto_verified",
            worker_id=str(worker_id),
            task_type=task_type,
            proficiency=skill.proficiency_level,
            accuracy=skill.accuracy,
        )


# ─── Endpoints ────────────────────────────────────────────────────────────

@router.get("/me/skills", response_model=WorkerSkillsOut)
async def get_my_skills(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Return worker skill profile with per-task-type breakdown."""
    uid = uuid.UUID(user_id)
    result = await db.execute(
        select(WorkerSkillDB)
        .where(WorkerSkillDB.worker_id == uid)
        .order_by(WorkerSkillDB.proficiency_level.desc(), WorkerSkillDB.tasks_completed.desc())
    )
    rows = result.scalars().all()

    skills = []
    for s in rows:
        skills.append(
            WorkerSkillOut(
                task_type=s.task_type,
                tasks_completed=s.tasks_completed,
                tasks_approved=s.tasks_approved,
                tasks_rejected=s.tasks_rejected,
                accuracy=s.accuracy,
                avg_response_minutes=s.avg_response_minutes,
                credits_earned=s.credits_earned,
                proficiency_level=s.proficiency_level,
                proficiency_label=PROFICIENCY_LABELS.get(s.proficiency_level, "Novice"),
                last_task_at=s.last_task_at,
                verified=s.verified,
                verified_at=s.verified_at,
            )
        )

    top_skill: Optional[str] = None
    strongest_category: Optional[str] = None

    if skills:
        top = max(skills, key=lambda s: (s.proficiency_level, s.tasks_completed))
        top_skill = top.task_type

        human_score = sum(
            s.proficiency_level for s in skills if s.task_type in HUMAN_TASK_TYPES
        )
        ai_score = sum(
            s.proficiency_level for s in skills if s.task_type in AI_TASK_TYPES
        )
        if human_score > 0 or ai_score > 0:
            strongest_category = "human" if human_score >= ai_score else "ai"

    # ── Onboarding: mark skills step ─────────────────────────────────────
    try:
        from routers.onboarding import mark_onboarding_step
        await mark_onboarding_step(uid, "skills", db)
        await db.commit()  # flush → commit so the step is actually persisted
    except SQLAlchemyError:
        logger.warning(
            "skills.onboarding_step_failed",
            user_id=str(uid),
            step="skills",
            exc_info=True,
        )

    verified_count = sum(1 for s in skills if s.verified)

    return WorkerSkillsOut(
        skills=skills,
        top_skill=top_skill,
        strongest_category=strongest_category,
        verified_count=verified_count,
    )


@router.get("/me/recommended", response_model=PaginatedMarketplaceTasks)
async def get_recommended_tasks(
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Return personalized open task recommendations based on skill profile."""
    uid = uuid.UUID(user_id)

    # Load worker's skills, sorted by proficiency desc
    skill_result = await db.execute(
        select(WorkerSkillDB)
        .where(WorkerSkillDB.worker_id == uid)
        .order_by(WorkerSkillDB.proficiency_level.desc())
    )
    skills = skill_result.scalars().all()

    # Build a priority list of task types
    known_types = [s.task_type for s in skills]
    # Types the worker has never done go last
    all_human = list(HUMAN_TASK_TYPES)
    new_types = [t for t in all_human if t not in known_types]
    preferred_types = known_types + new_types

    # Build a weighted query: rank tasks by type preference
    # Simple approach: fetch open tasks, prefer preferred types
    tasks_result = await db.execute(
        select(TaskDB)
        .where(
            and_(
                TaskDB.status == "open",
                TaskDB.execution_mode == "human",
                TaskDB.type.in_(preferred_types),
            )
        )
        .order_by(
            # Approximate preference ordering — items matching top skills come first
            # We use a CASE WHEN via Python ordering post-fetch for simplicity
            TaskDB.worker_reward_credits.desc(),
            TaskDB.created_at.desc(),
        )
        .limit(200)  # Fetch a pool, then re-rank in Python
    )
    all_tasks = tasks_result.scalars().all()

    # Check which tasks this worker already has active assignments for
    assignments_result = await db.execute(
        select(TaskAssignmentDB.task_id).where(
            and_(
                TaskAssignmentDB.worker_id == uid,
                TaskAssignmentDB.status.in_(["active", "submitted"]),
            )
        )
    )
    claimed_task_ids = {r for r, in assignments_result.fetchall()}

    # Filter out already-claimed tasks
    available = [t for t in all_tasks if t.id not in claimed_task_ids]

    # Re-rank by type preference (index in preferred_types = lower is better)
    type_rank = {t: i for i, t in enumerate(preferred_types)}
    available.sort(key=lambda t: (type_rank.get(t.type, 999), -t.worker_reward_credits))

    total = len(available)
    page = available[offset: offset + limit]

    # Bulk-check which page tasks this worker has already applied to
    page_task_ids = [t.id for t in page]
    applied_ids: set = set()
    if page_task_ids:
        applied_res = await db.execute(
            select(TaskApplicationDB.task_id).where(
                TaskApplicationDB.worker_id == uid,
                TaskApplicationDB.task_id.in_(page_task_ids),
                TaskApplicationDB.status.in_(("pending", "accepted")),
            )
        )
        applied_ids = {row[0] for row in applied_res}

    items = []
    for t in page:
        items.append(
            MarketplaceTaskOut(
                id=t.id,
                type=t.type,
                task_instructions=t.task_instructions,
                reward_credits=t.worker_reward_credits or 0,
                slots_available=t.assignments_required - t.assignments_completed,
                assignments_required=t.assignments_required,
                assignments_completed=t.assignments_completed,
                priority=t.priority,
                estimated_minutes=t.claim_timeout_minutes or 30,
                created_at=t.created_at,
                tags=t.tags,
                application_mode=bool(t.application_mode),
                user_applied=t.id in applied_ids,
            )
        )

    return PaginatedMarketplaceTasks(
        items=items,
        total=total,
        page=(offset // limit) + 1 if limit else 1,
        page_size=limit,
        has_next=(offset + limit) < total,
    )


@router.get("/{worker_id}/verified-skills", response_model=list[WorkerSkillOut])
async def get_worker_verified_skills(
    worker_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint — return the verified skills for a worker profile.
    Only returns skills where verified=True.
    """
    try:
        uid = uuid.UUID(worker_id)
    except ValueError:
        return []

    # Ensure the worker exists and their profile is public
    user_result = await db.execute(select(UserDB).where(UserDB.id == uid))
    user = user_result.scalar_one_or_none()
    if not user or not user.profile_public:
        return []

    result = await db.execute(
        select(WorkerSkillDB)
        .where(WorkerSkillDB.worker_id == uid, WorkerSkillDB.verified == True)  # noqa: E712
        .order_by(WorkerSkillDB.proficiency_level.desc(), WorkerSkillDB.tasks_completed.desc())
    )
    rows = result.scalars().all()

    return [
        WorkerSkillOut(
            task_type=s.task_type,
            tasks_completed=s.tasks_completed,
            tasks_approved=s.tasks_approved,
            tasks_rejected=s.tasks_rejected,
            accuracy=s.accuracy,
            avg_response_minutes=s.avg_response_minutes,
            credits_earned=s.credits_earned,
            proficiency_level=s.proficiency_level,
            proficiency_label=PROFICIENCY_LABELS.get(s.proficiency_level, "Novice"),
            last_task_at=s.last_task_at,
            verified=True,
            verified_at=s.verified_at,
        )
        for s in rows
    ]
