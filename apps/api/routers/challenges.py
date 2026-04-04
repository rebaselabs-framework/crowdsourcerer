"""Daily challenges — rotating bonus tasks for workers."""
from __future__ import annotations
import random
from datetime import datetime, timezone, date, timedelta
from typing import Optional
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.auth import get_current_user_id
from core.database import get_db
from models.db import (
    DailyChallengeDB, DailyChallengeProgressDB, UserDB,
    CreditTransactionDB,
)
from models.schemas import DailyChallengeOut, DailyChallengeProgressOut

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/challenges", tags=["challenges"])


# ─── Challenge templates (cycled daily) ───────────────────────────────────

CHALLENGE_TEMPLATES = [
    {
        "task_type": "label_image",
        "title": "Image Labeling Sprint",
        "description": "Label images accurately. Help us build better training datasets!",
        "bonus_xp": 30,
        "bonus_credits": 8,
        "target_count": 3,
    },
    {
        "task_type": "verify_fact",
        "title": "Fact Checker",
        "description": "Verify facts with accuracy. Your attention to detail matters.",
        "bonus_xp": 40,
        "bonus_credits": 10,
        "target_count": 3,
    },
    {
        "task_type": "moderate_content",
        "title": "Content Guardian",
        "description": "Keep the platform safe. Review and moderate submitted content.",
        "bonus_xp": 25,
        "bonus_credits": 6,
        "target_count": 3,
    },
    {
        "task_type": "answer_question",
        "title": "Knowledge Quest",
        "description": "Answer questions thoughtfully and accurately.",
        "bonus_xp": 50,
        "bonus_credits": 12,
        "target_count": 2,
    },
    {
        "task_type": "label_text",
        "title": "Text Analysis Day",
        "description": "Classify and label text content — critical for NLP training.",
        "bonus_xp": 25,
        "bonus_credits": 6,
        "target_count": 4,
    },
    {
        "task_type": "compare_rank",
        "title": "The Ranking Challenge",
        "description": "Compare and rank items. Your judgments power AI evaluation.",
        "bonus_xp": 30,
        "bonus_credits": 8,
        "target_count": 3,
    },
    {
        "task_type": "transcription_review",
        "title": "Transcription Accuracy",
        "description": "Review and correct audio transcriptions for perfect accuracy.",
        "bonus_xp": 45,
        "bonus_credits": 10,
        "target_count": 2,
    },
    {
        "task_type": "rate_quality",
        "title": "Quality Rater",
        "description": "Rate content quality — your ratings help train better models.",
        "bonus_xp": 20,
        "bonus_credits": 5,
        "target_count": 5,
    },
]


def _template_for_date(d: date) -> dict:
    """Deterministically pick a challenge template based on the date."""
    # Use the day-of-year to cycle through templates
    idx = d.timetuple().tm_yday % len(CHALLENGE_TEMPLATES)
    return CHALLENGE_TEMPLATES[idx]


async def _get_or_create_challenge(db: AsyncSession, target_date: date) -> DailyChallengeDB:
    """Get today's challenge, creating it if it doesn't exist yet."""
    result = await db.execute(
        select(DailyChallengeDB).where(DailyChallengeDB.challenge_date == target_date)
    )
    challenge = result.scalar_one_or_none()

    if not challenge:
        template = _template_for_date(target_date)
        challenge = DailyChallengeDB(
            id=uuid4(),
            challenge_date=target_date,
            task_type=template["task_type"],
            title=template["title"],
            description=template["description"],
            bonus_xp=template["bonus_xp"],
            bonus_credits=template["bonus_credits"],
            target_count=template["target_count"],
        )
        db.add(challenge)
        await db.flush()  # Get the ID without full commit
        logger.info("daily_challenge_created", date=str(target_date), type=template["task_type"])

    return challenge


async def _get_or_create_progress(
    db: AsyncSession, user_id: str, challenge: DailyChallengeDB
) -> DailyChallengeProgressDB:
    """Get or create a worker's progress record for a challenge."""
    result = await db.execute(
        select(DailyChallengeProgressDB).where(
            DailyChallengeProgressDB.user_id == user_id,
            DailyChallengeProgressDB.challenge_id == challenge.id,
        )
    )
    progress = result.scalar_one_or_none()

    if not progress:
        progress = DailyChallengeProgressDB(
            id=uuid4(),
            user_id=user_id,
            challenge_id=challenge.id,
            tasks_completed=0,
            bonus_claimed=False,
        )
        db.add(progress)
        await db.flush()

    return progress


