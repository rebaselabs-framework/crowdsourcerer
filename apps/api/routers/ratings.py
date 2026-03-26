"""Task rating / feedback endpoints.

After a requester approves a worker's submission they can leave a 1–5 star
rating with an optional comment.  Ratings are public on worker profiles and
drive the ``avg_feedback_score`` field on UserDB.
"""
from __future__ import annotations

import structlog
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.database import get_db
from core.notify import create_notification, NotifType
from core.scopes import require_scope, SCOPE_TASKS_WRITE, SCOPE_TASKS_READ
from models.db import (
    TaskDB, TaskRatingDB, TaskAssignmentDB, UserDB,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/tasks", tags=["ratings"])


# ─── Schemas ─────────────────────────────────────────────────────────────────

class RateTaskRequest(BaseModel):
    score: int = Field(..., ge=1, le=5, description="Star rating 1–5")
    comment: Optional[str] = Field(None, max_length=1000)
    submission_id: Optional[UUID] = None  # optional link to specific submission


class RatingOut(BaseModel):
    id: UUID
    task_id: UUID
    requester_id: UUID
    worker_id: UUID
    submission_id: Optional[UUID]
    score: int
    comment: Optional[str]
    created_at: str

    model_config = {"from_attributes": True}


class WorkerRatingSummary(BaseModel):
    avg_score: Optional[float]
    total_ratings: int
    distribution: dict  # {1: N, 2: N, ...}
    recent: list[RatingOut]


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _refresh_worker_avg(worker_id: UUID, db: AsyncSession) -> None:
    """Recalculate and persist avg_feedback_score for a worker using aggregate query."""
    agg_res = await db.execute(
        select(
            func.avg(TaskRatingDB.score).label("avg"),
            func.count(TaskRatingDB.id).label("cnt"),
        ).where(TaskRatingDB.worker_id == worker_id)
    )
    row = agg_res.one_or_none()
    if not row or not row.cnt:
        return
    avg = round(float(row.avg), 2)
    worker_res = await db.execute(select(UserDB).where(UserDB.id == worker_id))
    worker = worker_res.scalar_one_or_none()
    if worker:
        worker.avg_feedback_score = avg
        worker.total_ratings_received = row.cnt


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/{task_id}/rate", response_model=RatingOut, status_code=status.HTTP_201_CREATED)
async def rate_task(
    task_id: UUID,
    body: RateTaskRequest,
    user_id: UUID = Depends(require_scope(SCOPE_TASKS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Rate a worker's output for this task (requester only, once per task)."""
    task_res = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Only the task owner can rate
    if task.user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the task requester can leave a rating")

    # Task must be completed
    if task.status != "completed":
        raise HTTPException(
            status_code=400,
            detail="You can only rate completed tasks",
        )

    # Prevent duplicate rating
    existing_res = await db.execute(
        select(TaskRatingDB).where(
            TaskRatingDB.task_id == task_id,
            TaskRatingDB.requester_id == user_id,
        )
    )
    if existing_res.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="You have already rated this task")

    # Resolve the worker_id from the most recent approved/submitted assignment
    assignment_res = await db.execute(
        select(TaskAssignmentDB)
        .where(
            TaskAssignmentDB.task_id == task_id,
            TaskAssignmentDB.status.in_(["approved", "completed", "submitted"]),
        )
        .order_by(TaskAssignmentDB.completed_at.desc().nullslast())
        .limit(1)
    )
    assignment = assignment_res.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=400, detail="No worker found for this task to rate")
    worker_id: UUID = assignment.worker_id

    rating = TaskRatingDB(
        task_id=task_id,
        requester_id=user_id,
        worker_id=worker_id,
        submission_id=body.submission_id,
        score=body.score,
        comment=body.comment,
    )
    db.add(rating)
    await db.flush()

    # Refresh aggregate on worker (single aggregate query, not full table scan)
    await _refresh_worker_avg(worker_id, db)
    await db.commit()
    await db.refresh(rating)

    # Notify worker (fire-and-forget, non-fatal)
    try:
        star_str = "⭐" * body.score
        msg = f"You received a {star_str} ({body.score}/5) rating!"
        if body.comment:
            msg += f' "{body.comment[:100]}"'
        await create_notification(
            db=db,
            user_id=worker_id,
            type=NotifType.SYSTEM,
            title="New feedback rating",
            body=msg,
            link="/worker/ratings",
        )
    except Exception:
        logger.warning("ratings.notify_failed", task_id=str(task_id), exc_info=True)

    return RatingOut(
        id=rating.id,
        task_id=rating.task_id,
        requester_id=rating.requester_id,
        worker_id=rating.worker_id,
        submission_id=rating.submission_id,
        score=rating.score,
        comment=rating.comment,
        created_at=rating.created_at.isoformat(),
    )


@router.get("/{task_id}/rating", response_model=Optional[RatingOut])
async def get_task_rating(
    task_id: UUID,
    user_id: UUID = Depends(require_scope(SCOPE_TASKS_READ)),
    db: AsyncSession = Depends(get_db),
):
    """Get the rating left for this task (if any)."""
    task_res = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    rating_res = await db.execute(
        select(TaskRatingDB).where(
            TaskRatingDB.task_id == task_id,
            TaskRatingDB.requester_id == user_id,
        )
    )
    rating = rating_res.scalar_one_or_none()
    if not rating:
        return None

    return RatingOut(
        id=rating.id,
        task_id=rating.task_id,
        requester_id=rating.requester_id,
        worker_id=rating.worker_id,
        submission_id=rating.submission_id,
        score=rating.score,
        comment=rating.comment,
        created_at=rating.created_at.isoformat(),
    )


# ─── Worker-facing rating endpoints (different prefix) ───────────────────────

worker_router = APIRouter(prefix="/v1/workers", tags=["ratings"])


@worker_router.get("/me/ratings", response_model=WorkerRatingSummary)
async def my_ratings(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get my incoming feedback ratings summary (worker view)."""
    ratings_res = await db.execute(
        select(TaskRatingDB)
        .where(TaskRatingDB.worker_id == user_id)
        .order_by(TaskRatingDB.created_at.desc())
        .limit(50)
    )
    ratings = ratings_res.scalars().all()

    dist: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for r in ratings:
        dist[r.score] = dist.get(r.score, 0) + 1

    me_res = await db.execute(select(UserDB).where(UserDB.id == user_id))
    me = me_res.scalar_one_or_none()

    recent = [
        RatingOut(
            id=r.id,
            task_id=r.task_id,
            requester_id=r.requester_id,
            worker_id=r.worker_id,
            submission_id=r.submission_id,
            score=r.score,
            comment=r.comment,
            created_at=r.created_at.isoformat(),
        )
        for r in ratings[:20]
    ]

    return WorkerRatingSummary(
        avg_score=me.avg_feedback_score if me else None,
        total_ratings=me.total_ratings_received if me else 0,
        distribution=dist,
        recent=recent,
    )


@worker_router.get("/{worker_id}/ratings")
async def public_worker_ratings(
    worker_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Public endpoint: get ratings summary for a worker profile."""
    worker_res = await db.execute(select(UserDB).where(UserDB.id == worker_id))
    worker = worker_res.scalar_one_or_none()
    if not worker or not worker.profile_public:
        raise HTTPException(status_code=404, detail="Worker not found")

    ratings_res = await db.execute(
        select(TaskRatingDB)
        .where(TaskRatingDB.worker_id == worker_id)
        .order_by(TaskRatingDB.created_at.desc())
        .limit(20)
    )
    ratings = ratings_res.scalars().all()

    dist: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for r in ratings:
        dist[r.score] = dist.get(r.score, 0) + 1

    recent = [
        {"score": r.score, "comment": r.comment, "created_at": r.created_at.isoformat()}
        for r in ratings
    ]

    return {
        "avg_score": worker.avg_feedback_score,
        "total_ratings": worker.total_ratings_received,
        "distribution": dist,
        "recent": recent,
    }
