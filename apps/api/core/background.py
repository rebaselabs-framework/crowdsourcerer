"""Utilities for fire-and-forget background tasks.

asyncio.create_task() spawns a coroutine but swallows its exceptions by
default — they're only surfaced as generic RuntimeWarning when the task is
garbage-collected.  Using safe_create_task() instead attaches a done-callback
that logs any unhandled exception at ERROR level with structured context, so
failures are visible in the application log.

Usage:
    from core.background import safe_create_task

    # Instead of:
    asyncio.create_task(send_notification(...))

    # Use:
    safe_create_task(send_notification(...), name="notify.task_completed")
"""

import asyncio
from typing import Coroutine, Any, Optional

import structlog

logger = structlog.get_logger()


def _done_callback(task: asyncio.Task) -> None:
    """Log any unhandled exception at ERROR level."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "background_task.unhandled_exception",
            task_name=task.get_name(),
            error=str(exc),
            exc_info=exc,
        )


def safe_create_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: Optional[str] = None,
) -> asyncio.Task:
    """Create an asyncio Task and attach an error-logging done callback.

    Equivalent to ``asyncio.create_task(coro, name=name)`` except that any
    exception raised inside the coroutine is logged at ERROR level rather than
    being silently discarded.
    """
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_done_callback)
    return task
