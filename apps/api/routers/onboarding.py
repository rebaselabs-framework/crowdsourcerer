"""Worker onboarding flow — 5-step guided tutorial."""
import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from core.notify import create_notification, NotifType
from models.db import OnboardingProgressDB, UserDB, CreditTransactionDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/onboarding", tags=["onboarding"])

ONBOARDING_STEPS = ["profile", "explore", "first_task", "skills", "cert"]
STEP_LABELS = {
    "profile": "Set up your profile",
    "explore": "Explore the marketplace",
    "first_task": "Complete your first task",
    "skills": "View your skills",
    "cert": "Attempt a certification",
}
COMPLETION_BONUS_CREDITS = 100   # bonus awarded for finishing all 5 steps


# ─── Schemas ──────────────────────────────────────────────────────────────────

class OnboardingStepOut(BaseModel):
    key: str
    label: str
    completed: bool
    order: int


class OnboardingStatusOut(BaseModel):
    user_id: UUID
    steps: list[OnboardingStepOut]
    completed_steps: int
    total_steps: int
    pct_complete: float
    is_complete: bool
    completed_at: Optional[datetime]
    skipped_at: Optional[datetime]
    bonus_claimed: bool
    banner_dismissed: bool = False

    model_config = ConfigDict(from_attributes=True)


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/status", response_model=OnboardingStatusOut)
async def get_onboarding_status(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get the current worker's onboarding progress."""
    progress = await _get_or_create(UUID(user_id), db)
    return _to_out(progress)


@router.post("/steps/{step}/complete")
async def complete_step(
    step: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Mark an onboarding step as complete."""
    if step not in ONBOARDING_STEPS:
        raise HTTPException(400, f"Unknown step '{step}'. Valid: {ONBOARDING_STEPS}")

    progress = await _get_or_create(UUID(user_id), db)
    col = f"step_{step}"
    if getattr(progress, col, False):
        return {"status": "already_complete", "step": step}

    setattr(progress, col, True)
    progress.updated_at = datetime.now(timezone.utc)

    # Check if all steps now done
    all_done = all(getattr(progress, f"step_{s}", False) for s in ONBOARDING_STEPS)
    if all_done and not progress.completed_at:
        progress.completed_at = datetime.now(timezone.utc)

    await db.commit()

    # Award bonus credits if just completed all steps (and not claimed yet).
    # Lock the user row so two concurrent final-step requests don't both pass
    # the bonus_claimed guard before either commits.
    if all_done and not progress.bonus_claimed:
        uid = UUID(user_id)
        user_res = await db.execute(
            select(UserDB).where(UserDB.id == uid).with_for_update()
        )
        user = user_res.scalar_one_or_none()
        if user:
            user.credits += COMPLETION_BONUS_CREDITS
            # NOTE: `type` is a NOT-NULL enum column ("charge"|"credit"|"refund"|"earning").
            # `balance_after` and `tx_type` are not valid columns — they were previously
            # set as phantom Python attributes, causing a silent NULL type on commit.
            tx = CreditTransactionDB(
                user_id=uid,
                amount=COMPLETION_BONUS_CREDITS,
                type="credit",
                description=f"Worker onboarding completion bonus (+{COMPLETION_BONUS_CREDITS} credits)",
            )
            db.add(tx)
            progress.bonus_claimed = True
            await db.commit()
            await create_notification(
                db=db,
                user_id=uid,
                type=NotifType.BADGE_EARNED,
                title="🎉 Onboarding Complete!",
                body=f"You earned +{COMPLETION_BONUS_CREDITS} bonus credits for finishing onboarding.",
                link="/worker/onboarding",
            )

    return {
        "status": "completed",
        "step": step,
        "all_done": all_done,
        "bonus_claimed": progress.bonus_claimed,
    }


@router.post("/skip")
async def skip_onboarding(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Skip the remaining onboarding steps."""
    progress = await _get_or_create(UUID(user_id), db)
    if progress.completed_at:
        return {"status": "already_complete"}
    progress.skipped_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "skipped"}


@router.post("/dismiss-banner")
async def dismiss_onboarding_banner(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Dismiss the onboarding progress banner on the worker dashboard."""
    progress = await _get_or_create(UUID(user_id), db)
    progress.banner_dismissed = True
    await db.commit()
    return {"status": "dismissed"}


@router.post("/reset")
async def reset_onboarding(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Reset onboarding progress (for testing / re-onboarding)."""
    progress = await _get_or_create(UUID(user_id), db)
    for step in ONBOARDING_STEPS:
        setattr(progress, f"step_{step}", False)
    progress.completed_at = None
    progress.skipped_at = None
    progress.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "reset"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def mark_onboarding_step(user_id: UUID, step: str, db: AsyncSession) -> None:
    """Internal helper — mark a step without HTTP context (called from other routers)."""
    if step not in ONBOARDING_STEPS:
        return
    progress = await _get_or_create(user_id, db)
    col = f"step_{step}"
    if not getattr(progress, col, False):
        setattr(progress, col, True)
        progress.updated_at = datetime.now(timezone.utc)
        await db.flush()


async def _get_or_create(user_id: UUID, db: AsyncSession) -> OnboardingProgressDB:
    res = await db.execute(
        select(OnboardingProgressDB).where(OnboardingProgressDB.user_id == user_id)
    )
    progress = res.scalar_one_or_none()
    if not progress:
        # Explicitly set all boolean fields to their defaults so in-memory
        # attributes are correct before the DB INSERT is flushed/refreshed.
        progress = OnboardingProgressDB(
            id=_uuid.uuid4(),
            user_id=user_id,
            step_profile=False,
            step_explore=False,
            step_first_task=False,
            step_skills=False,
            step_cert=False,
            completed_at=None,
            skipped_at=None,
            bonus_claimed=False,
            banner_dismissed=False,
        )
        db.add(progress)
        await db.flush()
    return progress


def _to_out(p: OnboardingProgressDB) -> OnboardingStatusOut:
    steps = []
    for i, key in enumerate(ONBOARDING_STEPS):
        steps.append(OnboardingStepOut(
            key=key,
            label=STEP_LABELS[key],
            completed=getattr(p, f"step_{key}", False),
            order=i + 1,
        ))
    completed_count = sum(1 for s in steps if s.completed)
    total = len(ONBOARDING_STEPS)
    return OnboardingStatusOut(
        user_id=p.user_id,
        steps=steps,
        completed_steps=completed_count,
        total_steps=total,
        pct_complete=round(completed_count / total * 100, 1),
        is_complete=p.completed_at is not None,
        completed_at=p.completed_at,
        skipped_at=p.skipped_at,
        bonus_claimed=p.bonus_claimed,
        banner_dismissed=getattr(p, "banner_dismissed", False),
    )
