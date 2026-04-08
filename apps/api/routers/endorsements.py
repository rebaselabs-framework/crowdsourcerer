"""Worker endorsement endpoints.

Requesters can endorse workers after task completion.  Endorsements are
shown on the worker's public profile.
"""

from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from core.scopes import require_scope, SCOPE_TASKS_READ
from models.db import UserDB, TaskDB, TaskAssignmentDB, WorkerEndorsementDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/workers", tags=["endorsements"])


# ─── Request / Response schemas ───────────────────────────────────────────────

class EndorseRequest(BaseModel):
    task_id: UUID
    skill_tag: Optional[str] = Field(None, max_length=100)
    note: Optional[str] = Field(None, max_length=500)


class EndorsementOut(BaseModel):
    id: UUID
    skill_tag: Optional[str] = None
    note: Optional[str] = None
    created_at: str  # ISO string — never expose requester identity

    model_config = {"from_attributes": True}


class EndorsementListOut(BaseModel):
    items: list[EndorsementOut]
    total: int


class EndorsementCountOut(BaseModel):
    worker_id: UUID
    count: int


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/{worker_id}/endorse", status_code=201)
async def create_endorsement(
    worker_id: UUID,
    req: EndorseRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """Create an endorsement for a worker.

    The requester must have a completed task that was submitted by this worker.
    Only one endorsement per (worker, requester, task) combination is allowed.
    """
    # Verify the worker exists
    worker_result = await db.execute(select(UserDB).where(UserDB.id == worker_id))
    worker = worker_result.scalar_one_or_none()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    # Verify the task exists and belongs to the requester
    task_result = await db.execute(
        select(TaskDB).where(TaskDB.id == req.task_id, TaskDB.user_id == user_id)
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or does not belong to you")

    if task.status != "completed":
        raise HTTPException(
            status_code=400,
            detail="You can only endorse a worker for completed tasks",
        )

    # Verify the worker submitted work on this task
    assignment_result = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.task_id == req.task_id,
            TaskAssignmentDB.worker_id == worker_id,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
        )
    )
    assignment = assignment_result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(
            status_code=400,
            detail="This worker did not submit work on the specified task",
        )

    # Check for duplicate endorsement
    existing = await db.scalar(
        select(func.count()).where(
            WorkerEndorsementDB.worker_id == worker_id,
            WorkerEndorsementDB.requester_id == user_id,
            WorkerEndorsementDB.task_id == req.task_id,
        )
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="You have already endorsed this worker for this task",
        )

    endorsement = WorkerEndorsementDB(
        worker_id=worker_id,
        requester_id=user_id,
        task_id=req.task_id,
        skill_tag=req.skill_tag,
        note=req.note,
    )
    db.add(endorsement)
    try:
        await db.commit()
        await db.refresh(endorsement)
    except IntegrityError:
        # Two concurrent requests passed the count check — DB unique constraint caught it
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="You have already endorsed this worker for this task",
        )

    logger.info(
        "endorsement.created",
        endorsement_id=str(endorsement.id),
        worker_id=str(worker_id),
        requester_id=str(user_id),
    )

    return {
        "id": str(endorsement.id),
        "worker_id": str(worker_id),
        "skill_tag": endorsement.skill_tag,
        "note": endorsement.note,
        "created_at": endorsement.created_at.isoformat(),
    }


@router.get("/{worker_id}/endorsements", response_model=EndorsementListOut)
async def list_endorsements(
    worker_id: UUID,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List public endorsements for a worker.

    No authentication required. Requester identity is never exposed.
    """
    # Verify worker exists
    worker_result = await db.execute(select(UserDB).where(UserDB.id == worker_id))
    if not worker_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Worker not found")

    total = await db.scalar(
        select(func.count()).where(WorkerEndorsementDB.worker_id == worker_id)
    ) or 0

    rows_result = await db.execute(
        select(WorkerEndorsementDB)
        .where(WorkerEndorsementDB.worker_id == worker_id)
        .order_by(WorkerEndorsementDB.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = rows_result.scalars().all()

    items = [
        EndorsementOut(
            id=e.id,
            skill_tag=e.skill_tag,
            note=e.note,
            created_at=e.created_at.isoformat(),
        )
        for e in rows
    ]

    return EndorsementListOut(items=items, total=total)


@router.get("/{worker_id}/endorsements/count", response_model=EndorsementCountOut)
async def get_endorsement_count(
    worker_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return the total number of endorsements for a worker. No auth required."""
    # Verify worker exists
    worker_result = await db.execute(select(UserDB).where(UserDB.id == worker_id))
    if not worker_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Worker not found")

    count = await db.scalar(
        select(func.count()).where(WorkerEndorsementDB.worker_id == worker_id)
    ) or 0

    return EndorsementCountOut(worker_id=worker_id, count=count)
