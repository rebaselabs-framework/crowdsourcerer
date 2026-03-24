"""Worker availability calendar and blackout management."""
from __future__ import annotations
import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from core.auth import get_current_user_id
from core.database import get_db
from models.db import UserDB, WorkerAvailabilityDB, WorkerBlackoutDB

router = APIRouter(prefix="/v1/worker/availability", tags=["availability"])

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ── Pydantic schemas ───────────────────────────────────────────────────────────

class AvailabilitySlotIn(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6)
    start_hour: int = Field(..., ge=0, le=23)
    end_hour: int = Field(..., ge=1, le=24)

    @validator("end_hour")
    def end_after_start(cls, v, values):
        if "start_hour" in values and v <= values["start_hour"]:
            raise ValueError("end_hour must be greater than start_hour")
        return v


class AvailabilitySlotOut(BaseModel):
    id: UUID
    day_of_week: int
    day_name: str
    start_hour: int
    end_hour: int

    class Config:
        from_attributes = True


class BlackoutIn(BaseModel):
    blackout_date: datetime.date
    reason: Optional[str] = None


class BlackoutOut(BaseModel):
    id: UUID
    blackout_date: datetime.date
    reason: Optional[str]
    created_at: datetime.datetime

    class Config:
        from_attributes = True


class SetAvailabilityIn(BaseModel):
    slots: List[AvailabilitySlotIn] = Field(..., description="All weekly slots (replaces existing)")


class AvailabilityResponse(BaseModel):
    slots: List[AvailabilitySlotOut]
    blackouts: List[BlackoutOut]
    timezone_note: str = "All hours are in UTC. Workers should set hours in their local timezone."


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_slot_out(s: WorkerAvailabilityDB) -> AvailabilitySlotOut:
    return AvailabilitySlotOut(
        id=s.id,
        day_of_week=s.day_of_week,
        day_name=DAY_NAMES[s.day_of_week],
        start_hour=s.start_hour,
        end_hour=s.end_hour,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=AvailabilityResponse)
async def get_my_availability(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get my weekly availability slots and blackout dates."""
    slots_result = await db.execute(
        select(WorkerAvailabilityDB)
        .where(WorkerAvailabilityDB.worker_id == UUID(user_id))
        .order_by(WorkerAvailabilityDB.day_of_week, WorkerAvailabilityDB.start_hour)
    )
    slots = slots_result.scalars().all()

    blackouts_result = await db.execute(
        select(WorkerBlackoutDB)
        .where(WorkerBlackoutDB.worker_id == UUID(user_id))
        .order_by(WorkerBlackoutDB.blackout_date)
    )
    blackouts = blackouts_result.scalars().all()

    return AvailabilityResponse(
        slots=[_build_slot_out(s) for s in slots],
        blackouts=list(blackouts),
    )


@router.put("", response_model=AvailabilityResponse)
async def set_availability(
    body: SetAvailabilityIn,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Replace all weekly availability slots."""
    if len(body.slots) > 56:  # max 8h/day * 7 days = 56
        raise HTTPException(status_code=400, detail="Too many slots (max 56)")

    await db.execute(
        delete(WorkerAvailabilityDB).where(WorkerAvailabilityDB.worker_id == UUID(user_id))
    )
    for slot in body.slots:
        db.add(WorkerAvailabilityDB(
            worker_id=UUID(user_id),
            day_of_week=slot.day_of_week,
            start_hour=slot.start_hour,
            end_hour=slot.end_hour,
        ))
    await db.commit()

    return await get_my_availability(user_id=user_id, db=db)


@router.get("/worker/{worker_id}", response_model=AvailabilityResponse)
async def get_worker_availability(
    worker_id: UUID,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get a specific worker's availability (visible to all authenticated users)."""
    worker_result = await db.execute(select(UserDB).where(UserDB.id == worker_id))
    worker = worker_result.scalar_one_or_none()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    slots_result = await db.execute(
        select(WorkerAvailabilityDB)
        .where(WorkerAvailabilityDB.worker_id == worker_id)
        .order_by(WorkerAvailabilityDB.day_of_week, WorkerAvailabilityDB.start_hour)
    )
    slots = slots_result.scalars().all()

    blackouts_result = await db.execute(
        select(WorkerBlackoutDB)
        .where(WorkerBlackoutDB.worker_id == worker_id)
        .order_by(WorkerBlackoutDB.blackout_date)
    )
    blackouts = blackouts_result.scalars().all()

    return AvailabilityResponse(
        slots=[_build_slot_out(s) for s in slots],
        blackouts=list(blackouts),
    )


@router.post("/blackouts", response_model=BlackoutOut, status_code=201)
async def add_blackout(
    body: BlackoutIn,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Add a blackout date (unavailable all day)."""
    existing_result = await db.execute(
        select(WorkerBlackoutDB).where(
            WorkerBlackoutDB.worker_id == UUID(user_id),
            WorkerBlackoutDB.blackout_date == body.blackout_date,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Blackout already exists for that date")

    bo = WorkerBlackoutDB(
        worker_id=UUID(user_id),
        blackout_date=body.blackout_date,
        reason=body.reason,
    )
    db.add(bo)
    await db.commit()
    await db.refresh(bo)
    return bo


@router.delete("/blackouts/{blackout_id}", status_code=204, response_model=None)
async def remove_blackout(
    blackout_id: UUID,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Remove a blackout date."""
    result = await db.execute(
        select(WorkerBlackoutDB).where(
            WorkerBlackoutDB.id == blackout_id,
            WorkerBlackoutDB.worker_id == UUID(user_id),
        )
    )
    bo = result.scalar_one_or_none()
    if not bo:
        raise HTTPException(status_code=404, detail="Blackout not found")
    await db.delete(bo)
    await db.commit()
