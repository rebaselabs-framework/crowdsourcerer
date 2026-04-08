"""Public worker profile endpoints."""
from uuid import UUID
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.background import safe_create_task
from core.database import get_db
from models.db import UserDB, WorkerSkillDB, WorkerBadgeDB, WorkerCertificationDB, TaskDB, TaskAssignmentDB
from models.schemas import (
    PublicWorkerProfileOut, PublicProfileSkill, PublicProfileBadge,
    ProfileUpdateRequest, UserOut,
)
from routers.badges import _BADGE_MAP  # badge metadata (name, description, icon) keyed by badge_id

logger = structlog.get_logger()
router = APIRouter(prefix="/v1", tags=["profiles"])


# ─── Public profile ────────────────────────────────────────────────────────────

@router.get("/workers/{worker_id}/profile", response_model=PublicWorkerProfileOut)
async def get_public_profile(
    worker_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return the public profile of a worker.

    No authentication required. Returns 404 if the worker has set
    ``profile_public = false``.
    """
    result = await db.execute(select(UserDB).where(UserDB.id == worker_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="Worker not found")

    # Must be a worker role
    if user.role not in ("worker", "both"):
        raise HTTPException(status_code=404, detail="Worker not found")

    # Respect profile visibility
    if not user.profile_public:
        raise HTTPException(status_code=404, detail="Profile is private")

    # Skills
    skills_result = await db.execute(
        select(WorkerSkillDB).where(WorkerSkillDB.worker_id == worker_id)
    )
    skill_rows = skills_result.scalars().all()

    # Certifications (to mark certified skills)
    cert_result = await db.execute(
        select(WorkerCertificationDB).where(
            WorkerCertificationDB.worker_id == worker_id,
            WorkerCertificationDB.passed == True,  # noqa: E712
        )
    )
    certified_task_types = {c.task_type for c in cert_result.scalars().all()}

    skills = [
        PublicProfileSkill(
            task_type=s.task_type,
            proficiency_level=s.proficiency_level,
            tasks_completed=s.tasks_completed,
            avg_accuracy=s.avg_accuracy,
            certified=s.task_type in certified_task_types,
        )
        for s in skill_rows
    ]

    # Badges
    badges_result = await db.execute(
        select(WorkerBadgeDB).where(WorkerBadgeDB.user_id == worker_id)
    )
    badge_rows = badges_result.scalars().all()

    badges = []
    for b in badge_rows:
        defn = _BADGE_MAP.get(b.badge_id)
        badges.append(PublicProfileBadge(
            badge_slug=b.badge_id,
            badge_name=defn.name if defn else b.badge_id.replace("_", " ").title(),
            badge_description=defn.description if defn else None,
            badge_icon=defn.icon if defn else "🏆",
            earned_at=b.earned_at,
        ))

    return PublicWorkerProfileOut(
        id=user.id,
        name=user.name,
        bio=user.bio,
        avatar_url=user.avatar_url,
        location=user.location,
        website_url=user.website_url,
        role=user.role,
        worker_level=user.worker_level,
        worker_xp=user.worker_xp,
        worker_tasks_completed=user.worker_tasks_completed,
        worker_accuracy=user.worker_accuracy,
        worker_reliability=user.worker_reliability,
        reputation_score=user.reputation_score,
        worker_streak_days=user.worker_streak_days,
        avg_feedback_score=user.avg_feedback_score,
        total_ratings_received=user.total_ratings_received or 0,
        skills=skills,
        badges=badges,
        member_since=user.created_at,
    )


# ─── Per-task-type stats ──────────────────────────────────────────────────────

@router.get("/workers/{worker_id}/task-stats")
async def get_worker_task_stats(
    worker_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return per-task-type stats for a worker.

    Returns a list of objects with task_type, tasks_completed, avg_accuracy,
    total_earnings_credits, and proficiency_level.  Sorted by tasks_completed desc.
    """
    result = await db.execute(select(UserDB).where(UserDB.id == worker_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both") or not user.profile_public:
        raise HTTPException(status_code=404, detail="Worker not found")

    skills_result = await db.execute(
        select(WorkerSkillDB)
        .where(WorkerSkillDB.worker_id == worker_id)
        .order_by(desc(WorkerSkillDB.tasks_completed))
    )
    skills = skills_result.scalars().all()

    return [
        {
            "task_type": s.task_type,
            "tasks_completed": s.tasks_completed,
            "avg_accuracy": round(s.avg_accuracy, 4) if s.avg_accuracy is not None else None,
            "proficiency_level": s.proficiency_level,
        }
        for s in skills
    ]


# ─── Recent activity ──────────────────────────────────────────────────────────

@router.get("/workers/{worker_id}/recent-activity")
async def get_worker_recent_activity(
    worker_id: UUID,
    limit: int = Query(10, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
):
    """Return recent approved/completed task assignments for a worker (public).

    Returns task type, status, submitted_at and earnings for each.
    """
    result = await db.execute(select(UserDB).where(UserDB.id == worker_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both") or not user.profile_public:
        raise HTTPException(status_code=404, detail="Worker not found")

    assignments_result = await db.execute(
        select(TaskAssignmentDB, TaskDB)
        .join(TaskDB, TaskDB.id == TaskAssignmentDB.task_id)
        .where(
            TaskAssignmentDB.worker_id == worker_id,
            TaskAssignmentDB.status == "approved",
        )
        .order_by(desc(TaskAssignmentDB.submitted_at))
        .limit(limit)
    )
    rows = assignments_result.all()

    return [
        {
            "task_type": task.type,
            "submitted_at": assignment.submitted_at.isoformat() if assignment.submitted_at else None,
            "earnings_credits": assignment.earnings_credits,
            "xp_earned": assignment.xp_earned,
        }
        for assignment, task in rows
    ]


# ─── Own profile management ────────────────────────────────────────────────────

@router.patch("/users/me/profile", response_model=UserOut)
async def update_my_profile(
    req: ProfileUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Update the authenticated user's public profile fields."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if req.name is not None:
        user.name = req.name
    if req.bio is not None:
        user.bio = req.bio
    if req.avatar_url is not None:
        # Basic URL validation
        url = req.avatar_url.strip()
        if url and not url.startswith(("https://", "http://")):
            raise HTTPException(status_code=400, detail="avatar_url must be a valid URL")
        user.avatar_url = url or None
    if req.profile_public is not None:
        user.profile_public = req.profile_public
    if req.location is not None:
        user.location = req.location.strip() or None
    if req.website_url is not None:
        url = req.website_url.strip()
        if url and not url.startswith(("https://", "http://")):
            raise HTTPException(status_code=400, detail="website_url must be a valid URL")
        user.website_url = url or None

    await db.commit()
    await db.refresh(user)

    # Auto-advance onboarding steps for both flows — any profile save counts.
    # Both fire in the background so they add zero latency to the profile save.
    #   • Requester onboarding: "welcome" step (CTA = /dashboard/profile)
    #   • Worker   onboarding: "profile" step (CTA = /dashboard/profile)
    safe_create_task(_advance_requester_onboarding(user_id), name="onboarding.welcome")
    safe_create_task(_advance_worker_onboarding_profile(user_id), name="onboarding.profile")

    return user


async def _advance_requester_onboarding(user_id: str) -> None:
    """Background helper: advance requester onboarding 'welcome' step."""
    from core.database import AsyncSessionLocal
    from routers.requester_onboarding import complete_step_internal
    async with AsyncSessionLocal() as _db:
        try:
            await complete_step_internal(user_id, "welcome", _db)
        except Exception:
            logger.warning("onboarding.requester_welcome_advance_failed", user_id=user_id, exc_info=True)


async def _advance_worker_onboarding_profile(user_id: str) -> None:
    """Background helper: advance worker onboarding 'profile' step."""
    import uuid as _uuid
    from core.database import AsyncSessionLocal
    from routers.onboarding import mark_onboarding_step
    async with AsyncSessionLocal() as _db:
        try:
            await mark_onboarding_step(_uuid.UUID(user_id), "profile", _db)
            await _db.commit()
        except Exception:
            logger.warning("onboarding.worker_profile_advance_failed", user_id=user_id, exc_info=True)


@router.get("/users/me/profile-status", response_model=dict)
async def get_profile_status(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return profile completeness info for the authenticated user.

    Returns a 0-100 completeness score based on filled fields, skill count,
    and certification count.  Used to show the profile completion banner on
    the worker dashboard.
    """
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Count skills and certifications
    skill_count = await db.scalar(
        select(func.count()).where(WorkerSkillDB.worker_id == user_id)
    ) or 0
    cert_count = await db.scalar(
        select(func.count()).where(
            WorkerCertificationDB.worker_id == user_id,
            WorkerCertificationDB.passed == True,  # noqa: E712
        )
    ) or 0

    # Weighted completion score:
    # name (15), bio (20), avatar (15), location (10), website (10),
    # has_skills (20), has_cert (10)
    score = 0
    if user.name:
        score += 15
    if user.bio:
        score += 20
    if user.avatar_url:
        score += 15
    if user.location:
        score += 10
    if user.website_url:
        score += 10
    if skill_count > 0:
        score += 20
    if cert_count > 0:
        score += 10

    missing = []
    if not user.bio:
        missing.append("bio")
    if not user.avatar_url:
        missing.append("avatar")
    if not user.location:
        missing.append("location")
    if not user.website_url:
        missing.append("website")
    if skill_count == 0:
        missing.append("skills")
    if cert_count == 0:
        missing.append("certification")

    return {
        "profile_public": user.profile_public,
        "completeness_pct": score,
        "has_name": bool(user.name),
        "has_bio": bool(user.bio),
        "has_avatar": bool(user.avatar_url),
        "has_location": bool(user.location),
        "has_website": bool(user.website_url),
        "skill_count": skill_count,
        "cert_count": cert_count,
        "totp_enabled": user.totp_enabled,
        "missing": missing,
    }
