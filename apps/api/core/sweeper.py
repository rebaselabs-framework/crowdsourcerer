"""Background sweeper — expires timed-out task assignments and reopens stalled tasks.

Runs as an asyncio background task started in the FastAPI startup hook.
Interval: every SWEEP_INTERVAL_SECONDS (default 5 minutes).

What it does:
  1. Find all task assignments where status='active' and timeout_at <= now()
  2. Mark each as 'timed_out'
  3. For each parent task: if the task still has active/submitted capacity,
     decrement assignments_completed (since the worker bailed) and if the task
     has open assignment slots, set it back to 'open'.
  4. Log a summary per sweep run.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.db import TaskDB, TaskAssignmentDB, UserDB

logger = structlog.get_logger()

SWEEP_INTERVAL_SECONDS = 300  # 5 minutes


async def sweep_once(session_factory: async_sessionmaker) -> dict:
    """Run a single sweep pass. Returns a summary dict."""
    now = datetime.now(timezone.utc)
    timed_out_assignments: list[str] = []
    reopened_tasks: list[str] = []
    errors: list[str] = []

    async with session_factory() as db:
        try:
            # ── Find expired active assignments ──────────────────────────
            result = await db.execute(
                select(TaskAssignmentDB).where(
                    and_(
                        TaskAssignmentDB.status == "active",
                        TaskAssignmentDB.timeout_at != None,  # noqa: E711
                        TaskAssignmentDB.timeout_at <= now,
                    )
                )
            )
            expired = result.scalars().all()

            for assignment in expired:
                try:
                    assignment.status = "timed_out"
                    assignment.released_at = now
                    timed_out_assignments.append(str(assignment.id))

                    # Penalise worker reliability slightly
                    worker_result = await db.execute(
                        select(UserDB).where(UserDB.id == assignment.worker_id)
                    )
                    worker = worker_result.scalar_one_or_none()
                    if worker:
                        current_reliability = worker.worker_reliability or 1.0
                        # Exponential moving average: new = 0.9*old + 0.1*0.0 (timeout = 0 score)
                        worker.worker_reliability = round(current_reliability * 0.9, 4)

                    # Check the parent task
                    task_result = await db.execute(
                        select(TaskDB).where(TaskDB.id == assignment.task_id)
                    )
                    task = task_result.scalar_one_or_none()
                    if task and task.status == "assigned":
                        # Count remaining active/submitted assignments
                        active_count = await db.scalar(
                            select(func.count()).where(
                                and_(
                                    TaskAssignmentDB.task_id == task.id,
                                    TaskAssignmentDB.status.in_(["active", "submitted", "approved"]),
                                )
                            )
                        ) or 0

                        if active_count < task.assignments_required:
                            # Reopen the task so another worker can claim it
                            task.status = "open"
                            reopened_tasks.append(str(task.id))
                            logger.info(
                                "sweeper.task_reopened",
                                task_id=str(task.id),
                                task_type=task.type,
                                active_remaining=active_count,
                                required=task.assignments_required,
                            )

                except Exception as exc:  # noqa: BLE001
                    errors.append(f"assignment:{assignment.id}: {exc}")
                    logger.exception("sweeper.assignment_error", assignment_id=str(assignment.id))

            await db.commit()

        except Exception as exc:  # noqa: BLE001
            errors.append(f"sweep_pass: {exc}")
            logger.exception("sweeper.pass_error")
            await db.rollback()

    summary = {
        "swept_at": now.isoformat(),
        "timed_out": len(timed_out_assignments),
        "reopened": len(reopened_tasks),
        "errors": len(errors),
        "assignment_ids": timed_out_assignments,
        "task_ids": reopened_tasks,
    }

    if timed_out_assignments or errors:
        logger.info(
            "sweeper.pass_complete",
            timed_out=len(timed_out_assignments),
            reopened=len(reopened_tasks),
            errors=len(errors),
        )

    return summary


async def run_sweeper(session_factory: async_sessionmaker, interval: int = SWEEP_INTERVAL_SECONDS):
    """Infinite loop: sweep, sleep, repeat. Designed to run as an asyncio background task."""
    logger.info("sweeper.started", interval_seconds=interval)
    while True:
        try:
            await sweep_once(session_factory)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.unhandled_error")
        await asyncio.sleep(interval)


# ── Module-level reference so we can cancel/inspect from outside ──────────
_sweeper_task: Optional[asyncio.Task] = None


def start_sweeper(session_factory: async_sessionmaker, interval: int = SWEEP_INTERVAL_SECONDS):
    """Start the sweeper as an asyncio background task. Call once at startup."""
    global _sweeper_task  # noqa: PLW0603
    _sweeper_task = asyncio.create_task(
        run_sweeper(session_factory, interval),
        name="assignment-timeout-sweeper",
    )
    return _sweeper_task


def stop_sweeper():
    """Cancel the sweeper background task. Call at shutdown."""
    if _sweeper_task and not _sweeper_task.done():
        _sweeper_task.cancel()


def get_sweeper_task() -> Optional[asyncio.Task]:
    """Return the current sweeper task (for admin/health inspection)."""
    return _sweeper_task
