"""Worker Application / Proposal System.

Workers can apply to tasks that have application_mode=True.
Requesters can review, accept, or reject applications.
Accepting an application auto-assigns the task to that worker.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from core.auth import get_current_user_id
from core.database import get_db
from core.notify import create_notification, NotifType
from models.db import (
    TaskApplicationDB, TaskDB, UserDB, TaskAssignmentDB,
)

logger = structlog.get_logger()
router = APIRouter(tags=["applications"])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class ApplicationSubmitRequest(BaseModel):
    proposal: str = Field(..., min_length=1, max_length=1000)
    proposed_reward: Optional[int] = Field(None, ge=1)


class ApplicationOut(BaseModel):
    id: str
    task_id: str
    worker_id: str
    worker_name: Optional[str]
    worker_reputation: Optional[float]
    proposal: str
    proposed_reward: Optional[int]
    status: str
    created_at: str
    updated_at: str


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _fmt_application(app: TaskApplicationDB, db: AsyncSession) -> ApplicationOut:
    worker_result = await db.execute(select(UserDB).where(UserDB.id == app.worker_id))
    worker = worker_result.scalar_one_or_none()
    return ApplicationOut(
        id=str(app.id),
        task_id=str(app.task_id),
        worker_id=str(app.worker_id),
        worker_name=worker.name if worker else None,
        worker_reputation=round(worker.reputation_score, 1) if worker else None,
        proposal=app.proposal,
        proposed_reward=app.proposed_reward,
        status=app.status,
        created_at=app.created_at.isoformat(),
        updated_at=app.updated_at.isoformat(),
    )


async def _get_task_or_404(task_id: UUID, db: AsyncSession) -> TaskDB:
    result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/v1/tasks/{task_id}/apply", response_model=ApplicationOut, status_code=201)
async def submit_application(
    task_id: UUID,
    req: ApplicationSubmitRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Worker submits a proposal to an application-mode task."""
    # Load + validate worker
    worker_result = await db.execute(select(UserDB).where(UserDB.id == UUID(user_id)))
    worker = worker_result.scalar_one_or_none()
    if not worker or worker.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Only workers can apply to tasks")
    if worker.is_banned:
        raise HTTPException(status_code=403, detail="Your worker account has been suspended")
    if (worker.reputation_score or 0) < 10:
        raise HTTPException(status_code=403, detail="Reputation too low to apply to tasks")

    task = await _get_task_or_404(task_id, db)

    if not task.application_mode:
        raise HTTPException(status_code=400, detail="This task does not accept applications")
    if task.status not in ("open", "pending"):
        raise HTTPException(status_code=400, detail="Task is not accepting applications")

    # Check not already applied
    existing = await db.scalar(
        select(func.count()).where(
            TaskApplicationDB.task_id == task_id,
            TaskApplicationDB.worker_id == UUID(user_id),
            TaskApplicationDB.status != "withdrawn",
        )
    )
    if existing and existing > 0:
        raise HTTPException(status_code=409, detail="You have already applied to this task")

    app = TaskApplicationDB(
        task_id=task_id,
        worker_id=UUID(user_id),
        proposal=req.proposal,
        proposed_reward=req.proposed_reward,
        status="pending",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(app)
    await db.flush()

    # Notify requester
    try:
        await create_notification(
            db,
            task.user_id,
            NotifType.APPLICATION_RECEIVED,
            "New application received",
            f"{worker.name or 'A worker'} applied to your task.",
            link=f"/dashboard/tasks/{task_id}",
        )
    except Exception:  # noqa: BLE001
        pass

    await db.commit()
    await db.refresh(app)

    logger.info("application.submitted", task_id=str(task_id), worker_id=user_id)
    return await _fmt_application(app, db)


@router.get("/v1/tasks/{task_id}/applications", response_model=list[ApplicationOut])
async def list_applications(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Requester lists all applications for their task."""
    task = await _get_task_or_404(task_id, db)
    if str(task.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Only the task owner can view applications")

    result = await db.execute(
        select(TaskApplicationDB)
        .where(TaskApplicationDB.task_id == task_id)
        .order_by(TaskApplicationDB.created_at.asc())
    )
    apps = result.scalars().all()
    return [await _fmt_application(a, db) for a in apps]


@router.post("/v1/tasks/{task_id}/applications/{app_id}/accept", response_model=ApplicationOut)
async def accept_application(
    task_id: UUID,
    app_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Requester accepts an application — assigns the task to that worker."""
    task = await _get_task_or_404(task_id, db)
    if str(task.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Only the task owner can accept applications")

    app_result = await db.execute(
        select(TaskApplicationDB).where(
            TaskApplicationDB.id == app_id,
            TaskApplicationDB.task_id == task_id,
        )
    )
    app = app_result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if app.status != "pending":
        raise HTTPException(status_code=400, detail=f"Application is already {app.status}")

    # Accept this application
    app.status = "accepted"
    app.updated_at = utcnow()

    # Reject all other pending applications
    all_apps_result = await db.execute(
        select(TaskApplicationDB).where(
            TaskApplicationDB.task_id == task_id,
            TaskApplicationDB.id != app_id,
            TaskApplicationDB.status == "pending",
        )
    )
    other_apps = all_apps_result.scalars().all()
    for other in other_apps:
        other.status = "rejected"
        other.updated_at = utcnow()
        # Notify rejected workers
        try:
            await create_notification(
                db,
                other.worker_id,
                NotifType.APPLICATION_REJECTED,
                "Application not selected",
                "The requester selected another applicant for this task.",
                link=f"/worker/applications",
            )
        except Exception:  # noqa: BLE001
            pass

    # Create task assignment for the accepted worker
    from datetime import timedelta
    timeout_at = utcnow() + timedelta(minutes=task.claim_timeout_minutes)
    assignment = TaskAssignmentDB(
        task_id=task_id,
        worker_id=app.worker_id,
        status="active",
        claimed_at=utcnow(),
        timeout_at=timeout_at,
    )
    db.add(assignment)
    task.status = "assigned"

    # Notify accepted worker
    try:
        await create_notification(
            db,
            app.worker_id,
            NotifType.APPLICATION_ACCEPTED,
            "Your application was accepted!",
            "Your proposal was selected. The task has been assigned to you.",
            link=f"/worker/tasks",
        )
    except Exception:  # noqa: BLE001
        pass

    await db.commit()
    await db.refresh(app)

    logger.info("application.accepted", task_id=str(task_id), app_id=str(app_id), worker_id=str(app.worker_id))
    return await _fmt_application(app, db)


@router.post("/v1/tasks/{task_id}/applications/{app_id}/reject", response_model=ApplicationOut)
async def reject_application(
    task_id: UUID,
    app_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Requester manually rejects a specific application."""
    task = await _get_task_or_404(task_id, db)
    if str(task.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Only the task owner can reject applications")

    app_result = await db.execute(
        select(TaskApplicationDB).where(
            TaskApplicationDB.id == app_id,
            TaskApplicationDB.task_id == task_id,
        )
    )
    app = app_result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if app.status != "pending":
        raise HTTPException(status_code=400, detail=f"Application is already {app.status}")

    app.status = "rejected"
    app.updated_at = utcnow()

    try:
        await create_notification(
            db,
            app.worker_id,
            NotifType.APPLICATION_REJECTED,
            "Application not selected",
            "The requester rejected your application for this task.",
            link=f"/worker/applications",
        )
    except Exception:  # noqa: BLE001
        pass

    await db.commit()
    await db.refresh(app)

    logger.info("application.rejected", task_id=str(task_id), app_id=str(app_id))
    return await _fmt_application(app, db)


@router.delete("/v1/tasks/{task_id}/applications", status_code=204, response_class=Response)
async def withdraw_application(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Worker withdraws their own application."""
    app_result = await db.execute(
        select(TaskApplicationDB).where(
            TaskApplicationDB.task_id == task_id,
            TaskApplicationDB.worker_id == UUID(user_id),
            TaskApplicationDB.status == "pending",
        )
    )
    app = app_result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="No pending application found for this task")

    app.status = "withdrawn"
    app.updated_at = utcnow()
    await db.commit()

    logger.info("application.withdrawn", task_id=str(task_id), worker_id=user_id)


@router.get("/v1/worker/applications", response_model=list[ApplicationOut])
async def list_my_applications(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Worker lists their own application history."""
    worker_result = await db.execute(select(UserDB).where(UserDB.id == UUID(user_id)))
    worker = worker_result.scalar_one_or_none()
    if not worker or worker.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Only workers can view their applications")

    q = select(TaskApplicationDB).where(TaskApplicationDB.worker_id == UUID(user_id))
    if status:
        q = q.where(TaskApplicationDB.status == status)
    q = q.order_by(TaskApplicationDB.created_at.desc()).offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(q)
    apps = result.scalars().all()
    return [await _fmt_application(a, db) for a in apps]
