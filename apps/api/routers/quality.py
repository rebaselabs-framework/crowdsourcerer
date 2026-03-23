"""Quality control — gold standard tasks and worker accuracy evaluation."""
from __future__ import annotations
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.database import get_db
from models.db import TaskDB, UserDB, TaskAssignmentDB
from models.schemas import GoldStandardCreateRequest, QualityReportOut

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/quality", tags=["quality"])


# ─── Answer comparison helpers ────────────────────────────────────────────

def _compare_answers(task_type: str, worker_response: Any, gold_answer: Any) -> bool:
    """
    Returns True if the worker's answer matches the gold standard answer.
    Uses task-type-specific comparison logic.
    """
    if not isinstance(worker_response, dict) or not isinstance(gold_answer, dict):
        return str(worker_response).strip().lower() == str(gold_answer).strip().lower()

    task_type = task_type.lower()

    # label_image / label_text: compare "label" or "labels" field
    if task_type in ("label_image", "label_text"):
        wl = worker_response.get("label") or worker_response.get("labels")
        gl = gold_answer.get("label") or gold_answer.get("labels")
        if isinstance(wl, list) and isinstance(gl, list):
            return sorted([str(x).lower() for x in wl]) == sorted([str(x).lower() for x in gl])
        return str(wl).lower() == str(gl).lower()

    # rate_quality: compare "rating" within ±1 tolerance
    if task_type == "rate_quality":
        try:
            wr = float(worker_response.get("rating", 0))
            gr = float(gold_answer.get("rating", 0))
            return abs(wr - gr) <= 1.0
        except (TypeError, ValueError):
            return False

    # verify_fact: compare "verdict" field (true/false/unsupported)
    if task_type == "verify_fact":
        wv = str(worker_response.get("verdict", "")).lower()
        gv = str(gold_answer.get("verdict", "")).lower()
        return wv == gv

    # moderate_content: compare "decision" field
    if task_type == "moderate_content":
        wd = str(worker_response.get("decision", "")).lower()
        gd = str(gold_answer.get("decision", "")).lower()
        return wd == gd

    # compare_rank: compare "ranked_ids" list order (exact match)
    if task_type == "compare_rank":
        wr = worker_response.get("ranked_ids", [])
        gr = gold_answer.get("ranked_ids", [])
        return [str(x) for x in wr] == [str(x) for x in gr]

    # answer_question / transcription_review: fuzzy comparison on text
    if task_type in ("answer_question", "transcription_review"):
        wa = str(worker_response.get("answer") or worker_response.get("text") or "").lower().strip()
        ga = str(gold_answer.get("answer") or gold_answer.get("text") or "").lower().strip()
        # Simple: require at least 80% character overlap (rough approximation)
        if not wa or not ga:
            return wa == ga
        common = sum(1 for c in wa if c in ga)
        similarity = common / max(len(wa), len(ga))
        return similarity >= 0.80

    # Default: exact dict equality
    return worker_response == gold_answer


async def _update_accuracy(worker: UserDB, db: AsyncSession) -> None:
    """Recompute worker accuracy from all evaluated gold standard assignments."""
    # Count approved/rejected assignments on gold standard tasks
    result = await db.execute(
        select(TaskAssignmentDB, TaskDB)
        .join(TaskDB, TaskAssignmentDB.task_id == TaskDB.id)
        .where(
            TaskAssignmentDB.worker_id == worker.id,
            TaskAssignmentDB.status.in_(["approved", "rejected"]),
            TaskDB.is_gold_standard == True,
        )
    )
    rows = result.all()

    if not rows:
        return

    approved_count = sum(1 for a, _ in rows if a.status == "approved")
    total_count = len(rows)
    worker.worker_accuracy = approved_count / total_count


# ─── Routes ───────────────────────────────────────────────────────────────

