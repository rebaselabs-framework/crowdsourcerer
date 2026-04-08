"""Quests — weekly multi-step challenges for workers.

Quests provide week-long goals that complement daily challenges. Each week,
3-5 quests are selected from a template library. Workers earn bonus XP +
credits for completing each quest.

Quest types:
  - volume: complete N tasks
  - streak: maintain a streak of N days
  - variety: complete tasks of N different types
  - accuracy: get N tasks approved without rejection
  - challenge: complete N daily challenges
"""
import random
from datetime import datetime, timezone, timedelta, date
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.database import get_db
from models.db import (
    UserDB, ActiveQuestDB, QuestProgressDB, CreditTransactionDB,
)
from models.schemas import (
    QuestOut, QuestProgressOut, WeeklyQuestsOut,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/quests", tags=["quests"])


# ─── Quest template library ────────────────────────────────────────────────

QUEST_TEMPLATES = [
    # Volume quests (complete N tasks)
    {
        "quest_key": "volume_5",
        "title": "Getting Started",
        "description": "Complete 5 tasks this week. Any type counts!",
        "icon": "📋",
        "quest_type": "volume",
        "target_value": 5,
        "xp_reward": 50,
        "credits_reward": 10,
        "difficulty": "easy",
    },
    {
        "quest_key": "volume_15",
        "title": "Steady Worker",
        "description": "Complete 15 tasks this week. Stay consistent!",
        "icon": "🔨",
        "quest_type": "volume",
        "target_value": 15,
        "xp_reward": 120,
        "credits_reward": 25,
        "difficulty": "medium",
    },
    {
        "quest_key": "volume_30",
        "title": "Productivity Machine",
        "description": "Complete 30 tasks this week. Only the dedicated reach this.",
        "icon": "⚡",
        "quest_type": "volume",
        "target_value": 30,
        "xp_reward": 250,
        "credits_reward": 50,
        "difficulty": "hard",
    },

    # Streak quests
    {
        "quest_key": "streak_3",
        "title": "Three-Day Streak",
        "description": "Complete at least one task for 3 consecutive days.",
        "icon": "🔥",
        "quest_type": "streak",
        "target_value": 3,
        "xp_reward": 40,
        "credits_reward": 8,
        "difficulty": "easy",
    },
    {
        "quest_key": "streak_5",
        "title": "Five-Day Streak",
        "description": "Complete at least one task for 5 consecutive days.",
        "icon": "🔥",
        "quest_type": "streak",
        "target_value": 5,
        "xp_reward": 80,
        "credits_reward": 15,
        "difficulty": "medium",
    },
    {
        "quest_key": "streak_7",
        "title": "Perfect Week",
        "description": "Complete at least one task every day this week.",
        "icon": "🌟",
        "quest_type": "streak",
        "target_value": 7,
        "xp_reward": 150,
        "credits_reward": 30,
        "difficulty": "hard",
    },

    # Variety quests
    {
        "quest_key": "variety_3",
        "title": "Jack of Trades",
        "description": "Complete tasks of 3 different types this week.",
        "icon": "🎨",
        "quest_type": "variety",
        "target_value": 3,
        "xp_reward": 60,
        "credits_reward": 12,
        "difficulty": "easy",
    },
    {
        "quest_key": "variety_5",
        "title": "Renaissance Worker",
        "description": "Complete tasks of 5 different types this week.",
        "icon": "🌈",
        "quest_type": "variety",
        "target_value": 5,
        "xp_reward": 100,
        "credits_reward": 20,
        "difficulty": "medium",
    },

    # Accuracy quests
    {
        "quest_key": "accuracy_5",
        "title": "Precision Player",
        "description": "Get 5 tasks approved without a single rejection.",
        "icon": "🎯",
        "quest_type": "accuracy",
        "target_value": 5,
        "xp_reward": 70,
        "credits_reward": 15,
        "difficulty": "medium",
    },
    {
        "quest_key": "accuracy_10",
        "title": "Flawless Execution",
        "description": "Get 10 tasks approved without a single rejection.",
        "icon": "💎",
        "quest_type": "accuracy",
        "target_value": 10,
        "xp_reward": 160,
        "credits_reward": 35,
        "difficulty": "hard",
    },

    # Challenge quests
    {
        "quest_key": "challenge_3",
        "title": "Challenge Accepted",
        "description": "Complete 3 daily challenges this week.",
        "icon": "🏆",
        "quest_type": "challenge",
        "target_value": 3,
        "xp_reward": 60,
        "credits_reward": 12,
        "difficulty": "easy",
    },
    {
        "quest_key": "challenge_5",
        "title": "Challenge Master",
        "description": "Complete 5 daily challenges this week.",
        "icon": "👑",
        "quest_type": "challenge",
        "target_value": 5,
        "xp_reward": 100,
        "credits_reward": 20,
        "difficulty": "medium",
    },
]


# ─── Helpers ────────────────────────────────────────────────────────────────

def _current_week_start() -> date:
    """Return Monday of the current week (UTC)."""
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=today.weekday())


