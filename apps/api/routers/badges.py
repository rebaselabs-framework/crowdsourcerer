"""Worker badges / achievements system."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.database import get_db
from models.db import UserDB, WorkerBadgeDB
from models.schemas import BadgeOut, WorkerBadgesOut

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/worker/badges", tags=["badges"])


# ─── Badge Definitions ────────────────────────────────────────────────────

@dataclass
class BadgeDef:
    badge_id: str
    name: str
    description: str
    icon: str


ALL_BADGES: list[BadgeDef] = [
    # ── Task milestones ────────────────────────────────────────────────────
    BadgeDef("first_task",       "First Task",         "Complete your first task",                        "🎯"),
    BadgeDef("tasks_10",         "Getting Started",    "Complete 10 tasks",                               "⭐"),
    BadgeDef("tasks_50",         "Contributor",        "Complete 50 tasks",                               "🌟"),
    BadgeDef("tasks_100",        "Century Worker",     "Complete 100 tasks",                              "💯"),
    BadgeDef("tasks_500",        "Veteran",            "Complete 500 tasks",                              "🏆"),
    BadgeDef("tasks_1000",       "Elite Worker",       "Complete 1,000 tasks",                            "👑"),

    # ── Streak achievements ────────────────────────────────────────────────
    BadgeDef("streak_3",         "On a Roll",          "Maintain a 3-day streak",                         "🔥"),
    BadgeDef("streak_7",         "Week Warrior",       "Maintain a 7-day streak",                         "🔥🔥"),
    BadgeDef("streak_30",        "Monthly Grind",      "Maintain a 30-day streak",                        "🔥🔥🔥"),

    # ── XP / Level milestones ─────────────────────────────────────────────
    BadgeDef("level_5",          "Rising Star",        "Reach level 5",                                   "🌠"),
    BadgeDef("level_10",         "Expert",             "Reach level 10",                                  "🎖️"),
    BadgeDef("level_15",         "Master",             "Reach level 15",                                  "🏅"),
    BadgeDef("level_20",         "Divine",             "Reach the maximum level 20",                      "✨"),

    # ── Accuracy badges ───────────────────────────────────────────────────
    BadgeDef("accuracy_90",      "Sharp Eye",          "Achieve 90%+ accuracy on gold standard tasks",    "🎯"),
    BadgeDef("accuracy_95",      "Precision",          "Achieve 95%+ accuracy on gold standard tasks",    "🔬"),
    BadgeDef("accuracy_100",     "Perfection",         "Achieve 100% accuracy on gold standard tasks",    "💎"),

    # ── Reliability badges ────────────────────────────────────────────────
    BadgeDef("reliability_90",   "Reliable",           "Achieve 90%+ reliability (rarely releases tasks)", "⚓"),
    BadgeDef("reliability_100",  "Ironclad",           "100% reliability — never released a claimed task", "🛡️"),

    # ── League promotions ────────────────────────────────────────────────
    BadgeDef("league_silver",    "Silver League",      "Promoted to the Silver league",                    "🥈"),
    BadgeDef("league_gold",      "Gold League",        "Promoted to the Gold league",                      "🥇"),
    BadgeDef("league_platinum",  "Platinum League",    "Promoted to the Platinum league",                  "💠"),
    BadgeDef("league_diamond",   "Diamond League",     "Promoted to the Diamond league",                   "💎"),
    BadgeDef("league_obsidian",  "Obsidian League",    "Reached the legendary Obsidian league",            "🖤"),
    BadgeDef("league_champion",  "Season Champion",    "Finished #1 in your league group",                 "👑"),

    # ── Special ───────────────────────────────────────────────────────────
    BadgeDef("daily_challenge",  "Daily Challenger",   "Complete your first daily challenge",              "📅"),
    BadgeDef("challenge_7",      "Challenge Champion", "Complete daily challenges 7 days in a row",       "🏆"),
    BadgeDef("early_adopter",    "Early Adopter",      "One of the first 100 workers on the platform",    "🚀"),
]

_BADGE_MAP: dict[str, BadgeDef] = {b.badge_id: b for b in ALL_BADGES}


def _badge_to_out(
    badge_def: BadgeDef,
    earned_at: Optional[datetime] = None,
    rarity: Optional[float] = None,
) -> BadgeOut:
    return BadgeOut(
        badge_id=badge_def.badge_id,
        name=badge_def.name,
        description=badge_def.description,
        icon=badge_def.icon,
        earned_at=earned_at,
        earned=earned_at is not None,
        rarity=rarity,
    )


# ─── Check which badges a user should have ────────────────────────────────

def compute_eligible_badge_ids(
    tasks_completed: int,
    streak_days: int,
    level: int,
    accuracy: Optional[float],
    reliability: Optional[float],
    challenge_completions: int = 0,
    challenge_streak: int = 0,
    worker_rank: Optional[int] = None,
) -> set[str]:
    """Return set of badge_ids this worker has earned based on their stats."""
    earned: set[str] = set()

    # Task milestones
    if tasks_completed >= 1:    earned.add("first_task")
    if tasks_completed >= 10:   earned.add("tasks_10")
    if tasks_completed >= 50:   earned.add("tasks_50")
    if tasks_completed >= 100:  earned.add("tasks_100")
    if tasks_completed >= 500:  earned.add("tasks_500")
    if tasks_completed >= 1000: earned.add("tasks_1000")

    # Streak
    if streak_days >= 3:  earned.add("streak_3")
    if streak_days >= 7:  earned.add("streak_7")
    if streak_days >= 30: earned.add("streak_30")

    # Levels
    if level >= 5:  earned.add("level_5")
    if level >= 10: earned.add("level_10")
    if level >= 15: earned.add("level_15")
    if level >= 20: earned.add("level_20")

    # Accuracy
    if accuracy is not None:
        if accuracy >= 0.90: earned.add("accuracy_90")
        if accuracy >= 0.95: earned.add("accuracy_95")
        if accuracy >= 1.00: earned.add("accuracy_100")

    # Reliability
    if reliability is not None:
        if reliability >= 0.90: earned.add("reliability_90")
        if reliability >= 1.00: earned.add("reliability_100")

    # Challenges
    if challenge_completions >= 1: earned.add("daily_challenge")
    if challenge_streak >= 7:      earned.add("challenge_7")

    # Early adopter (set by admin, not computed here)

    return earned


async def award_new_badges(
    user: UserDB,
    db: AsyncSession,
    challenge_completions: int = 0,
    challenge_streak: int = 0,
) -> list[str]:
    """Check for newly-earned badges and insert them. Returns list of new badge_ids."""
    from routers.worker import compute_level  # avoid circular import

    level, _ = compute_level(user.worker_xp)

    eligible = compute_eligible_badge_ids(
        tasks_completed=user.worker_tasks_completed,
        streak_days=user.worker_streak_days,
        level=level,
        accuracy=user.worker_accuracy,
        reliability=user.worker_reliability,
        challenge_completions=challenge_completions,
        challenge_streak=challenge_streak,
    )

    # Fetch already-earned badge IDs
    result = await db.execute(
        select(WorkerBadgeDB.badge_id).where(WorkerBadgeDB.user_id == user.id)
    )
    already_earned = {row[0] for row in result.all()}

    new_badges = eligible - already_earned
    now = datetime.now(timezone.utc)
    for badge_id in new_badges:
        db.add(WorkerBadgeDB(user_id=user.id, badge_id=badge_id, earned_at=now))

    if new_badges:
        logger.info("badges_awarded", user_id=str(user.id), badges=list(new_badges))

    return list(new_badges)


# ─── League badge helpers (called from process_season_end) ───────────

# Map tier names to badge IDs (bronze has no badge — it's the starting tier)
_TIER_BADGE_MAP: dict[str, str] = {
    "silver": "league_silver",
    "gold": "league_gold",
    "platinum": "league_platinum",
    "diamond": "league_diamond",
    "obsidian": "league_obsidian",
}


async def award_league_badges(
    user: UserDB,
    db: AsyncSession,
    new_tier: str,
    final_rank: int,
) -> list[str]:
    """Award league promotion badges after season end.

    Called from ``process_season_end`` for promoted workers and #1 finishers.
    Returns list of newly awarded badge IDs.
    """
    new_badge_ids: list[str] = []
    now = datetime.now(timezone.utc)

    # Fetch already-earned badge IDs for this user
    result = await db.execute(
        select(WorkerBadgeDB.badge_id).where(WorkerBadgeDB.user_id == user.id)
    )
    already_earned = {row[0] for row in result.all()}

    # Award tier badge if applicable
    tier_badge = _TIER_BADGE_MAP.get(new_tier)
    if tier_badge and tier_badge not in already_earned:
        db.add(WorkerBadgeDB(user_id=user.id, badge_id=tier_badge, earned_at=now))
        new_badge_ids.append(tier_badge)

    # Award champion badge for #1 finisher
    if final_rank == 1 and "league_champion" not in already_earned:
        db.add(WorkerBadgeDB(user_id=user.id, badge_id="league_champion", earned_at=now))
        new_badge_ids.append("league_champion")

    if new_badge_ids:
        logger.info("league_badges_awarded", user_id=str(user.id), badges=new_badge_ids)

    return new_badge_ids


# ─── Routes ───────────────────────────────────────────────────────────────

@router.get("", response_model=WorkerBadgesOut)
async def get_my_badges(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get all earned and locked badges for the current worker, with rarity stats."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Not enrolled as a worker.")

    # Total worker count for rarity calculation
    total_workers = await db.scalar(
        select(func.count()).select_from(UserDB).where(
            UserDB.role.in_(["worker", "both"]),
            UserDB.worker_tasks_completed > 0,
        )
    ) or 1  # avoid division by zero

    # Count how many workers have each badge
    rarity_result = await db.execute(
        select(WorkerBadgeDB.badge_id, func.count(WorkerBadgeDB.badge_id))
        .group_by(WorkerBadgeDB.badge_id)
    )
    badge_counts: dict[str, int] = {row[0]: row[1] for row in rarity_result.all()}

    # Fetch earned badges for this user
    result = await db.execute(
        select(WorkerBadgeDB).where(WorkerBadgeDB.user_id == user_id)
    )
    earned_rows = result.scalars().all()
    earned_map = {b.badge_id: b.earned_at for b in earned_rows}

    earned_out: list[BadgeOut] = []
    locked_out: list[BadgeOut] = []

    for badge_def in ALL_BADGES:
        count = badge_counts.get(badge_def.badge_id, 0)
        rarity = round(count / total_workers * 100, 1) if total_workers > 0 else 0.0
        if badge_def.badge_id in earned_map:
            earned_out.append(_badge_to_out(badge_def, earned_map[badge_def.badge_id], rarity=rarity))
        else:
            locked_out.append(_badge_to_out(badge_def, None, rarity=rarity))

    return WorkerBadgesOut(
        earned=earned_out,
        locked=locked_out,
        total_earned=len(earned_out),
        total_workers=total_workers,
    )


@router.get("/user/{target_user_id}", response_model=WorkerBadgesOut)
async def get_user_badges(
    target_user_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get badges for a specific worker (public view), with rarity stats."""
    result = await db.execute(select(UserDB).where(UserDB.id == target_user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("worker", "both"):
        raise HTTPException(status_code=404, detail="Worker not found")

    # Total workers and rarity counts
    total_workers = await db.scalar(
        select(func.count()).select_from(UserDB).where(
            UserDB.role.in_(["worker", "both"]),
            UserDB.worker_tasks_completed > 0,
        )
    ) or 1
    rarity_result = await db.execute(
        select(WorkerBadgeDB.badge_id, func.count(WorkerBadgeDB.badge_id))
        .group_by(WorkerBadgeDB.badge_id)
    )
    badge_counts: dict[str, int] = {row[0]: row[1] for row in rarity_result.all()}

    result = await db.execute(
        select(WorkerBadgeDB).where(WorkerBadgeDB.user_id == target_user_id)
    )
    earned_rows = result.scalars().all()
    earned_map = {b.badge_id: b.earned_at for b in earned_rows}

    earned_out: list[BadgeOut] = []
    locked_out: list[BadgeOut] = []

    for badge_def in ALL_BADGES:
        count = badge_counts.get(badge_def.badge_id, 0)
        rarity = round(count / total_workers * 100, 1) if total_workers > 0 else 0.0
        if badge_def.badge_id in earned_map:
            earned_out.append(_badge_to_out(badge_def, earned_map[badge_def.badge_id], rarity=rarity))
        else:
            locked_out.append(_badge_to_out(badge_def, None, rarity=rarity))

    return WorkerBadgesOut(
        earned=earned_out,
        locked=locked_out,
        total_earned=len(earned_out),
        total_workers=total_workers,
    )
