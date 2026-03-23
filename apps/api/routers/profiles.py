"""Public worker profile endpoints."""
from __future__ import annotations
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from models.db import UserDB, WorkerSkillDB, WorkerBadgeDB, WorkerCertificationDB
from models.schemas import (
    PublicWorkerProfileOut, PublicProfileSkill, PublicProfileBadge,
    ProfileUpdateRequest, UserOut,
)

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

    badges = [
        PublicProfileBadge(
            badge_slug=b.badge_slug,
            badge_name=b.badge_name,
            badge_description=b.badge_description,
            earned_at=b.earned_at,
        )
        for b in badge_rows
    ]

    return PublicWorkerProfileOut(
        id=user.id,
        name=user.name,
        bio=user.bio,
        avatar_url=user.avatar_url,
        role=user.role,
        worker_level=user.worker_level,
        worker_xp=user.worker_xp,
        worker_tasks_completed=user.worker_tasks_completed,
        worker_accuracy=user.worker_accuracy,
        worker_reliability=user.worker_reliability,
        reputation_score=user.reputation_score,
        worker_streak_days=user.worker_streak_days,
        skills=skills,
        badges=badges,
        member_since=user.created_at,
    )


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

    await db.commit()
    await db.refresh(user)
    return user


@router.get("/users/me/profile-status", response_model=dict)
async def get_profile_status(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return profile completeness info for the authenticated user."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    completeness_fields = [
        bool(user.name),
        bool(user.bio),
        bool(user.avatar_url),
    ]
    pct = int(sum(completeness_fields) / len(completeness_fields) * 100)

    return {
        "profile_public": user.profile_public,
        "completeness_pct": pct,
        "has_name": bool(user.name),
        "has_bio": bool(user.bio),
        "has_avatar": bool(user.avatar_url),
        "totp_enabled": user.totp_enabled,
    }
