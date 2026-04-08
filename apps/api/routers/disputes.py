"""Dispute resolution and consensus management for multi-worker human tasks."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id, require_admin
from core.database import get_db
from core.notify import create_notification, NotifType
from models.db import TaskDB, UserDB, TaskAssignmentDB, DisputeEvidenceDB, DisputeEventDB
from models.schemas import (
    ConsensusStateOut, ConsensusVoteOut,
    DisputeResolveRequest, DisputeResolveResponse,
    TaskOut, PaginatedTasks,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/disputes", tags=["disputes"])
limiter = Limiter(key_func=get_remote_address)


# ──────────────────────────────────────────────────────────────────────────────
# Consensus helpers
# ──────────────────────────────────────────────────────────────────────────────

def _response_key(response: dict) -> str:
    """Canonical JSON key for a response dict (for vote comparison)."""
    return json.dumps(response, sort_keys=True, default=str)


async def check_and_apply_consensus(task: TaskDB, db: AsyncSession) -> None:
    """
    Called after a new worker submission is saved.
    Examines all submitted assignments and applies consensus logic:
    - any_first: first submission wins → task already completed upstream
    - majority_vote: >50% of workers must agree
    - unanimous: all workers must agree
    - requester_review: never auto-resolves; always flags for review
    """
    if task.consensus_strategy == "any_first":
        # Already handled in the submit endpoint (first-past-the-post)
        return

    if task.assignments_completed < task.assignments_required:
        # Not all assignments in yet
        return

    # Load all submitted assignments
    result = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.task_id == task.id,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
        )
    )
    assignments = result.scalars().all()

    if not assignments:
        return

    strategy = task.consensus_strategy

    if strategy == "requester_review":
        # Always flag for requester review — mark task as disputed
        task.dispute_status = "disputed"
        task.status = "completed"  # Mark as completed but flagged
        task.completed_at = datetime.now(timezone.utc)
        logger.info("task_flagged_for_review", task_id=str(task.id))
        # Notify requester
        await create_notification(
            db=db,
            user_id=str(task.user_id),
            type=NotifType.DISPUTE_FLAGGED,
            title="Task needs your review",
            body=f"Task {str(task.id)[:8]}… received all submissions. Please review and pick the best answer.",
            link=f"/dashboard/disputes/{task.id}",
        )
        return

    # Build vote tally
    vote_counts: dict[str, list[UUID]] = {}
    for a in assignments:
        if a.response:
            key = _response_key(a.response)
            if key not in vote_counts:
                vote_counts[key] = []
            vote_counts[key].append(a.id)

    total = len(assignments)
    winning_key: Optional[str] = None
    winning_assignment_ids: Optional[list[UUID]] = None

    if strategy == "majority_vote":
        for key, ids in vote_counts.items():
            if len(ids) > total / 2:
                winning_key = key
                winning_assignment_ids = ids
                break
    elif strategy == "unanimous":
        if len(vote_counts) == 1:
            winning_key, winning_assignment_ids = next(iter(vote_counts.items()))

    if winning_key is not None and winning_assignment_ids:
        # Consensus reached — pick the first matching assignment as the winner
        task.winning_assignment_id = winning_assignment_ids[0]
        task.dispute_status = None  # No dispute
        task.status = "completed"
        task.completed_at = datetime.now(timezone.utc)
        # Set task output to the winning response
        winner_result = await db.execute(
            select(TaskAssignmentDB).where(
                TaskAssignmentDB.id == task.winning_assignment_id
            )
        )
        winner = winner_result.scalar_one_or_none()
        if winner:
            task.output = winner.response
        logger.info("consensus_reached", task_id=str(task.id), strategy=strategy,
                    winner=str(task.winning_assignment_id))
    else:
        # No consensus — dispute!
        task.dispute_status = "disputed"
        task.status = "completed"  # Mark completed but flagged as disputed
        task.completed_at = datetime.now(timezone.utc)
        logger.info("dispute_created", task_id=str(task.id), strategy=strategy,
                    vote_counts={k: len(v) for k, v in vote_counts.items()})
        # Notify requester
        await create_notification(
            db=db,
            user_id=str(task.user_id),
            type=NotifType.DISPUTE_FLAGGED,
            title="Workers disagree — dispute flagged",
            body=f"Task {str(task.id)[:8]}… has conflicting answers. Please resolve the dispute.",
            link=f"/dashboard/disputes/{task.id}",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/tasks", response_model=PaginatedTasks)
async def list_disputed_tasks(
    status: Optional[str] = Query(None, description="Filter: disputed | resolved | all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List all tasks that require dispute resolution (requester only)."""
    q = select(TaskDB).where(
        TaskDB.user_id == user_id,
        TaskDB.dispute_status.isnot(None),
    )
    if status and status != "all":
        q = q.where(TaskDB.dispute_status == status)
    else:
        # Default: show both disputed and resolved
        pass

    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = q.order_by(TaskDB.completed_at.desc().nullslast(), TaskDB.created_at.desc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    tasks = result.scalars().all()

    return PaginatedTasks(
        items=[TaskOut.model_validate(t) for t in tasks],
        total=total,
        page=page,
        page_size=page_size,
        has_next=(page * page_size) < total,
    )


@router.get("/tasks/{task_id}/consensus", response_model=ConsensusStateOut)
async def get_consensus_state(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get the consensus / vote breakdown for a multi-worker task."""
    result = await db.execute(
        select(TaskDB).where(
            TaskDB.id == task_id,
            TaskDB.user_id == user_id,
        )
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Load submitted assignments
    asgn_result = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.task_id == task_id,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
        )
    )
    assignments = asgn_result.scalars().all()

    # Build vote breakdown
    vote_counts: dict[str, list[UUID]] = {}
    for a in assignments:
        if a.response:
            key = _response_key(a.response)
            if key not in vote_counts:
                vote_counts[key] = []
            vote_counts[key].append(a.id)

    total = len(assignments)
    votes = [
        ConsensusVoteOut(
            response_key=key,
            count=len(ids),
            percentage=round(len(ids) / total * 100, 1) if total else 0,
            assignment_ids=ids,
        )
        for key, ids in sorted(vote_counts.items(), key=lambda x: -len(x[1]))
    ]

    consensus_reached = (
        task.dispute_status is None
        and task.status == "completed"
        and task.winning_assignment_id is not None
    ) or (
        task.consensus_strategy == "any_first"
        and task.status == "completed"
    )

    return ConsensusStateOut(
        task_id=task.id,
        strategy=task.consensus_strategy,
        assignments_required=task.assignments_required,
        assignments_submitted=total,
        consensus_reached=consensus_reached,
        dispute_status=task.dispute_status,
        winning_assignment_id=task.winning_assignment_id,
        votes=votes,
    )


@router.post("/tasks/{task_id}/resolve", response_model=DisputeResolveResponse)
async def resolve_dispute(
    task_id: UUID,
    req: DisputeResolveRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Manually resolve a dispute by picking the winning submission.
    Requester selects one assignment as the definitive answer.
    """
    result = await db.execute(
        select(TaskDB).where(
            TaskDB.id == task_id,
            TaskDB.user_id == user_id,
        )
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.dispute_status not in ("disputed",) or task.execution_mode != "human":
        raise HTTPException(
            status_code=400,
            detail="Task is not in a disputed state or is not a human task",
        )

    # Verify the winning assignment belongs to this task
    asgn_result = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.id == req.winning_assignment_id,
            TaskAssignmentDB.task_id == task_id,
            TaskAssignmentDB.status.in_(["submitted", "approved"]),
        )
    )
    winner = asgn_result.scalar_one_or_none()
    if not winner:
        raise HTTPException(
            status_code=404,
            detail="Winning assignment not found or does not belong to this task",
        )

    # Apply resolution
    task.winning_assignment_id = winner.id
    task.dispute_status = "resolved"
    task.output = winner.response  # Set task output to the selected answer

    # Approve the winner, reject others (only if not already reviewed)
    all_asgn_result = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.task_id == task_id,
            TaskAssignmentDB.status.in_(["submitted"]),
        )
    )
    all_assignments = all_asgn_result.scalars().all()
    for a in all_assignments:
        if a.id == winner.id:
            a.status = "approved"
        else:
            # Refund rejected workers' slots, mark rejected
            a.status = "rejected"

    await db.commit()

    logger.info("dispute_resolved", task_id=str(task_id),
                winner=str(winner.id), resolved_by=user_id)

    # Notify the winning worker
    await create_notification(
        db=db,
        user_id=str(winner.worker_id),
        type=NotifType.SUBMISSION_APPROVED,
        title="Your submission was selected!",
        body=f"Your answer was chosen as the best response for task {str(task_id)[:8]}…",
        link=f"/worker/submitted",
    )
    await db.commit()

    # Resume pipeline if this task was a human step in a pipeline
    try:
        from routers.pipelines import resume_pipeline_after_human_step
        await resume_pipeline_after_human_step(task.id, task.output, db)
    except Exception:
        logger.exception("pipeline_resume_failed_after_dispute", task_id=str(task_id))

    return DisputeResolveResponse(
        task_id=task_id,
        winning_assignment_id=winner.id,
        status="resolved",
        message="Dispute resolved. The selected submission has been approved.",
    )


