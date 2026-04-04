"""Leagues — weekly competitive seasons with tier promotion/demotion.

Workers compete in groups of ~30 at the same tier. Top performers promote
to the next tier, bottom performers demote. Modeled after Duolingo leagues,
which increase engagement by 25%.

League tiers: Bronze → Silver → Gold → Platinum → Diamond → Obsidian
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta, date
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, and_

from core.auth import get_current_user_id
from core.database import get_db
from models.db import (
    UserDB, LeagueSeasonDB, LeagueGroupDB, LeagueGroupMemberDB,
    LEAGUE_TIERS,
)
from models.schemas import (
    LeagueTierInfo, LeagueStandingEntry, LeagueGroupOut,
    LeagueSeasonOut, LeagueCurrentOut, LeagueHistoryEntry,
    LeagueHistoryOut,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/leagues", tags=["leagues"])

# ─── Tier metadata ──────────────────────────────────────────────────────────

GROUP_SIZE = 30          # target workers per group
PROMO_SLOTS = 5          # top N promote
DEMO_SLOTS = 5           # bottom N demote

TIER_META: dict[str, dict] = {
    "bronze":   {"name": "Bronze League",   "icon": "🥉", "color": "amber",    "order": 0},
    "silver":   {"name": "Silver League",   "icon": "🥈", "color": "gray",     "order": 1},
    "gold":     {"name": "Gold League",     "icon": "🥇", "color": "yellow",   "order": 2},
    "platinum": {"name": "Platinum League", "icon": "💎", "color": "cyan",     "order": 3},
    "diamond":  {"name": "Diamond League",  "icon": "💠", "color": "blue",     "order": 4},
    "obsidian": {"name": "Obsidian League", "icon": "🖤", "color": "slate",    "order": 5},
}


def _tier_info(tier: str) -> dict:
    return TIER_META.get(tier, TIER_META["bronze"])


# ─── Helpers ────────────────────────────────────────────────────────────────

def _current_week_start() -> date:
    """Return Monday of the current week (UTC)."""
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=today.weekday())  # weekday() 0=Mon


def _current_week_end() -> date:
    """Return Sunday of the current week (UTC)."""
    return _current_week_start() + timedelta(days=6)


async def get_or_create_season(db: AsyncSession) -> LeagueSeasonDB:
    """Get the active season for the current week, creating it if needed."""
    week_start = _current_week_start()
    result = await db.execute(
        select(LeagueSeasonDB).where(LeagueSeasonDB.week_start == week_start)
    )
    season = result.scalar_one_or_none()
    if season:
        return season

    season = LeagueSeasonDB(
        week_start=week_start,
        week_end=_current_week_end(),
        status="active",
    )
    db.add(season)
    await db.flush()
    return season


async def _find_or_create_group(
    db: AsyncSession, season: LeagueSeasonDB, tier: str
) -> LeagueGroupDB:
    """Find a group in the given tier with room, or create a new one."""
    # Find groups in this tier with < GROUP_SIZE members
    result = await db.execute(
        select(LeagueGroupDB)
        .where(
            LeagueGroupDB.season_id == season.id,
            LeagueGroupDB.tier == tier,
        )
        .order_by(LeagueGroupDB.group_number)
    )
    groups = result.scalars().all()

    for group in groups:
        count_res = await db.execute(
            select(func.count(LeagueGroupMemberDB.id))
            .where(LeagueGroupMemberDB.group_id == group.id)
        )
        count = count_res.scalar() or 0
        if count < GROUP_SIZE:
            return group

    # All full (or none exist) — create new group
    next_num = (len(groups) + 1) if groups else 1
    group = LeagueGroupDB(
        season_id=season.id,
        tier=tier,
        group_number=next_num,
    )
    db.add(group)
    await db.flush()
    return group


async def _get_group_standings(
    db: AsyncSession, group_id: UUID, caller_id: Optional[str] = None,
) -> list[LeagueStandingEntry]:
    """Build ranked standings for a group."""
    result = await db.execute(
        select(LeagueGroupMemberDB, UserDB)
        .join(UserDB, LeagueGroupMemberDB.user_id == UserDB.id)
        .where(LeagueGroupMemberDB.group_id == group_id)
        .order_by(desc(LeagueGroupMemberDB.xp_earned), UserDB.name)
    )
    rows = result.all()
    total = len(rows)
    entries = []
    for i, (member, user) in enumerate(rows):
        rank = i + 1
        # Determine zone
        if rank <= PROMO_SLOTS:
            zone = "promo"
        elif total > DEMO_SLOTS and rank > total - DEMO_SLOTS:
            zone = "demo"
        else:
            zone = "safe"

        entries.append(LeagueStandingEntry(
            rank=rank,
            user_id=user.id,
            name=user.name,
            worker_level=user.worker_level,
            xp_earned=member.xp_earned,
            is_me=(str(user.id) == str(caller_id)) if caller_id else False,
            zone=zone,
            result=member.result,
        ))
    return entries


# ─── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/tiers", response_model=list[LeagueTierInfo])
async def list_tiers():
    """List all league tiers with metadata."""
    return [
        LeagueTierInfo(
            tier=tier,
            name=TIER_META[tier]["name"],
            icon=TIER_META[tier]["icon"],
            color=TIER_META[tier]["color"],
            order=TIER_META[tier]["order"],
            promo_slots=PROMO_SLOTS,
            demo_slots=DEMO_SLOTS,
        )
        for tier in LEAGUE_TIERS
    ]


@router.get("/current", response_model=LeagueCurrentOut)
async def get_current_league(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get the current season and the caller's league group + standings."""
    season = await get_or_create_season(db)
    await db.commit()  # persist season if just created

    user_res = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if user has role = worker or both
    if user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Only workers can participate in leagues")

    season_out = LeagueSeasonOut(
        season_id=season.id,
        week_start=season.week_start,
        week_end=season.week_end,
        status=season.status,
    )

    # Days remaining in the season
    today = datetime.now(timezone.utc).date()
    days_remaining = max(0, (season.week_end - today).days)

    # Find the user's group in this season
    member_res = await db.execute(
        select(LeagueGroupMemberDB)
        .join(LeagueGroupDB, LeagueGroupMemberDB.group_id == LeagueGroupDB.id)
        .where(
            LeagueGroupDB.season_id == season.id,
            LeagueGroupMemberDB.user_id == user_id,
        )
    )
    member = member_res.scalar_one_or_none()

    if not member:
        return LeagueCurrentOut(
            season=season_out,
            group=None,
            my_rank=None,
            my_xp=0,
            my_tier=user.league_tier or "bronze",
            joined=False,
            days_remaining=days_remaining,
        )

    # Load the group and standings
    group_res = await db.execute(
        select(LeagueGroupDB).where(LeagueGroupDB.id == member.group_id)
    )
    group = group_res.scalar_one()
    standings = await _get_group_standings(db, group.id, caller_id=str(user_id))

    meta = _tier_info(group.tier)
    total_members = len(standings)
    my_rank = next(
        (e.rank for e in standings if e.is_me), None
    )

    group_out = LeagueGroupOut(
        group_id=group.id,
        tier=group.tier,
        tier_name=meta["name"],
        tier_icon=meta["icon"],
        standings=standings,
        total_members=total_members,
        promo_slots=PROMO_SLOTS,
        demo_slots=DEMO_SLOTS,
    )

    return LeagueCurrentOut(
        season=season_out,
        group=group_out,
        my_rank=my_rank,
        my_xp=member.xp_earned,
        my_tier=user.league_tier or "bronze",
        joined=True,
        days_remaining=days_remaining,
    )


