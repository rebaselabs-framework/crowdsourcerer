"""Server-Sent Events streaming endpoints for tasks.

Extracted from ``routers/tasks.py`` so the main router module can
focus on CRUD and lifecycle. These endpoints all decorate the same
``router`` instance imported from ``routers.tasks`` — FastAPI doesn't
care which Python module a route is registered from as long as the
decoration happens before ``app.include_router(...)`` is called.

Import order is critical: ``routers/tasks.py`` defines the ``router``
instance at module top, then triggers this module with a trailing
``from routers import tasks_streams`` import so the routes register
onto the already-built router.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from models.db import TaskDB
from routers.tasks import router


@router.get("/stream")
async def dashboard_task_stream(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Server-Sent Events stream for the task dashboard.

    Polls all of the current user's non-terminal tasks and emits events
    whenever any status, progress, or priority changes. Closes
    automatically once no active tasks remain (or after 10 minutes).

    A ``{"event": "heartbeat"}`` is sent every ~5 s to keep connections
    alive. A ``{"event": "stream_end"}`` marks the close.
    """
    TERMINAL = {"completed", "failed", "cancelled", "archived"}

    async def event_generator() -> AsyncIterator[str]:
        from core.database import AsyncSessionLocal

        poll_interval = 2.0
        max_polls = 300  # 10 minutes at 2s intervals
        snapshot: dict[str, tuple[str, int, str]] = {}
        heartbeat_counter = 0

        for _ in range(max_polls):
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(TaskDB).where(
                        TaskDB.user_id == user_id,
                        TaskDB.status.notin_(list(TERMINAL)),
                    )
                )
                active_tasks = result.scalars().all()

            for t in active_tasks:
                tid = str(t.id)
                current = (t.status, t.assignments_completed or 0, t.priority or "normal")
                if snapshot.get(tid) != current:
                    snapshot[tid] = current
                    payload = {
                        "task_id": tid,
                        "status": t.status,
                        "assignments_completed": t.assignments_completed or 0,
                        "assignments_required": t.assignments_required or 1,
                        "priority": t.priority or "normal",
                        "priority_escalated_at": (
                            t.priority_escalated_at.isoformat()
                            if getattr(t, "priority_escalated_at", None)
                            else None
                        ),
                        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                        "error": t.error,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"

            if not active_tasks:
                break

            heartbeat_counter += 1
            if heartbeat_counter % 3 == 0:
                yield 'data: {"event": "heartbeat"}\n\n'

            await asyncio.sleep(poll_interval)

        yield 'data: {"event": "stream_end"}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}/status-stream")
async def task_status_stream(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """SSE stream for real-time updates on a single task.

    Emits an event whenever status or progress changes, then closes
    once the task reaches a terminal state (or after 7.5 minutes).
    """
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_generator() -> AsyncIterator[str]:
        TERMINAL = {"completed", "failed", "cancelled"}
        last_status: str | None = None
        last_assignments_completed: int | None = None
        poll_interval = 1.5
        max_polls = 300

        from core.database import AsyncSessionLocal

        for _ in range(max_polls):
            async with AsyncSessionLocal() as session:
                r = await session.execute(select(TaskDB).where(TaskDB.id == task_id))
                t = r.scalar_one_or_none()
                if t is None:
                    break

                status_changed = t.status != last_status
                assignments_changed = t.assignments_completed != last_assignments_completed

                if status_changed or assignments_changed:
                    last_status = t.status
                    last_assignments_completed = t.assignments_completed
                    payload = {
                        "task_id": str(task_id),
                        "status": t.status,
                        "assignments_completed": t.assignments_completed,
                        "assignments_required": t.assignments_required,
                        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                        "error": t.error,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"

                    if t.status in TERMINAL:
                        break

            await asyncio.sleep(poll_interval)

        yield "data: {\"event\": \"stream_end\"}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}/output-stream")
async def task_output_stream(
    task_id: UUID,
    speed: float = Query(default=40.0, ge=1.0, le=500.0, description="Characters per second"),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Character-by-character SSE stream of a completed task's LLM output.

    Creates the effect of live token streaming for already-completed
    ``llm_generate`` (and similar text-output) tasks. While the task
    is still running, emits ``buffering`` heartbeats and waits up to
    10 minutes for completion before closing.

    Events:
      - ``{"event": "token", "char": "x", "position": N}``
      - ``{"event": "done"}``
      - ``{"event": "buffering"}``
      - ``{"event": "error", "detail": "..."}``
    """
    result = await db.execute(
        select(TaskDB).where(TaskDB.id == task_id, TaskDB.user_id == user_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    delay = 1.0 / speed  # seconds per character

    async def event_generator() -> AsyncIterator[str]:
        from core.database import AsyncSessionLocal
        TERMINAL = {"completed", "failed", "cancelled"}
        max_wait_polls = 300  # 10 minutes at 2s intervals

        # Phase 1: wait for task to reach a terminal state
        polls = 0
        while polls < max_wait_polls:
            async with AsyncSessionLocal() as session:
                r = await session.execute(select(TaskDB).where(TaskDB.id == task_id))
                t = r.scalar_one_or_none()
                if t is None:
                    yield 'data: {"event": "error", "detail": "Task not found"}\n\n'
                    return
                if t.status in TERMINAL:
                    break
            yield 'data: {"event": "buffering", "status": "' + (t.status or "pending") + '"}\n\n'
            await asyncio.sleep(2.0)
            polls += 1
        else:
            yield 'data: {"event": "error", "detail": "Timed out waiting for task completion"}\n\n'
            return

        # Phase 2: load output text
        async with AsyncSessionLocal() as session:
            r = await session.execute(select(TaskDB).where(TaskDB.id == task_id))
            t = r.scalar_one_or_none()

        if not t or t.status != "completed":
            error_detail = (t.error or "Task did not complete successfully") if t else "Task not found"
            yield f'data: {{"event": "error", "detail": {json.dumps(error_detail)}}}\n\n'
            return

        # Extract text from output — try common LLM output keys
        output_text = ""
        if t.output:
            for key in ("result", "text", "content", "answer", "response", "output", "summary"):
                val = t.output.get(key)
                if isinstance(val, str) and val.strip():
                    output_text = val
                    break
            if not output_text:
                output_text = json.dumps(t.output, ensure_ascii=False, indent=2)

        if not output_text:
            yield 'data: {"event": "done", "total_chars": 0}\n\n'
            return

        # Phase 3: stream characters
        for pos, char in enumerate(output_text):
            payload = json.dumps({"event": "token", "char": char, "position": pos})
            yield f"data: {payload}\n\n"
            await asyncio.sleep(delay)

        yield f'data: {{"event": "done", "total_chars": {len(output_text)}}}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