# ─── Routes ───────────────────────────────────────────────────────────────

@router.get("/today", response_model=DailyChallengeProgressOut)
async def get_today_challenge(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get today's daily challenge and the current worker's progress."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    today = datetime.now(timezone.utc).date()
    challenge = await _get_or_create_challenge(db, today)
    progress = await _get_or_create_progress(db, user_id, challenge)
    await db.commit()

    return DailyChallengeProgressOut(
        challenge=DailyChallengeOut.model_validate(challenge),
        tasks_completed=progress.tasks_completed,
        bonus_claimed=progress.bonus_claimed,
        is_complete=progress.tasks_completed >= challenge.target_count,
        tasks_remaining=max(0, challenge.target_count - progress.tasks_completed),
    )


@router.post("/today/claim", response_model=DailyChallengeProgressOut)
async def claim_daily_bonus(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Claim the daily challenge bonus (if challenge is complete and bonus not yet claimed)."""
    # Lock the user row so that a double-click / concurrent request cannot award
    # the bonus twice before the first commit sets bonus_claimed=True.
    result = await db.execute(select(UserDB).where(UserDB.id == user_id).with_for_update())
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    today = datetime.now(timezone.utc).date()
    challenge = await _get_or_create_challenge(db, today)
    progress = await _get_or_create_progress(db, user_id, challenge)

    if progress.tasks_completed < challenge.target_count:
        raise HTTPException(
            status_code=400,
            detail=f"Challenge not complete. Need {challenge.target_count - progress.tasks_completed} more tasks.",
        )

    if progress.bonus_claimed:
        raise HTTPException(status_code=409, detail="Daily bonus already claimed.")

    # Award the bonus
    now = datetime.now(timezone.utc)
    progress.bonus_claimed = True
    progress.bonus_claimed_at = now

    user.worker_xp += challenge.bonus_xp
    user.credits += challenge.bonus_credits

    # Record credit transaction
    txn = CreditTransactionDB(
        user_id=user_id,
        amount=challenge.bonus_credits,
        type="credit",
        description=f"Daily challenge bonus: {challenge.title}",
    )
    db.add(txn)

    # Update challenge-type quests
    try:
        from routers.quests import update_quest_on_challenge_complete
        await update_quest_on_challenge_complete(db, str(user_id))
    except Exception:
        import structlog as _sl
        _sl.get_logger().warning("quest.challenge_update_failed", user_id=str(user_id), exc_info=True)

    await db.commit()

    logger.info(
        "daily_challenge_claimed",
        user_id=user_id,
        challenge_date=str(today),
        bonus_xp=challenge.bonus_xp,
        bonus_credits=challenge.bonus_credits,
    )

    return DailyChallengeProgressOut(
        challenge=DailyChallengeOut.model_validate(challenge),
        tasks_completed=progress.tasks_completed,
        bonus_claimed=True,
        is_complete=True,
        tasks_remaining=0,
    )


@router.get("/history", response_model=list[DailyChallengeProgressOut])
async def get_challenge_history(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get the worker's challenge history (last 30 days)."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    thirty_days_ago = datetime.now(timezone.utc).date() - timedelta(days=30)

    result = await db.execute(
        select(DailyChallengeProgressDB, DailyChallengeDB)
        .join(DailyChallengeDB, DailyChallengeProgressDB.challenge_id == DailyChallengeDB.id)
        .where(
            DailyChallengeProgressDB.user_id == user_id,
            DailyChallengeDB.challenge_date >= thirty_days_ago,
        )
        .order_by(DailyChallengeDB.challenge_date.desc())
    )
    rows = result.all()

    return [
        DailyChallengeProgressOut(
            challenge=DailyChallengeOut.model_validate(challenge),
            tasks_completed=progress.tasks_completed,
            bonus_claimed=progress.bonus_claimed,
            is_complete=progress.tasks_completed >= challenge.target_count,
            tasks_remaining=max(0, challenge.target_count - progress.tasks_completed),
        )
        for progress, challenge in rows
    ]
