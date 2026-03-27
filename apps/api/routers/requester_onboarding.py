"""Requester onboarding flow — guided steps with a 200-credit completion bonus.

Steps (in order):
  1. welcome        — complete profile / acknowledge
  2. create_task    — auto-hooked when user creates first task
  3. view_results   — mark manually when user opens a completed task's result page
  4. set_webhook    — auto-hooked when user registers a webhook endpoint
  5. invite_team    — auto-hooked when user creates an org invite (or can skip)

+200 credits awarded on full completion (once per user).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.auth import get_current_user_id
from core.database import get_db
from models.db import RequesterOnboardingDB, UserDB, CreditTransactionDB
from models.schemas import (
    RequesterOnboardingStatusOut,
    RequesterOnboardingStepOut,
    REQUESTER_ONBOARDING_STEPS,
    REQUESTER_STEP_META,
)

router = APIRouter(prefix="/v1/requester-onboarding", tags=["requester-onboarding"])

ONBOARDING_BONUS = 200


def _build_status(rec: RequesterOnboardingDB) -> RequesterOnboardingStatusOut:
    """Convert a DB record to the status response object."""
    step_flags = {
        "welcome": rec.step_welcome,
        "create_task": rec.step_create_task,
        "view_results": rec.step_view_results,
        "set_webhook": rec.step_set_webhook,
        "invite_team": rec.step_invite_team,
    }
    steps = []
    for key in REQUESTER_ONBOARDING_STEPS:
        meta = REQUESTER_STEP_META[key]
        steps.append(RequesterOnboardingStepOut(
            key=key,
            title=meta["title"],
            description=meta["description"],
            cta=meta["cta"],
            cta_url=meta["cta_url"],
            icon=meta["icon"],
            completed=bool(step_flags.get(key, False)),
        ))

    completed_count = sum(1 for s in steps if s.completed)
    return RequesterOnboardingStatusOut(
        steps=steps,
        completed_count=completed_count,
        total_steps=len(REQUESTER_ONBOARDING_STEPS),
        all_complete=(completed_count == len(REQUESTER_ONBOARDING_STEPS)),
        bonus_claimed=rec.bonus_claimed,
        skipped_at=rec.skipped_at,
    )


async def _get_or_create_record(user_id: str, db: AsyncSession) -> RequesterOnboardingDB:
    result = await db.execute(
        select(RequesterOnboardingDB).where(RequesterOnboardingDB.user_id == user_id)
    )
    rec = result.scalar_one_or_none()
    if not rec:
        # Explicitly set all boolean fields to their defaults so in-memory
        # attributes are correct before the DB INSERT is flushed/refreshed.
        rec = RequesterOnboardingDB(
            id=uuid.uuid4(),
            user_id=user_id,
            step_welcome=False,
            step_create_task=False,
            step_view_results=False,
            step_set_webhook=False,
            step_invite_team=False,
            bonus_claimed=False,
        )
        db.add(rec)
        await db.flush()
    return rec


@router.get("/status", response_model=RequesterOnboardingStatusOut)
async def get_status(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get current onboarding progress."""
    rec = await _get_or_create_record(user_id, db)
    await db.commit()
    return _build_status(rec)


@router.post("/steps/{step}/complete", response_model=RequesterOnboardingStatusOut)
async def complete_step(
    step: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Manually mark a step as complete (useful for view_results)."""
    if step not in REQUESTER_ONBOARDING_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown step. Valid steps: {', '.join(REQUESTER_ONBOARDING_STEPS)}",
        )
    rec = await _get_or_create_record(user_id, db)
    await _set_step(rec, step, db, user_id)
    await db.commit()
    await db.refresh(rec)
    return _build_status(rec)


@router.post("/steps/{step}/skip", response_model=RequesterOnboardingStatusOut)
async def skip_step(
    step: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Skip a non-critical step (e.g. invite_team if user works solo)."""
    if step not in ("set_webhook", "invite_team"):
        raise HTTPException(status_code=400, detail="Only set_webhook and invite_team can be skipped")
    rec = await _get_or_create_record(user_id, db)
    await _set_step(rec, step, db, user_id)
    await db.commit()
    await db.refresh(rec)
    return _build_status(rec)


@router.post("/skip", response_model=RequesterOnboardingStatusOut)
async def skip_onboarding(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Skip the entire onboarding flow (persisted to DB — won't resurface on next login)."""
    rec = await _get_or_create_record(user_id, db)
    if not rec.completed_at:  # Already completed → no-op; just return status
        rec.skipped_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(rec)
    return _build_status(rec)


@router.post("/reset", status_code=204, response_model=None)
async def reset_onboarding(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Reset onboarding progress (dev/testing use)."""
    result = await db.execute(
        select(RequesterOnboardingDB).where(RequesterOnboardingDB.user_id == user_id)
    )
    rec = result.scalar_one_or_none()
    if rec:
        rec.step_welcome = False
        rec.step_create_task = False
        rec.step_view_results = False
        rec.step_set_webhook = False
        rec.step_invite_team = False
        rec.completed_at = None
        rec.skipped_at = None
        rec.bonus_claimed = False
        await db.commit()


async def _set_step(
    rec: RequesterOnboardingDB,
    step: str,
    db: AsyncSession,
    user_id: str,
) -> None:
    """Mark a step complete; award bonus if all steps now done."""
    flag_map = {
        "welcome": "step_welcome",
        "create_task": "step_create_task",
        "view_results": "step_view_results",
        "set_webhook": "step_set_webhook",
        "invite_team": "step_invite_team",
    }
    setattr(rec, flag_map[step], True)

    # Check if all steps are now complete
    all_done = all(
        getattr(rec, flag_map[s], False) for s in REQUESTER_ONBOARDING_STEPS
    )

    if all_done and not rec.completed_at:
        rec.completed_at = datetime.now(timezone.utc)

    if all_done and not rec.bonus_claimed:
        rec.bonus_claimed = True
        # Lock user row so concurrent final-step completions can't both award.
        user_result = await db.execute(
            select(UserDB).where(UserDB.id == user_id).with_for_update()
        )
        user = user_result.scalar_one_or_none()
        if user:
            user.credits += ONBOARDING_BONUS
            txn = CreditTransactionDB(
                user_id=user_id,
                amount=ONBOARDING_BONUS,
                type="credit",
                description=f"Requester onboarding completion bonus (+{ONBOARDING_BONUS} credits)",
            )
            db.add(txn)
            # In-app notification
            from core.notify import create_notification, NotifType
            await create_notification(
                db=db,
                user_id=user_id,
                type=NotifType.SYSTEM,
                title="Onboarding complete! 🎉",
                body=(
                    f"You've completed all requester onboarding steps and earned "
                    f"{ONBOARDING_BONUS} bonus credits. Welcome to CrowdSorcerer!"
                ),
            )


async def complete_step_internal(user_id: str, step: str, db: AsyncSession) -> None:
    """Called from other routers (tasks, webhooks, orgs) to auto-advance onboarding."""
    if step not in REQUESTER_ONBOARDING_STEPS:
        return
    rec = await _get_or_create_record(user_id, db)
    # Don't re-complete (saves a write)
    flag_map = {
        "welcome": "step_welcome",
        "create_task": "step_create_task",
        "view_results": "step_view_results",
        "set_webhook": "step_set_webhook",
        "invite_team": "step_invite_team",
    }
    if getattr(rec, flag_map[step], False):
        return  # already done
    await _set_step(rec, step, db, user_id)
    await db.commit()