@router.post("/gold-standard")
async def mark_gold_standard(
    req: GoldStandardCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Mark a task as gold standard with the known correct answer.
    Only the task requester can do this.
    """
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == req.task_id, TaskDB.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or not owned by you")
    if task.execution_mode != "human":
        raise HTTPException(status_code=400, detail="Gold standards only apply to human tasks")

    task.is_gold_standard = True
    task.gold_answer = req.gold_answer
    await db.commit()

    logger.info("gold_standard_set", task_id=str(req.task_id), user_id=user_id)
    return {"task_id": str(req.task_id), "message": "Task marked as gold standard"}


@router.post("/evaluate/{task_id}")
async def evaluate_submissions(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Evaluate all submitted answers for a gold standard task.
    Marks assignments as approved/rejected and updates worker accuracy.
    """
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or not owned by you")
    if not task.is_gold_standard or not task.gold_answer:
        raise HTTPException(status_code=400, detail="Task is not a gold standard task")

    # Get all submitted assignments
    result = await db.execute(
        select(TaskAssignmentDB).where(
            TaskAssignmentDB.task_id == task_id,
            TaskAssignmentDB.status == "submitted",
        )
    )
    assignments = result.scalars().all()

    if not assignments:
        return {"evaluated": 0, "message": "No submitted assignments to evaluate"}

    evaluated = 0
    approved = 0
    rejected_workers: list[str] = []
    approved_workers: list[str] = []

    for assignment in assignments:
        is_correct = _compare_answers(
            task.type,
            assignment.response,
            task.gold_answer,
        )
        assignment.status = "approved" if is_correct else "rejected"
        evaluated += 1
        if is_correct:
            approved += 1
            approved_workers.append(str(assignment.worker_id))
        else:
            rejected_workers.append(str(assignment.worker_id))

    # Update accuracy for all workers who were evaluated
    all_worker_ids = {a.worker_id for a in assignments}
    for wid in all_worker_ids:
        w_result = await db.execute(select(UserDB).where(UserDB.id == wid))
        worker = w_result.scalar_one_or_none()
        if worker:
            await _update_accuracy(worker, db)

    await db.commit()

    logger.info(
        "gold_standard_evaluated",
        task_id=str(task_id),
        evaluated=evaluated,
        approved=approved,
    )

    return {
        "task_id": str(task_id),
        "evaluated": evaluated,
        "approved": approved,
        "rejected": evaluated - approved,
        "approval_rate": approved / evaluated if evaluated > 0 else 0,
    }


@router.get("/report", response_model=list[QualityReportOut])
async def get_quality_report(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get quality report for all workers who completed your gold standard tasks."""
    # Find all gold standard tasks owned by this requester
    result = await db.execute(
        select(TaskDB).where(
            TaskDB.user_id == user_id,
            TaskDB.is_gold_standard == True,
        )
    )
    gold_tasks = result.scalars().all()
    if not gold_tasks:
        return []

    gold_task_ids = [t.id for t in gold_tasks]

    # Find all workers who completed these tasks
    result = await db.execute(
        select(
            TaskAssignmentDB.worker_id,
            func.count().label("total"),
            func.sum(
                func.cast(TaskAssignmentDB.status == "approved", func.Integer())
                if hasattr(func, "Integer") else 1
            ).label("approved_count"),
        )
        .where(
            TaskAssignmentDB.task_id.in_(gold_task_ids),
            TaskAssignmentDB.status.in_(["approved", "rejected"]),
        )
        .group_by(TaskAssignmentDB.worker_id)
    )
    rows = result.all()

    report: list[QualityReportOut] = []
    for row in rows:
        w_result = await db.execute(select(UserDB).where(UserDB.id == row.worker_id))
        worker = w_result.scalar_one_or_none()
        if not worker:
            continue

        # Count correct (approved) assignments for this worker on these tasks
        correct_result = await db.execute(
            select(func.count()).where(
                TaskAssignmentDB.worker_id == row.worker_id,
                TaskAssignmentDB.task_id.in_(gold_task_ids),
                TaskAssignmentDB.status == "approved",
            )
        )
        correct_count = correct_result.scalar() or 0

        total_result = await db.execute(
            select(func.count()).where(
                TaskAssignmentDB.worker_id == row.worker_id,
                TaskAssignmentDB.task_id.in_(gold_task_ids),
                TaskAssignmentDB.status.in_(["approved", "rejected"]),
            )
        )
        total_count = total_result.scalar() or 0

        accuracy = correct_count / total_count if total_count > 0 else 0.0

        from routers.worker import compute_level
        level, _ = compute_level(worker.worker_xp)

        report.append(QualityReportOut(
            worker_id=worker.id,
            name=worker.name,
            tasks_evaluated=total_count,
            tasks_correct=correct_count,
            accuracy=accuracy,
            reliability=worker.worker_reliability,
            worker_level=level,
            worker_xp=worker.worker_xp,
        ))

    return sorted(report, key=lambda r: r.accuracy, reverse=True)