# ─── Helper: record a dispute event ────────────────────────────────────────

async def _log_dispute_event(
    db: AsyncSession,
    task_id: UUID,
    event_type: str,
    description: str,
    actor_id: Optional[UUID] = None,
    metadata: Optional[dict] = None,
) -> DisputeEventDB:
    ev = DisputeEventDB(
        task_id=task_id,
        actor_id=actor_id,
        event_type=event_type,
        description=description,
        event_metadata=metadata or {},
    )
    db.add(ev)
    await db.flush()
    return ev


# ─── Evidence endpoints ────────────────────────────────────────────────────

@router.get("/tasks/{task_id}/evidence")
async def list_dispute_evidence(
    task_id: UUID,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List all evidence submitted for a disputed task."""
    uid = UUID(user_id)

    # Verify access: must be task owner, an assigned worker, or admin
    task_res = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_res.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    # Check access
    user_res = await db.execute(select(UserDB).where(UserDB.id == uid))
    user = user_res.scalar_one_or_none()
    is_admin = user and user.is_admin
    is_owner = task.user_id == uid
    asgn_res = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.task_id == task_id,
            TaskAssignmentDB.worker_id == uid,
        )
    )
    is_worker = asgn_res.scalar_one_or_none() is not None

    if not (is_admin or is_owner or is_worker):
        raise HTTPException(403, "Access denied")

    ev_res = await db.execute(
        select(DisputeEvidenceDB)
        .where(DisputeEvidenceDB.task_id == task_id)
        .order_by(DisputeEvidenceDB.created_at.asc())
    )
    evidence = ev_res.scalars().all()

    return [
        {
            "id": str(e.id),
            "submitter_id": str(e.submitter_id),
            "submitter_role": e.submitter_role,
            "evidence_type": e.evidence_type,
            "content": e.content,
            "assignment_id": str(e.assignment_id) if e.assignment_id else None,
            "created_at": e.created_at.isoformat(),
        }
        for e in evidence
    ]


class _DisputeEvidenceRequest(BaseModel):
    evidence_type: str = Field("text", description="Type: text, url, or image_url")
    content: str = Field(..., min_length=1, description="Evidence content")
    assignment_id: Optional[UUID] = None


@router.post("/tasks/{task_id}/evidence", status_code=201)
@limiter.limit("10/minute")
async def submit_dispute_evidence(
    request: Request,
    task_id: UUID,
    body: _DisputeEvidenceRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Submit evidence in support of a disputed task."""
    uid = UUID(user_id)
    evidence_type = body.evidence_type
    content = body.content
    assignment_id = body.assignment_id

    if evidence_type not in ("text", "url", "image_url"):
        raise HTTPException(400, "evidence_type must be text, url, or image_url")
    if not content.strip():
        raise HTTPException(400, "content cannot be empty")

    task_res = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_res.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    # Determine submitter role
    user_res = await db.execute(select(UserDB).where(UserDB.id == uid))
    user = user_res.scalar_one_or_none()

    if user and user.is_admin:
        role = "mediator"
    elif task.user_id == uid:
        role = "requester"
    else:
        asgn_res = await db.execute(
            select(TaskAssignmentDB).where(
                TaskAssignmentDB.task_id == task_id,
                TaskAssignmentDB.worker_id == uid,
            )
        )
        if not asgn_res.scalar_one_or_none():
            raise HTTPException(403, "You are not involved in this dispute")
        role = "worker"

    evidence = DisputeEvidenceDB(
        task_id=task_id,
        submitter_id=uid,
        submitter_role=role,
        evidence_type=evidence_type,
        content=content,
        assignment_id=assignment_id,
    )
    db.add(evidence)

    await _log_dispute_event(
        db, task_id, "evidence_added",
        f"{role.capitalize()} submitted evidence ({evidence_type})",
        actor_id=uid,
        metadata={"evidence_type": evidence_type, "content_preview": content[:100]},
    )
    await db.commit()
    await db.refresh(evidence)

    # Notify task owner if worker submitted evidence
    if role == "worker" and task.user_id != uid:
        await create_notification(
            db=db,
            user_id=str(task.user_id),
            type=NotifType.SYSTEM,
            title="New dispute evidence",
            body=f"A worker submitted evidence for task {str(task_id)[:8]}…",
            link=f"/dashboard/disputes",
        )
        await db.commit()

    return {
        "id": str(evidence.id),
        "submitter_role": role,
        "evidence_type": evidence_type,
        "created_at": evidence.created_at.isoformat(),
    }


# ─── Timeline endpoints ─────────────────────────────────────────────────────

@router.get("/tasks/{task_id}/timeline")
async def get_dispute_timeline(
    task_id: UUID,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Return the full dispute event timeline for a task."""
    uid = UUID(user_id)

    task_res = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_res.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    # Access check
    user_res = await db.execute(select(UserDB).where(UserDB.id == uid))
    user = user_res.scalar_one_or_none()
    is_admin = user and user.is_admin
    is_owner = task.user_id == uid
    asgn_res = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.task_id == task_id,
            TaskAssignmentDB.worker_id == uid,
        )
    )
    is_worker = asgn_res.scalar_one_or_none() is not None

    if not (is_admin or is_owner or is_worker):
        raise HTTPException(403, "Access denied")

    ev_res = await db.execute(
        select(DisputeEventDB)
        .where(DisputeEventDB.task_id == task_id)
        .order_by(DisputeEventDB.created_at.asc())
    )
    events = ev_res.scalars().all()

    return [
        {
            "id": str(e.id),
            "event_type": e.event_type,
            "description": e.description,
            "actor_id": str(e.actor_id) if e.actor_id else None,
            "metadata": e.event_metadata or {},
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


# ─── Admin: assign mediator ────────────────────────────────────────────────

@router.post("/admin/tasks/{task_id}/assign-mediator", status_code=200)
async def assign_mediator(
    task_id: UUID,
    mediator_user_id: UUID = Body(..., embed=True),
    admin_id: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Admin: assign a mediator user to a disputed task."""
    task_res = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_res.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    if task.dispute_status != "disputed":
        raise HTTPException(400, "Task is not currently in disputed state")

    # Verify mediator is admin
    mediator_res = await db.execute(select(UserDB).where(UserDB.id == mediator_user_id))
    mediator = mediator_res.scalar_one_or_none()
    if not mediator:
        raise HTTPException(404, "Mediator user not found")
    if not mediator.is_admin:
        raise HTTPException(400, "Mediator must be an admin user")

    task.mediator_id = mediator_user_id
    await _log_dispute_event(
        db, task_id, "mediator_assigned",
        f"Mediator {mediator.email} assigned to resolve dispute",
        actor_id=UUID(admin_id),
        metadata={"mediator_id": str(mediator_user_id), "mediator_email": mediator.email},
    )
    await db.commit()

    # Notify mediator
    await create_notification(
        db=db,
        user_id=str(mediator_user_id),
        type=NotifType.SYSTEM,
        title="You've been assigned as mediator",
        body=f"Please review and resolve the dispute for task {str(task_id)[:8]}…",
        link=f"/dashboard/disputes",
    )
    await db.commit()

    logger.info("mediator_assigned", task_id=str(task_id), mediator_id=str(mediator_user_id))
    return {
        "task_id": str(task_id),
        "mediator_id": str(mediator_user_id),
        "mediator_email": mediator.email,
        "message": "Mediator assigned successfully",
    }