@router.post("/join", response_model=LeagueCurrentOut)
async def join_league(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Join the current week's league. Places the worker in a group for their tier."""
    user_res = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Only workers can join leagues")

    season = await get_or_create_season(db)

    # Check if already joined
    existing = await db.execute(
        select(LeagueGroupMemberDB)
        .join(LeagueGroupDB, LeagueGroupMemberDB.group_id == LeagueGroupDB.id)
        .where(
            LeagueGroupDB.season_id == season.id,
            LeagueGroupMemberDB.user_id == user_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already joined this week's league")

    tier = user.league_tier or "bronze"
    group = await _find_or_create_group(db, season, tier)

    member = LeagueGroupMemberDB(
        group_id=group.id,
        user_id=user.id,
        xp_earned=0,
    )
    db.add(member)
    await db.commit()

    logger.info("league.joined", user_id=str(user.id), tier=tier, group_id=str(group.id))

    # Return the current state
    return await get_current_league(db=db, user_id=user_id)


@router.get("/history", response_model=LeagueHistoryOut)
async def get_league_history(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get past season results for the authenticated worker."""
    user_res = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    result = await db.execute(
        select(LeagueGroupMemberDB, LeagueGroupDB, LeagueSeasonDB)
        .join(LeagueGroupDB, LeagueGroupMemberDB.group_id == LeagueGroupDB.id)
        .join(LeagueSeasonDB, LeagueGroupDB.season_id == LeagueSeasonDB.id)
        .where(
            LeagueGroupMemberDB.user_id == user_id,
            LeagueSeasonDB.status == "completed",
        )
        .order_by(desc(LeagueSeasonDB.week_start))
        .limit(20)
    )
    rows = result.all()

    entries = []
    for member, group, season in rows:
        meta = _tier_info(group.tier)
        # Count members in this group for group_size
        count_res = await db.execute(
            select(func.count(LeagueGroupMemberDB.id))
            .where(LeagueGroupMemberDB.group_id == group.id)
        )
        group_size = count_res.scalar() or 0

        entries.append(LeagueHistoryEntry(
            season_id=season.id,
            week_start=season.week_start,
            week_end=season.week_end,
            tier=group.tier,
            tier_icon=meta["icon"],
            final_rank=member.final_rank or 0,
            xp_earned=member.xp_earned,
            result=member.result or "stayed",
            group_size=group_size,
        ))

    return LeagueHistoryOut(
        seasons=entries,
        current_tier=user.league_tier or "bronze",
    )


# ─── League XP tracking helper (called from worker task submission) ─────────

async def add_league_xp(db: AsyncSession, user_id: str, xp_amount: int) -> None:
    """Increment a worker's XP in their current league group.

    Called from the task submission flow after XP is awarded.
    If the worker hasn't joined this week's league, this is a no-op.
    """
    if xp_amount <= 0:
        return

    week_start = _current_week_start()

    # Find the member's group for this week
    result = await db.execute(
        select(LeagueGroupMemberDB)
        .join(LeagueGroupDB, LeagueGroupMemberDB.group_id == LeagueGroupDB.id)
        .join(LeagueSeasonDB, LeagueGroupDB.season_id == LeagueSeasonDB.id)
        .where(
            LeagueSeasonDB.week_start == week_start,
            LeagueGroupMemberDB.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        return  # not in a league this week

    member.xp_earned += xp_amount


# ─── Season processing (called from sweeper) ───────────────────────────────

async def process_season_end(session_factory) -> int:
    """Finalize the previous week's season: rank, promote, demote, create new season.

    Should be called once on Monday morning. Returns number of workers processed.
    """
    from core.database import AsyncSessionLocal

    current_week = _current_week_start()
    prev_week_start = current_week - timedelta(days=7)
    processed = 0

    async with session_factory() as db:
        try:
            # Find the previous week's season
            result = await db.execute(
                select(LeagueSeasonDB).where(
                    LeagueSeasonDB.week_start == prev_week_start,
                    LeagueSeasonDB.status == "active",
                ).with_for_update(skip_locked=True)
            )
            season = result.scalar_one_or_none()
            if not season:
                return 0  # already processed or no season last week

            season.status = "processing"
            await db.flush()

            # Load all groups for this season
            groups_res = await db.execute(
                select(LeagueGroupDB).where(LeagueGroupDB.season_id == season.id)
            )
            groups = groups_res.scalars().all()

            for group in groups:
                # Load members ordered by XP (desc)
                members_res = await db.execute(
                    select(LeagueGroupMemberDB)
                    .where(LeagueGroupMemberDB.group_id == group.id)
                    .order_by(desc(LeagueGroupMemberDB.xp_earned))
                )
                members = members_res.scalars().all()
                total = len(members)
                if total == 0:
                    continue

                tier_idx = LEAGUE_TIERS.index(group.tier)

                for rank_0, member in enumerate(members):
                    rank = rank_0 + 1
                    member.final_rank = rank

                    # Determine result
                    if rank <= PROMO_SLOTS and tier_idx < len(LEAGUE_TIERS) - 1:
                        member.result = "promoted"
                    elif total > DEMO_SLOTS and rank > total - DEMO_SLOTS and tier_idx > 0:
                        member.result = "demoted"
                    else:
                        member.result = "stayed"

                    # Update user's league tier
                    user_res = await db.execute(
                        select(UserDB).where(UserDB.id == member.user_id)
                    )
                    user = user_res.scalar_one_or_none()
                    if user:
                        if member.result == "promoted":
                            user.league_tier = LEAGUE_TIERS[tier_idx + 1]
                        elif member.result == "demoted":
                            user.league_tier = LEAGUE_TIERS[tier_idx - 1]
                        # "stayed" = no change

                    processed += 1

            season.status = "completed"
            await db.commit()

            logger.info(
                "league.season_processed",
                week_start=str(prev_week_start),
                processed=processed,
                groups=len(groups),
            )

        except Exception:
            logger.exception("league.season_processing_error")
            await db.rollback()

    return processed
