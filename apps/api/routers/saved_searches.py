"""Saved search / task alert endpoints for workers."""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from models.db import SavedSearchDB
from models.schemas import (
    SavedSearchCreateRequest,
    SavedSearchUpdateRequest,
    SavedSearchOut,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/worker/saved-searches", tags=["saved-searches"])

_MAX_SAVED_SEARCHES = 20


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[SavedSearchOut])
async def list_saved_searches(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List all saved searches for the authenticated worker."""
    result = await db.execute(
        select(SavedSearchDB)
        .where(SavedSearchDB.user_id == user_id)
        .order_by(SavedSearchDB.created_at.desc())
        .limit(_MAX_SAVED_SEARCHES)  # cap matches the per-user max — no user can have more
    )
    return result.scalars().all()


@router.post("", response_model=SavedSearchOut, status_code=201)
async def create_saved_search(
    req: SavedSearchCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Save a search filter set with optional task alert."""
    # Enforce per-user cap — use aggregate count instead of fetching all rows
    current_count = await db.scalar(
        select(func.count()).select_from(SavedSearchDB).where(SavedSearchDB.user_id == user_id)
    ) or 0
    if current_count >= _MAX_SAVED_SEARCHES:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {_MAX_SAVED_SEARCHES} saved searches reached",
        )

    saved = SavedSearchDB(
        user_id=user_id,
        name=req.name,
        filters=req.filters.model_dump(exclude_none=True),
        alert_enabled=req.alert_enabled,
        alert_frequency=req.alert_frequency,
        created_at=_utcnow(),
    )
    db.add(saved)
    await db.commit()
    await db.refresh(saved)
    logger.info("saved_search_created", user_id=user_id, name=req.name)
    return saved


@router.get("/{search_id}", response_model=SavedSearchOut)
async def get_saved_search(
    search_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(
        select(SavedSearchDB).where(
            SavedSearchDB.id == search_id,
            SavedSearchDB.user_id == user_id,
        )
    )
    saved = result.scalar_one_or_none()
    if not saved:
        raise HTTPException(status_code=404, detail="Saved search not found")
    return saved


@router.patch("/{search_id}", response_model=SavedSearchOut)
async def update_saved_search(
    search_id: UUID,
    req: SavedSearchUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(
        select(SavedSearchDB).where(
            SavedSearchDB.id == search_id,
            SavedSearchDB.user_id == user_id,
        )
    )
    saved = result.scalar_one_or_none()
    if not saved:
        raise HTTPException(status_code=404, detail="Saved search not found")

    if req.name is not None:
        saved.name = req.name
    if req.filters is not None:
        saved.filters = req.filters.model_dump(exclude_none=True)
    if req.alert_enabled is not None:
        saved.alert_enabled = req.alert_enabled
    if req.alert_frequency is not None:
        saved.alert_frequency = req.alert_frequency
    saved.updated_at = _utcnow()

    await db.commit()
    await db.refresh(saved)
    return saved


@router.delete("/{search_id}", status_code=204, response_model=None)
async def delete_saved_search(
    search_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(
        select(SavedSearchDB).where(
            SavedSearchDB.id == search_id,
            SavedSearchDB.user_id == user_id,
        )
    )
    saved = result.scalar_one_or_none()
    if not saved:
        raise HTTPException(status_code=404, detail="Saved search not found")
    await db.delete(saved)
    await db.commit()


# ─── Alert trigger (internal use) ─────────────────────────────────────────────

async def notify_matching_saved_searches(
    task_type: str,
    priority: str,
    reward_credits: Optional[int],
    db: AsyncSession,
) -> None:
    """Fire in-app notifications for workers whose saved search matches a new task.

    Called from the task creation path. Only fires for ``instant`` alerts.
    """
    from core.notify import create_notification, NotifType

    result = await db.execute(
        select(SavedSearchDB).where(
            SavedSearchDB.alert_enabled == True,  # noqa: E712
            SavedSearchDB.alert_frequency == "instant",
        ).limit(10_000)  # safety cap — at scale, alert fanout should move to a queue
    )
    searches = result.scalars().all()

    matched_users: set[str] = set()

    for search in searches:
        filters = search.filters or {}

        # Task type filter
        if "task_type" in filters and filters["task_type"] != task_type:
            continue

        # Priority filter
        if "priority" in filters and filters["priority"] != priority:
            continue

        # Reward filters
        if "min_reward" in filters and reward_credits is not None:
            if reward_credits < filters["min_reward"]:
                continue
        if "max_reward" in filters and reward_credits is not None:
            if reward_credits > filters["max_reward"]:
                continue

        uid = str(search.user_id)
        if uid in matched_users:
            continue
        matched_users.add(uid)

        # Bump match count + update last_notified_at
        search.match_count += 1
        search.last_notified_at = _utcnow()

        await create_notification(
            db=db,
            user_id=uid,
            type=NotifType.SYSTEM,
            title="New task matches your alert",
            body=f'A new "{task_type}" task is available that matches your saved search "{search.name}".',
            link="/dashboard/marketplace",
        )

    if matched_users:
        await db.commit()
        logger.info("saved_search_alerts_sent", count=len(matched_users))
