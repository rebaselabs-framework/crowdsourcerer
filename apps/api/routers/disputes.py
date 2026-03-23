"""Dispute resolution and consensus management for multi-worker human tasks."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.database import get_db
from core.notify import create_notification, NotifType
from models.db import TaskDB, UserDB, TaskAssignmentDB
from models.schemas import (
    ConsensusStateOut, ConsensusVoteOut,
    DisputeResolveRequest, DisputeResolveResponse,
    TaskOut, PaginatedTasks,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/disputes", tags=["disputes"])


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

    if task.dispute_status not in ("disputed",) and task.execution_mode != "human":
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