def _current_week_end() -> date:
    return _current_week_start() + timedelta(days=6)


async def ensure_weekly_quests(db: AsyncSession) -> list[ActiveQuestDB]:
    """Ensure quests exist for the current week. Creates them if not."""
    week_start = _current_week_start()
    week_end = _current_week_end()

    result = await db.execute(
        select(ActiveQuestDB).where(ActiveQuestDB.week_start == week_start)
    )
    existing = result.scalars().all()
    if existing:
        return list(existing)

    # Select 4 quests for the week: 1 easy, 2 medium, 1 hard
    easy = [t for t in QUEST_TEMPLATES if t["difficulty"] == "easy"]
    medium = [t for t in QUEST_TEMPLATES if t["difficulty"] == "medium"]
    hard = [t for t in QUEST_TEMPLATES if t["difficulty"] == "hard"]

    selected = []
    if easy:
        selected.append(random.choice(easy))
    for _ in range(2):
        if medium:
            pick = random.choice(medium)
            medium = [m for m in medium if m["quest_key"] != pick["quest_key"]]
            selected.append(pick)
    if hard:
        selected.append(random.choice(hard))

    quests = []
    for tmpl in selected:
        quest = ActiveQuestDB(
            quest_key=tmpl["quest_key"],
            title=tmpl["title"],
            description=tmpl["description"],
            icon=tmpl["icon"],
            quest_type=tmpl["quest_type"],
            target_value=tmpl["target_value"],
            xp_reward=tmpl["xp_reward"],
            credits_reward=tmpl["credits_reward"],
            difficulty=tmpl["difficulty"],
            week_start=week_start,
            week_end=week_end,
        )
        db.add(quest)
        quests.append(quest)

    await db.flush()
    logger.info("quests.weekly_generated", count=len(quests), week_start=str(week_start))
    return quests


async def _get_or_create_progress(
    db: AsyncSession, quest_id, user_id,
) -> QuestProgressDB:
    """Get or lazily create a progress record for a user on a quest."""
    result = await db.execute(
        select(QuestProgressDB).where(
            QuestProgressDB.quest_id == quest_id,
            QuestProgressDB.user_id == user_id,
        )
    )
    progress = result.scalar_one_or_none()
    if progress:
        return progress

    progress = QuestProgressDB(
        quest_id=quest_id,
        user_id=user_id,
        current_value=0,
        extra_data={},
    )
    db.add(progress)
    await db.flush()
    return progress


# ─── Quest progress tracking (called from task submission) ──────────────────

async def update_quest_progress(
    db: AsyncSession,
    user_id: str,
    task_type: str,
    streak_days: int,
) -> None:
    """Update quest progress for a worker after task submission.

    Called from worker.py's submit_task endpoint. Updates relevant quests:
    - volume: +1 for any task
    - streak: set to current streak_days
    - variety: add task_type to tracked set
    """
    week_start = _current_week_start()

    result = await db.execute(
        select(ActiveQuestDB).where(ActiveQuestDB.week_start == week_start)
    )
    quests = result.scalars().all()
    now = datetime.now(timezone.utc)

    for quest in quests:
        progress = await _get_or_create_progress(db, quest.id, user_id)
        if progress.is_complete:
            continue  # already done

        if quest.quest_type == "volume":
            progress.current_value += 1

        elif quest.quest_type == "streak":
            # Streak value = max of current streak_days seen this week
            progress.current_value = max(progress.current_value, streak_days)

        elif quest.quest_type == "variety":
            # Track unique task types in metadata
            meta = progress.extra_data or {}
            task_types = set(meta.get("task_types", []))
            task_types.add(task_type)
            meta["task_types"] = sorted(task_types)
            progress.extra_data = meta
            progress.current_value = len(task_types)

        # Check completion
        if progress.current_value >= quest.target_value and not progress.is_complete:
            progress.is_complete = True
            progress.completed_at = now
            logger.info(
                "quest.completed",
                user_id=str(user_id),
                quest_key=quest.quest_key,
            )


async def update_quest_on_approval(db: AsyncSession, user_id: str) -> None:
    """Update accuracy quests when a task is approved."""
    week_start = _current_week_start()
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(ActiveQuestDB).where(
            ActiveQuestDB.week_start == week_start,
            ActiveQuestDB.quest_type == "accuracy",
        )
    )
    quests = result.scalars().all()

    for quest in quests:
        progress = await _get_or_create_progress(db, quest.id, user_id)
        if progress.is_complete:
            continue
        progress.current_value += 1
        if progress.current_value >= quest.target_value:
            progress.is_complete = True
            progress.completed_at = now


