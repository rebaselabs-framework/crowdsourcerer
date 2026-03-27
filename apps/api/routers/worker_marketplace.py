"""Worker Skill Marketplace + Task Watchlist + Direct Invite.

Allows requesters to:
- Browse workers by skill, reputation, certifications
- Send a direct invite to a worker for a specific open task

Allows workers to:
- View and respond to pending invites
- Bookmark (watchlist) open tasks to receive alerts when re-opened
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db


def _task_label(task: "TaskDB") -> str:
    """Return a human-friendly label for a task (no title column on TaskDB)."""
    if task.input and isinstance(task.input, dict):
        for key in ("url", "text", "description", "query", "title"):
            val = task.input.get(key)
            if val and isinstance(val, str):
                return val[:80]
    return task.type.replace("_", " ").title()
from core.notify import create_notification, NotifType
from core.scopes import require_scope, SCOPE_TASKS_READ, SCOPE_TASKS_WRITE
from models.db import (
    UserDB, TaskDB, TaskAssignmentDB,
    WorkerSkillDB, WorkerCertificationDB, WorkerEndorsementDB,
    WorkerInviteDB, TaskWatchlistDB, CertificationDB,
)
from models.schemas import BulkInviteRequest

logger = structlog.get_logger()
router = APIRouter(tags=["worker_marketplace"])

PROFICIENCY_LABELS = {1: "Novice", 2: "Apprentice", 3: "Intermediate", 4: "Advanced", 5: "Expert"}


# ─── Schemas ─────────────────────────────────────────────────────────────────

class WorkerSkillSummary(BaseModel):
    task_type: str
    proficiency_level: int
    proficiency_label: str
    tasks_completed: int
    accuracy: Optional[float] = None
    verified: bool = False

    model_config = {"from_attributes": True}


class WorkerCardOut(BaseModel):
    id: UUID
    name: Optional[str]
    avatar_url: Optional[str]
    bio: Optional[str]
    reputation_score: float
    worker_level: int
    worker_tasks_completed: int
    worker_accuracy: Optional[float]
    skills: list[WorkerSkillSummary]
    verified_skill_count: int
    cert_count: int
    endorsement_count: int
    profile_url: str
    availability_status: str = "available"

    model_config = {"from_attributes": True}


class WorkerBrowseOut(BaseModel):
    items: list[WorkerCardOut]
    total: int
    page: int
    pages: int


class InviteRequest(BaseModel):
    worker_id: UUID
    message: Optional[str] = Field(None, max_length=500)


class InviteOut(BaseModel):
    id: UUID
    task_id: UUID
    task_title: Optional[str]
    worker_id: UUID
    worker_name: Optional[str]
    requester_id: UUID
    message: Optional[str]
    status: str
    created_at: str
    responded_at: Optional[str]


class InviteRespondRequest(BaseModel):
    action: str  # "accept" | "decline"


class WatchlistItemOut(BaseModel):
    id: UUID
    task_id: UUID
    task_title: Optional[str]
    task_type: str
    task_status: str
    reward: Optional[int]
    created_at: str


class WatchlistOut(BaseModel):
    items: list[WatchlistItemOut]
    total: int


# ─── Worker Browse ─────────────────────────────────────────────────────────────

@router.get("/v1/workers/browse", response_model=WorkerBrowseOut)
async def browse_workers(
    skill: Optional[str] = Query(None, description="Filter by task_type skill"),
    verified_only: bool = Query(False, description="Only workers with verified skills"),
    min_reputation: float = Query(0.0, ge=0, le=100),
    min_tasks: int = Query(0, ge=0, description="Minimum tasks completed"),
    cert: Optional[str] = Query(None, description="Filter by certification task_type"),
    sort: str = Query("reputation", description="Sort: reputation|tasks|accuracy"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """Browse the worker marketplace.

    Returns paginated worker cards with skill summaries, reputation,
    certifications and endorsement counts.  Requesters use this to find
    the right worker to invite to a task.
    """
    offset = (page - 1) * limit

    # Base query — workers only, active, not banned
    base_q = (
        select(UserDB)
        .where(
            UserDB.role.in_(["worker", "both"]),
            UserDB.is_active == True,  # noqa: E712
            UserDB.is_banned == False,  # noqa: E712
            UserDB.profile_public == True,  # noqa: E712
            UserDB.reputation_score >= min_reputation,
        )
    )

    if min_tasks > 0:
        base_q = base_q.where(UserDB.worker_tasks_completed >= min_tasks)

    # Filter by skill — must have a WorkerSkillDB row for this task_type
    if skill:
        skill_sub = (
            select(WorkerSkillDB.worker_id)
            .where(WorkerSkillDB.task_type == skill)
        )
        if verified_only:
            skill_sub = skill_sub.where(WorkerSkillDB.verified == True)  # noqa: E712
        base_q = base_q.where(UserDB.id.in_(skill_sub))
    elif verified_only:
        # Any verified skill
        verified_sub = (
            select(WorkerSkillDB.worker_id)
            .where(WorkerSkillDB.verified == True)  # noqa: E712
        )
        base_q = base_q.where(UserDB.id.in_(verified_sub))

    # Filter by certification
    if cert:
        cert_sub = (
            select(WorkerCertificationDB.worker_id)
            .join(CertificationDB, CertificationDB.id == WorkerCertificationDB.cert_id)
            .where(
                CertificationDB.task_type == cert,
                WorkerCertificationDB.passed == True,  # noqa: E712
            )
        )
        base_q = base_q.where(UserDB.id.in_(cert_sub))

    # Count total
    count_q = select(func.count()).select_from(base_q.subquery())
    total = await db.scalar(count_q) or 0

    # Apply sort
    if sort == "tasks":
        base_q = base_q.order_by(UserDB.worker_tasks_completed.desc())
    elif sort == "accuracy":
        base_q = base_q.order_by(
            UserDB.worker_accuracy.desc().nulls_last()
        )
    else:
        base_q = base_q.order_by(UserDB.reputation_score.desc())

    result = await db.execute(base_q.offset(offset).limit(limit))
    workers = result.scalars().all()

    items: list[WorkerCardOut] = []
    if workers:
        worker_ids = [w.id for w in workers]

        # Bulk-load skills for all workers in one query (ordered so per-worker slice is stable)
        sk_result = await db.execute(
            select(WorkerSkillDB)
            .where(WorkerSkillDB.worker_id.in_(worker_ids))
            .order_by(WorkerSkillDB.worker_id, WorkerSkillDB.tasks_completed.desc())
        )
        skills_by_worker: dict[str, list[WorkerSkillDB]] = {}
        for s in sk_result.scalars():
            wid = str(s.worker_id)
            bucket = skills_by_worker.setdefault(wid, [])
            if len(bucket) < 6:  # keep at most 6 per worker (already sorted desc)
                bucket.append(s)

        # Bulk-load cert counts (passed=True) per worker via GROUP BY
        cert_row_result = await db.execute(
            select(WorkerCertificationDB.worker_id, func.count().label("cnt"))
            .where(
                WorkerCertificationDB.worker_id.in_(worker_ids),
                WorkerCertificationDB.passed == True,  # noqa: E712
            )
            .group_by(WorkerCertificationDB.worker_id)
        )
        cert_counts: dict[str, int] = {
            str(row.worker_id): row.cnt for row in cert_row_result
        }

        # Bulk-load endorsement counts per worker via GROUP BY
        endorse_row_result = await db.execute(
            select(WorkerEndorsementDB.worker_id, func.count().label("cnt"))
            .where(WorkerEndorsementDB.worker_id.in_(worker_ids))
            .group_by(WorkerEndorsementDB.worker_id)
        )
        endorse_counts: dict[str, int] = {
            str(row.worker_id): row.cnt for row in endorse_row_result
        }

        for w in workers:
            wid = str(w.id)
            sk_rows = skills_by_worker.get(wid, [])
            skills = [
                WorkerSkillSummary(
                    task_type=s.task_type,
                    proficiency_level=s.proficiency_level,
                    proficiency_label=PROFICIENCY_LABELS.get(s.proficiency_level, "Unknown"),
                    tasks_completed=s.tasks_completed,
                    accuracy=round(s.accuracy, 3) if s.accuracy else None,
                    verified=s.verified,
                )
                for s in sk_rows
            ]

            verified_count = sum(1 for s in sk_rows if s.verified)

            items.append(
                WorkerCardOut(
                    id=w.id,
                    name=w.name,
                    avatar_url=w.avatar_url,
                    bio=w.bio,
                    reputation_score=round(w.reputation_score, 1),
                    worker_level=w.worker_level,
                    worker_tasks_completed=w.worker_tasks_completed,
                    worker_accuracy=(
                        round(w.worker_accuracy * 100, 1)
                        if w.worker_accuracy else None
                    ),
                    skills=skills,
                    verified_skill_count=verified_count,
                    cert_count=cert_counts.get(wid, 0),
                    endorsement_count=endorse_counts.get(wid, 0),
                    profile_url=f"/workers/{w.id}",
                    availability_status=getattr(w, "availability_status", "available"),
                )
            )

    pages = max(1, (total + limit - 1) // limit)
    return WorkerBrowseOut(items=items, total=total, page=page, pages=pages)


# ─── Worker Invite ────────────────────────────────────────────────────────────

@router.post("/v1/tasks/{task_id}/invite", status_code=201)
async def invite_worker(
    task_id: UUID,
    req: InviteRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Send a direct invite to a worker for a specific task.

    The task must be open/pending and belong to the requester.
    Duplicate invites (same task + worker) are rejected.
    The worker receives an in-app notification.
    """
    task_result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.execution_mode != "human":
        raise HTTPException(status_code=400, detail="Can only invite workers to human tasks")

    if task.status not in ("open", "pending"):
        raise HTTPException(
            status_code=400,
            detail="Invites can only be sent for open or pending tasks",
        )

    # Verify worker exists and is active
    worker_result = await db.execute(
        select(UserDB).where(
            UserDB.id == req.worker_id,
            UserDB.role.in_(["worker", "both"]),
            UserDB.is_active == True,  # noqa: E712
        )
    )
    worker = worker_result.scalar_one_or_none()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    # Check for duplicate
    existing = await db.scalar(
        select(func.count()).where(
            WorkerInviteDB.task_id == task_id,
            WorkerInviteDB.worker_id == req.worker_id,
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="Worker already invited to this task")

    invite = WorkerInviteDB(
        id=_uuid.uuid4(),
        task_id=task_id,
        worker_id=req.worker_id,
        requester_id=user_id,
        message=req.message,
    )
    db.add(invite)
    await db.flush()

    # Notify the worker
    requester_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    requester = requester_result.scalar_one_or_none()
    requester_name = requester.name if requester else "Someone"
    await create_notification(
        db,
        user_id=req.worker_id,
        type=NotifType.WORKER_INVITED,
        title="You've been invited to a task!",
        body=(
            f"{requester_name} invited you to: {_task_label(task)}. "
            + (f'"{req.message}"' if req.message else "Check it out!")
        ),
        link=f"/worker/invites",
    )

    await db.commit()
    logger.info("invite.created", invite_id=str(invite.id),
                task_id=str(task_id), worker_id=str(req.worker_id))

    return {
        "id": str(invite.id),
        "task_id": str(task_id),
        "worker_id": str(req.worker_id),
        "status": "pending",
    }


@router.post("/v1/tasks/{task_id}/bulk-invite", status_code=201)
async def bulk_invite_workers(
    task_id: UUID,
    req: BulkInviteRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Invite multiple workers to a task in a single request.

    Workers already invited or not found are silently skipped.
    Returns counts of invited vs skipped workers.
    """
    task_result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.execution_mode != "human":
        raise HTTPException(status_code=400, detail="Can only invite workers to human tasks")

    if task.status not in ("open", "pending"):
        raise HTTPException(
            status_code=400,
            detail="Invites can only be sent for open or pending tasks",
        )

    # Fetch requester name once
    requester_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    requester = requester_result.scalar_one_or_none()
    requester_name = requester.name if requester else "Someone"

    # Bulk-load all valid workers and existing invites in two queries instead of N×2
    valid_workers: dict[str, UserDB] = {}
    if req.worker_ids:
        vw_result = await db.execute(
            select(UserDB).where(
                UserDB.id.in_(req.worker_ids),
                UserDB.role.in_(["worker", "both"]),
                UserDB.is_active == True,  # noqa: E712
            )
        )
        valid_workers = {str(u.id): u for u in vw_result.scalars()}

    existing_invite_worker_ids: set[str] = set()
    if req.worker_ids:
        ei_result = await db.execute(
            select(WorkerInviteDB.worker_id).where(
                WorkerInviteDB.task_id == task_id,
                WorkerInviteDB.worker_id.in_(req.worker_ids),
            )
        )
        existing_invite_worker_ids = {str(row[0]) for row in ei_result}

    invited: list[str] = []
    skipped: int = 0

    for worker_id in req.worker_ids:
        wid = str(worker_id)
        if wid not in valid_workers:
            skipped += 1
            continue
        if wid in existing_invite_worker_ids:
            skipped += 1
            continue

        invite = WorkerInviteDB(
            id=_uuid.uuid4(),
            task_id=task_id,
            worker_id=worker_id,
            requester_id=user_id,
            message=req.message,
        )
        db.add(invite)
        await db.flush()

        await create_notification(
            db,
            user_id=worker_id,
            type=NotifType.WORKER_INVITED,
            title="You've been invited to a task!",
            body=(
                f"{requester_name} invited you to: {_task_label(task)}. "
                + (f'"{req.message}"' if req.message else "Check it out!")
            ),
            link="/worker/invites",
        )
        invited.append(str(invite.id))

    await db.commit()
    logger.info(
        "bulk_invite.done",
        task_id=str(task_id),
        invited=len(invited),
        skipped=skipped,
    )
    return {"invited": len(invited), "skipped": skipped, "invite_ids": invited}


@router.get("/v1/tasks/{task_id}/invites", response_model=list[InviteOut])
async def list_task_invites(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """List all invites sent for a task (requester only)."""
    task_result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    result = await db.execute(
        select(WorkerInviteDB)
        .where(WorkerInviteDB.task_id == task_id)
        .order_by(WorkerInviteDB.created_at.desc())
    )
    invites = result.scalars().all()

    # Bulk-load workers for all invites (task is already known from the URL param)
    workers_map: dict[str, UserDB] = {}
    if invites:
        worker_ids = list({inv.worker_id for inv in invites})
        w_result = await db.execute(select(UserDB).where(UserDB.id.in_(worker_ids)))
        workers_map = {str(u.id): u for u in w_result.scalars()}

    out: list[InviteOut] = []
    for inv in invites:
        w = workers_map.get(str(inv.worker_id))
        out.append(InviteOut(
            id=inv.id,
            task_id=inv.task_id,
            task_title=_task_label(task),  # same task for all invites in this endpoint
            worker_id=inv.worker_id,
            worker_name=w.name if w else None,
            requester_id=inv.requester_id,
            message=inv.message,
            status=inv.status,
            created_at=inv.created_at.isoformat(),
            responded_at=inv.responded_at.isoformat() if inv.responded_at else None,
        ))
    return out


@router.get("/v1/worker/invites", response_model=list[InviteOut])
async def list_my_invites(
    status: Optional[str] = Query(None, description="Filter: pending|accepted|declined|expired"),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List invites received by the current worker."""
    q = (
        select(WorkerInviteDB)
        .where(WorkerInviteDB.worker_id == user_id)
        .order_by(WorkerInviteDB.created_at.desc())
    )
    if status:
        q = q.where(WorkerInviteDB.status == status)

    result = await db.execute(q)
    invites = result.scalars().all()

    # Bulk-load tasks and requesters for all invites up front
    tasks_map: dict[str, TaskDB] = {}
    requesters_map: dict[str, UserDB] = {}
    if invites:
        task_ids = list({inv.task_id for inv in invites})
        t_result = await db.execute(select(TaskDB).where(TaskDB.id.in_(task_ids)))
        tasks_map = {str(t.id): t for t in t_result.scalars()}

        requester_ids = list({inv.requester_id for inv in invites})
        r_result = await db.execute(select(UserDB).where(UserDB.id.in_(requester_ids)))
        requesters_map = {str(u.id): u for u in r_result.scalars()}

    # Auto-expire old pending invites
    now = datetime.now(timezone.utc)
    expired_ids: list[UUID] = []
    out: list[InviteOut] = []
    for inv in invites:
        effective_status = inv.status
        if (
            inv.status == "pending"
            and (now - inv.created_at.replace(tzinfo=timezone.utc)).total_seconds() > 48 * 3600
        ):
            effective_status = "expired"
            expired_ids.append(inv.id)

        t = tasks_map.get(str(inv.task_id))
        out.append(InviteOut(
            id=inv.id,
            task_id=inv.task_id,
            task_title=_task_label(t) if t else None,
            worker_id=inv.worker_id,
            worker_name=None,   # self — not needed
            requester_id=inv.requester_id,
            message=inv.message,
            status=effective_status,
            created_at=inv.created_at.isoformat(),
            responded_at=inv.responded_at.isoformat() if inv.responded_at else None,
        ))

    # Persist expiry updates in background
    if expired_ids:
        for inv_id in expired_ids:
            await db.execute(
                WorkerInviteDB.__table__.update()
                .where(WorkerInviteDB.id == inv_id)
                .values(status="expired", responded_at=now)
            )
        await db.commit()

    return out


@router.post("/v1/worker/invites/{invite_id}/respond", status_code=200)
async def respond_to_invite(
    invite_id: UUID,
    req: InviteRespondRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Accept or decline an invite.

    On accept, the worker is auto-assigned to the task (if still open and
    slots are available).  On decline, the invite is marked declined and the
    requester is notified.
    """
    if req.action not in ("accept", "decline"):
        raise HTTPException(status_code=400, detail="action must be 'accept' or 'decline'")

    invite_result = await db.execute(
        select(WorkerInviteDB)
        .where(
            WorkerInviteDB.id == invite_id,
            WorkerInviteDB.worker_id == user_id,
        )
        .with_for_update()
    )
    invite = invite_result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    if invite.status != "pending":
        raise HTTPException(status_code=400, detail=f"Invite is already {invite.status}")

    now = datetime.now(timezone.utc)

    # Auto-expire check
    if (now - invite.created_at.replace(tzinfo=timezone.utc)).total_seconds() > 48 * 3600:
        invite.status = "expired"
        invite.responded_at = now
        await db.commit()
        raise HTTPException(status_code=400, detail="Invite has expired")

    invite.responded_at = now

    if req.action == "decline":
        invite.status = "declined"
        await db.flush()

        # Notify requester
        worker_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
        worker = worker_result.scalar_one_or_none()
        task_result = await db.execute(select(TaskDB).where(TaskDB.id == invite.task_id))
        task = task_result.scalar_one_or_none()
        await create_notification(
            db,
            user_id=invite.requester_id,
            type=NotifType.INVITE_DECLINED,
            title="Worker declined your invite",
            body=(
                f"{worker.name if worker else 'A worker'} declined the invite "
                f"to: {_task_label(task) if task else 'your task'}."
            ),
            link=f"/dashboard/tasks/{invite.task_id}",
        )
        await db.commit()
        logger.info("invite.declined", invite_id=str(invite_id))
        return {"status": "declined"}

    # Accept — attempt to claim the task
    task_result = await db.execute(select(TaskDB).where(TaskDB.id == invite.task_id))
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task no longer exists")

    if task.status not in ("open", "pending"):
        raise HTTPException(status_code=400, detail="Task is no longer available")

    # Check slots
    active_assignments = await db.scalar(
        select(func.count()).where(
            TaskAssignmentDB.task_id == invite.task_id,
            TaskAssignmentDB.status.in_(["active", "submitted"]),
        )
    ) or 0
    max_slots = task.assignments_required or 1
    if active_assignments >= max_slots:
        raise HTTPException(status_code=400, detail="Task has no available slots")

    # Create assignment
    assignment = TaskAssignmentDB(
        id=_uuid.uuid4(),
        task_id=invite.task_id,
        worker_id=user_id,
        status="active",
        claimed_at=now,
    )
    db.add(assignment)

    # Update task status
    task.status = "assigned"

    invite.status = "accepted"
    await db.flush()

    # Notify requester
    worker_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    worker = worker_result.scalar_one_or_none()
    await create_notification(
        db,
        user_id=invite.requester_id,
        type=NotifType.INVITE_ACCEPTED,
        title="Worker accepted your invite!",
        body=(
            f"{worker.name if worker else 'A worker'} accepted the invite "
            f"to: {_task_label(task)}."
        ),
        link=f"/dashboard/tasks/{invite.task_id}",
    )
    await db.commit()
    logger.info("invite.accepted", invite_id=str(invite_id), task_id=str(invite.task_id))
    return {"status": "accepted", "assignment_id": str(assignment.id)}


# ─── Task Watchlist ──────────────────────────────────────────────────────────

@router.post("/v1/worker/watchlist/{task_id}", status_code=201)
async def add_to_watchlist(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Bookmark a task.  Max 100 items per worker."""
    task_result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    if not task_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Task not found")

    existing = await db.scalar(
        select(func.count()).where(
            TaskWatchlistDB.worker_id == user_id,
            TaskWatchlistDB.task_id == task_id,
        )
    )
    if existing:
        return {"status": "already_watching"}

    count = await db.scalar(
        select(func.count()).where(TaskWatchlistDB.worker_id == user_id)
    ) or 0
    if count >= 100:
        raise HTTPException(status_code=400, detail="Watchlist limit (100) reached")

    item = TaskWatchlistDB(
        id=_uuid.uuid4(),
        worker_id=user_id,
        task_id=task_id,
    )
    db.add(item)
    await db.commit()
    return {"status": "watching", "task_id": str(task_id)}


@router.delete("/v1/worker/watchlist/{task_id}", status_code=200)
async def remove_from_watchlist(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Remove a task from watchlist."""
    result = await db.execute(
        select(TaskWatchlistDB).where(
            TaskWatchlistDB.worker_id == user_id,
            TaskWatchlistDB.task_id == task_id,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Not in watchlist")

    await db.delete(item)
    await db.commit()
    return {"status": "removed"}


@router.get("/v1/worker/watchlist", response_model=WatchlistOut)
async def get_watchlist(
    include_all: bool = Query(False, description="Include closed/completed tasks too"),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return the current worker's watchlist."""
    q = (
        select(TaskWatchlistDB)
        .where(TaskWatchlistDB.worker_id == user_id)
        .order_by(TaskWatchlistDB.created_at.desc())
    )
    result = await db.execute(q)
    items_db = result.scalars().all()

    total = len(items_db)

    # Bulk-load all tasks at once instead of one query per watchlist item
    tasks_map: dict[str, TaskDB] = {}
    if items_db:
        task_ids = [w.task_id for w in items_db]
        t_result = await db.execute(select(TaskDB).where(TaskDB.id.in_(task_ids)))
        tasks_map = {str(t.id): t for t in t_result.scalars()}

    out: list[WatchlistItemOut] = []
    for w in items_db:
        task = tasks_map.get(str(w.task_id))
        if not task:
            continue
        if not include_all and task.status not in ("open", "pending"):
            continue
        out.append(WatchlistItemOut(
            id=w.id,
            task_id=w.task_id,
            task_title=_task_label(task),
            task_type=task.type,
            task_status=task.status,
            reward=task.worker_reward_credits,
            created_at=w.created_at.isoformat(),
        ))

    return WatchlistOut(items=out, total=total)


@router.get("/v1/worker/watchlist/check/{task_id}")
async def check_watchlist(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Check if a task is in the current worker's watchlist."""
    exists = await db.scalar(
        select(func.count()).where(
            TaskWatchlistDB.worker_id == user_id,
            TaskWatchlistDB.task_id == task_id,
        )
    )
    return {"watching": bool(exists), "task_id": str(task_id)}
