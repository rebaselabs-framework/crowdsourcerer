"""
Task dependency graph — tasks can block other tasks until they complete.

When a task has one or more dependencies that are not yet completed/failed,
it is held in `pending` status by the sweeper.  Once ALL upstream tasks
reach a terminal state (`completed` or `failed`), the sweeper unblocks the
dependent task (ai→queued, human→open).

Endpoints
---------
POST   /v1/tasks/{id}/dependencies          — declare a dependency
GET    /v1/tasks/{id}/dependencies          — list all upstream deps for a task
GET    /v1/tasks/{id}/dependents            — list downstream tasks waiting on this one
DELETE /v1/tasks/{id}/dependencies/{dep_id} — remove a dependency edge
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from core.auth import get_current_user_id
from core.scopes import require_scope, SCOPE_TASKS_READ, SCOPE_TASKS_WRITE
from core.database import get_db
from models.db import TaskDB, TaskDependencyDB
from models.schemas import TaskDependencyOut, AddDependencyRequest

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/tasks", tags=["task-dependencies"])


# ── helpers ────────────────────────────────────────────────────────────────

def _fmt(dep: TaskDependencyDB, upstream: TaskDB) -> TaskDependencyOut:
    title = (
        upstream.input.get("title", upstream.type.replace("_", " ").title())
        if isinstance(upstream.input, dict) else upstream.type
    )
    return TaskDependencyOut(
        id=dep.id,
        task_id=dep.task_id,
        depends_on_id=dep.depends_on_id,
        depends_on_title=title,
        depends_on_status=upstream.status,
        created_at=dep.created_at,
    )


async def _get_owned_task(task_id: uuid.UUID, user_id: str, db: AsyncSession) -> TaskDB:
    result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if str(task.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Not your task")
    return task


# ── endpoints ─────────────────────────────────────────────────────────────


@router.post("/{task_id}/dependencies", response_model=TaskDependencyOut, status_code=201)
async def add_dependency(
    task_id: uuid.UUID,
    body: AddDependencyRequest,
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """
    Declare that *task_id* must wait for *depends_on_id* to finish before
    it can be executed.  The dependent task must be in `pending` status.
    Self-dependencies and cycles (direct) are rejected.
    """
    if task_id == body.depends_on_id:
        raise HTTPException(status_code=400, detail="A task cannot depend on itself")

    task = await _get_owned_task(task_id, user_id, db)

    if task.status not in ("pending",):
        raise HTTPException(
            status_code=400,
            detail=f"Dependencies can only be added to pending tasks (current: {task.status})",
        )

    # Upstream task must belong to same user
    upstream_result = await db.execute(select(TaskDB).where(TaskDB.id == body.depends_on_id))
    upstream = upstream_result.scalar_one_or_none()
    if not upstream:
        raise HTTPException(status_code=404, detail="Upstream task not found")
    if str(upstream.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Cannot depend on another user's task")

    # Check for direct cycle: does upstream already depend on task_id?
    cycle_check = await db.execute(
        select(TaskDependencyDB).where(
            TaskDependencyDB.task_id == body.depends_on_id,
            TaskDependencyDB.depends_on_id == task_id,
        )
    )
    if cycle_check.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Adding this dependency would create a cycle")

    # Idempotent — already exists?
    existing = await db.execute(
        select(TaskDependencyDB).where(
            TaskDependencyDB.task_id == task_id,
            TaskDependencyDB.depends_on_id == body.depends_on_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Dependency already exists")

    dep = TaskDependencyDB(
        id=uuid.uuid4(),
        task_id=task_id,
        depends_on_id=body.depends_on_id,
    )
    db.add(dep)
    await db.commit()
    await db.refresh(dep)

    logger.info(
        "task_dependency.added",
        task_id=str(task_id),
        depends_on_id=str(body.depends_on_id),
        user_id=user_id,
    )
    return _fmt(dep, upstream)


@router.get("/{task_id}/dependencies", response_model=list[TaskDependencyOut])
async def list_dependencies(
    task_id: uuid.UUID,
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
    db: AsyncSession = Depends(get_db),
):
    """List all upstream tasks that *task_id* is waiting on."""
    await _get_owned_task(task_id, user_id, db)

    result = await db.execute(
        select(TaskDependencyDB, TaskDB)
        .join(TaskDB, TaskDependencyDB.depends_on_id == TaskDB.id)
        .where(TaskDependencyDB.task_id == task_id)
        .order_by(TaskDependencyDB.created_at)
        .limit(200)  # safety cap
    )
    return [_fmt(dep, upstream) for dep, upstream in result.all()]


@router.get("/{task_id}/dependents", response_model=list[TaskDependencyOut])
async def list_dependents(
    task_id: uuid.UUID,
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
    db: AsyncSession = Depends(get_db),
):
    """List all downstream tasks that are waiting for *task_id* to finish."""
    await _get_owned_task(task_id, user_id, db)

    result = await db.execute(
        select(TaskDependencyDB, TaskDB)
        .join(TaskDB, TaskDependencyDB.task_id == TaskDB.id)
        .where(TaskDependencyDB.depends_on_id == task_id)
        .order_by(TaskDependencyDB.created_at)
        .limit(200)  # safety cap
    )
    return [_fmt(dep, downstream) for dep, downstream in result.all()]


@router.delete("/{task_id}/dependencies/{dep_id}", status_code=204, response_model=None)
async def remove_dependency(
    task_id: uuid.UUID,
    dep_id: uuid.UUID,
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Remove a dependency edge."""
    await _get_owned_task(task_id, user_id, db)

    result = await db.execute(
        select(TaskDependencyDB).where(
            TaskDependencyDB.id == dep_id,
            TaskDependencyDB.task_id == task_id,
        )
    )
    dep = result.scalar_one_or_none()
    if not dep:
        raise HTTPException(status_code=404, detail="Dependency not found")

    await db.delete(dep)
    await db.commit()
    logger.info("task_dependency.removed", dep_id=str(dep_id), task_id=str(task_id))
