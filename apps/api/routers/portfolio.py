"""Worker portfolio / showcase endpoints.

Workers can pin up to 10 completed tasks to their public profile as a
portfolio of their best work.  Visitors to the worker's public profile see
the pinned items with task type, result summary, and worker caption.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import get_current_user_id
from core.database import get_db
from core.scopes import require_scope, SCOPE_TASKS_READ
from models.db import (
    TaskDB, UserDB, WorkerPortfolioItemDB, TaskAssignmentDB, TaskRatingDB
)

router = APIRouter(prefix="/v1/worker", tags=["portfolio"])
public_router = APIRouter(prefix="/v1/workers", tags=["portfolio"])

_MAX_PORTFOLIO = 10


# ─── Schemas ─────────────────────────────────────────────────────────────────

class PinTaskRequest(BaseModel):
    task_id: UUID
    caption: Optional[str] = Field(None, max_length=500)
    display_order: int = Field(default=0, ge=0)


class UpdatePinRequest(BaseModel):
    caption: Optional[str] = Field(None, max_length=500)
    display_order: Optional[int] = Field(None, ge=0)


class PortfolioItemOut(BaseModel):
    id: UUID
    task_id: UUID
    task_type: str
    task_title: Optional[str]
    caption: Optional[str]
    display_order: int
    pinned_at: str
    # Brief result summary
    result_snippet: Optional[str] = None
    # Rating for this task (if any)
    avg_rating: Optional[float] = None


def _result_snippet(task: TaskDB, max_chars: int = 200) -> Optional[str]:
    """Extract a short text snippet from task output."""
    if not task.output:
        return None
    output = task.output
    # try common fields
    for key in ("summary", "report", "text", "transcript", "result", "answer"):
        val = output.get(key)
        if isinstance(val, str) and val:
            return val[:max_chars]
    # fallback: stringify dict
    text = str(output)
    return text[:max_chars] if text else None


def _build_item(pin: WorkerPortfolioItemDB, db: Session) -> PortfolioItemOut:
    task = pin.task
    # Get avg rating for this task (worker_id = pin.worker_id)
    ratings = (
        db.query(TaskRatingDB.score)
        .filter(TaskRatingDB.task_id == pin.task_id, TaskRatingDB.worker_id == pin.worker_id)
        .all()
    )
    avg_rating = None
    if ratings:
        avg_rating = round(sum(r.score for r in ratings) / len(ratings), 1)

    return PortfolioItemOut(
        id=pin.id,
        task_id=pin.task_id,
        task_type=task.task_type if task else "unknown",
        task_title=getattr(task, "title", None) if task else None,
        caption=pin.caption,
        display_order=pin.display_order,
        pinned_at=pin.pinned_at.isoformat(),
        result_snippet=_result_snippet(task) if task else None,
        avg_rating=avg_rating,
    )


# ─── My portfolio (authenticated) ────────────────────────────────────────────

@router.post("/portfolio", response_model=PortfolioItemOut, status_code=status.HTTP_201_CREATED)
def pin_task(
    body: PinTaskRequest,
    user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Pin a completed task to my portfolio showcase."""
    # Verify task exists and worker was the assignee
    task = db.query(TaskDB).filter(TaskDB.id == body.task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status != "completed":
        raise HTTPException(status_code=400, detail="Only completed tasks can be pinned")

    # Worker must have been assigned (and submitted) this task
    assignment = (
        db.query(TaskAssignmentDB)
        .filter(
            TaskAssignmentDB.task_id == body.task_id,
            TaskAssignmentDB.worker_id == user_id,
        )
        .first()
    )
    # Also allow AI tasks created by the worker themselves (execution_mode='ai')
    is_ai_task_owner = (task.execution_mode == "ai" and str(task.user_id) == str(user_id))
    if not assignment and not is_ai_task_owner:
        raise HTTPException(status_code=403, detail="You did not work on this task")

    # Check duplicate
    existing = (
        db.query(WorkerPortfolioItemDB)
        .filter(
            WorkerPortfolioItemDB.worker_id == user_id,
            WorkerPortfolioItemDB.task_id == body.task_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="Task already pinned to portfolio")

    # Enforce max cap
    count = (
        db.query(WorkerPortfolioItemDB)
        .filter(WorkerPortfolioItemDB.worker_id == user_id)
        .count()
    )
    if count >= _MAX_PORTFOLIO:
        raise HTTPException(
            status_code=400,
            detail=f"Portfolio is full ({_MAX_PORTFOLIO} items max). Remove one first.",
        )

    pin = WorkerPortfolioItemDB(
        worker_id=user_id,
        task_id=body.task_id,
        caption=body.caption,
        display_order=body.display_order,
    )
    db.add(pin)
    db.commit()
    db.refresh(pin)

    return _build_item(pin, db)


@router.get("/portfolio", response_model=list[PortfolioItemOut])
def get_my_portfolio(
    user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Get my portfolio pins."""
    pins = (
        db.query(WorkerPortfolioItemDB)
        .filter(WorkerPortfolioItemDB.worker_id == user_id)
        .order_by(WorkerPortfolioItemDB.display_order, WorkerPortfolioItemDB.pinned_at)
        .all()
    )
    return [_build_item(p, db) for p in pins]


@router.patch("/portfolio/{pin_id}", response_model=PortfolioItemOut)
def update_pin(
    pin_id: UUID,
    body: UpdatePinRequest,
    user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Update caption or display order for a pinned task."""
    pin = db.query(WorkerPortfolioItemDB).filter(
        WorkerPortfolioItemDB.id == pin_id,
        WorkerPortfolioItemDB.worker_id == user_id,
    ).first()
    if not pin:
        raise HTTPException(status_code=404, detail="Pin not found")

    if body.caption is not None:
        pin.caption = body.caption
    if body.display_order is not None:
        pin.display_order = body.display_order
    db.commit()
    db.refresh(pin)

    return _build_item(pin, db)


@router.delete("/portfolio/{pin_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pin(
    pin_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Remove a task from my portfolio."""
    pin = db.query(WorkerPortfolioItemDB).filter(
        WorkerPortfolioItemDB.id == pin_id,
        WorkerPortfolioItemDB.worker_id == user_id,
    ).first()
    if not pin:
        raise HTTPException(status_code=404, detail="Pin not found")

    db.delete(pin)
    db.commit()


# ─── Public portfolio ─────────────────────────────────────────────────────────

@public_router.get("/{worker_id}/portfolio")
def public_portfolio(
    worker_id: UUID,
    db: Session = Depends(get_db),
):
    """Public portfolio for a worker profile page."""
    worker = db.query(UserDB).filter(UserDB.id == worker_id).first()
    if not worker or not worker.profile_public:
        raise HTTPException(status_code=404, detail="Worker not found")

    pins = (
        db.query(WorkerPortfolioItemDB)
        .filter(WorkerPortfolioItemDB.worker_id == worker_id)
        .order_by(WorkerPortfolioItemDB.display_order, WorkerPortfolioItemDB.pinned_at)
        .all()
    )

    items = []
    for pin in pins:
        task = pin.task
        items.append({
            "id": str(pin.id),
            "task_id": str(pin.task_id),
            "task_type": task.task_type if task else "unknown",
            "task_title": getattr(task, "title", None) if task else None,
            "caption": pin.caption,
            "display_order": pin.display_order,
            "pinned_at": pin.pinned_at.isoformat(),
            "result_snippet": _result_snippet(task) if task and task.status == "completed" else None,
        })

    return {"items": items, "total": len(items)}