async def reset_accuracy_quest_on_rejection(db: AsyncSession, user_id: str) -> None:
    """Reset accuracy quest progress when a task is rejected.

    The accuracy quest requires N consecutive approvals without rejection,
    so any rejection resets the counter to 0.
    """
    week_start = _current_week_start()

    result = await db.execute(
        select(ActiveQuestDB).where(
            ActiveQuestDB.week_start == week_start,
            ActiveQuestDB.quest_type == "accuracy",
        )
    )
    quests = result.scalars().all()

    for quest in quests:
        progress = await _get_or_create_progress(db, quest.id, user_id)
        if progress.is_complete:
            continue  # already completed — don't reset
        if progress.current_value > 0:
            progress.current_value = 0
            logger.info("quest.accuracy_reset", user_id=str(user_id), quest_key=quest.quest_key)


async def update_quest_on_challenge_complete(db: AsyncSession, user_id: str) -> None:
    """Update challenge-type quests when a daily challenge is completed."""
    week_start = _current_week_start()
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(ActiveQuestDB).where(
            ActiveQuestDB.week_start == week_start,
            ActiveQuestDB.quest_type == "challenge",
        )
    )
    quests = result.scalars().all()

    for quest in quests:
        progress = await _get_or_create_progress(db, quest.id, user_id)
        if progress.is_complete:
            continue
        progress.current_value += 1
        if progress.current_value >= quest.target_value:
            progress.is_complete = True
            progress.completed_at = now


# ─── Endpoints ──────────────────────────────────────────────────────────────

@router.get("", response_model=WeeklyQuestsOut)
async def get_weekly_quests(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get this week's quests with the worker's progress."""
    user_res = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_res.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Only workers can view quests")

    quests = await ensure_weekly_quests(db)
    await db.commit()  # persist if just created

    quest_progress = []
    total_completed = 0
    total_claimed = 0

    for quest in quests:
        progress = await _get_or_create_progress(db, quest.id, user_id)
        await db.flush()

        pct = min(100.0, (progress.current_value / quest.target_value) * 100) if quest.target_value > 0 else 0

        quest_progress.append(QuestProgressOut(
            quest=QuestOut.model_validate(quest),
            current_value=progress.current_value,
            target_value=quest.target_value,
            is_complete=progress.is_complete,
            is_claimed=progress.is_claimed,
            progress_pct=round(pct, 1),
        ))

        if progress.is_complete:
            total_completed += 1
        if progress.is_claimed:
            total_claimed += 1

    await db.commit()  # persist any new progress records

    return WeeklyQuestsOut(
        quests=quest_progress,
        week_start=_current_week_start(),
        week_end=_current_week_end(),
        total_completed=total_completed,
        total_claimed=total_claimed,
    )


@router.post("/{quest_id}/claim")
async def claim_quest_reward(
    quest_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Claim the reward for a completed quest."""
    quest_res = await db.execute(
        select(ActiveQuestDB).where(ActiveQuestDB.id == quest_id)
    )
    quest = quest_res.scalar_one_or_none()
    if not quest:
        raise HTTPException(status_code=404, detail="Quest not found")

    progress = await _get_or_create_progress(db, quest.id, user_id)

    if not progress.is_complete:
        raise HTTPException(status_code=400, detail="Quest not yet completed")
    if progress.is_claimed:
        raise HTTPException(status_code=409, detail="Reward already claimed")

    now = datetime.now(timezone.utc)
    progress.is_claimed = True
    progress.claimed_at = now

    # Award XP
    user_res = await db.execute(select(UserDB).where(UserDB.id == user_id).with_for_update())
    user = user_res.scalar_one_or_none()
    if user:
        user.worker_xp += quest.xp_reward
        from routers.worker import compute_level
        new_level, _ = compute_level(user.worker_xp)
        user.worker_level = new_level

        # Award credits
        user.credits += quest.credits_reward

        # Track league XP too
        try:
            from routers.leagues import add_league_xp
            await add_league_xp(db, str(user_id), quest.xp_reward)
        except Exception:
            logger.warning("quest.league_xp_failed", user_id=str(user_id), exc_info=True)

    # Credit transaction for audit
    txn = CreditTransactionDB(
        user_id=user_id,
        amount=quest.credits_reward,
        type="quest_reward",
        description=f"Quest completed: {quest.title}",
    )
    db.add(txn)

    await db.commit()

    logger.info(
        "quest.claimed",
        user_id=str(user_id),
        quest_key=quest.quest_key,
        xp=quest.xp_reward,
        credits=quest.credits_reward,
    )

    return {
        "claimed": True,
        "xp_reward": quest.xp_reward,
        "credits_reward": quest.credits_reward,
        "quest_title": quest.title,
    }
