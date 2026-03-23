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
import datetime as dt_module
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.db import TaskDB, TaskAssignmentDB, UserDB, SLABreachDB, TaskDependencyDB, NotificationDB, TaskWatchlistDB, NotificationPreferencesDB
from core.webhooks import fire_webhook_for_task

logger = structlog.get_logger()

SWEEP_INTERVAL_SECONDS = 300  # 5 minutes

# Module-level state for digest tracking
_last_digest_date: Optional[dt_module.date] = None
_last_daily_digest_date: Optional[dt_module.date] = None

# Track last sweep time for health dashboard
_LAST_SWEEP_AT: Optional[datetime] = None


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


async def _sweep_sla_breaches(session_factory: async_sessionmaker) -> int:
    """Check all open/assigned human tasks for SLA breaches and log them."""
    from core.sla import compute_sla_deadline
    breached = 0
    now = datetime.now(timezone.utc)

    async with session_factory() as db:
        try:
            # Find human tasks still open/assigned
            res = await db.execute(
                select(TaskDB).where(
                    TaskDB.execution_mode == "human",
                    TaskDB.status.in_(["open", "assigned"]),
                )
            )
            tasks = list(res.scalars().all())

            for task in tasks:
                priority = task.priority or "normal"

                # Get the requester's plan
                user_res = await db.execute(select(UserDB).where(UserDB.id == task.user_id))
                user = user_res.scalar_one_or_none()
                if not user:
                    continue

                plan = user.plan or "free"
                deadline = compute_sla_deadline(task.created_at, plan, priority)

                if now <= deadline:
                    continue  # still within SLA

                # Check if already recorded
                existing = await db.scalar(
                    select(func.count()).where(SLABreachDB.task_id == task.id)
                )
                if existing:
                    continue

                breach = SLABreachDB(
                    task_id=task.id,
                    user_id=user.id,
                    plan=plan,
                    priority=priority,
                    sla_hours=(now - task.created_at).total_seconds() / 3600,
                    task_created_at=task.created_at,
                    breach_at=deadline,
                )
                db.add(breach)
                breached += 1
                logger.warning(
                    "sweeper.sla_breach",
                    task_id=str(task.id),
                    plan=plan,
                    priority=priority,
                )
                # Fire sla.breach webhook if task has one
                if task.webhook_url:
                    asyncio.create_task(fire_webhook_for_task(
                        task=task,
                        event_type="sla.breach",
                        extra={"plan": plan, "priority": priority,
                               "breach_at": deadline.isoformat(),
                               "overdue_hours": round(
                                   (now - deadline).total_seconds() / 3600, 2)},
                    ))

            if breached:
                await db.commit()

        except Exception:  # noqa: BLE001
            logger.exception("sweeper.sla_breach_error")
            await db.rollback()

    return breached


async def send_weekly_digests(session_factory) -> int:
    """Send weekly digest emails to all active users. Returns count sent."""
    global _last_digest_date
    now = datetime.now(timezone.utc)
    today = now.date()

    # Only run on Mondays between 8:00–9:00 UTC
    if now.weekday() != 0 or now.hour != 8:
        return 0
    if _last_digest_date == today:
        return 0  # Already sent today

    from core.email import send_weekly_digest
    sent = 0
    week_start = now - timedelta(days=7)
    week_label = f"{week_start.strftime('%b %d')} – {now.strftime('%b %d, %Y')}"

    async with session_factory() as db:
        try:
            # Get top 5 workers this week (global)
            from sqlalchemy import func as sqlfunc
            top_workers_res = await db.execute(
                select(UserDB.id, UserDB.name, UserDB.email,
                       sqlfunc.count(TaskAssignmentDB.id).label("task_count"),
                       sqlfunc.sum(TaskAssignmentDB.earnings_credits).label("earnings"))
                .join(TaskAssignmentDB, TaskAssignmentDB.worker_id == UserDB.id)
                .where(
                    TaskAssignmentDB.status == "approved",
                    TaskAssignmentDB.submitted_at >= week_start,
                )
                .group_by(UserDB.id, UserDB.name, UserDB.email)
                .order_by(sqlfunc.count(TaskAssignmentDB.id).desc())
                .limit(5)
            )
            top_workers = [
                {"name": r.name or r.email.split("@")[0], "tasks": r.task_count, "earnings": r.earnings or 0}
                for r in top_workers_res
            ]

            # Get all active users
            users_res = await db.execute(
                select(UserDB).where(UserDB.is_active == True, UserDB.is_banned == False)
            )
            users = users_res.scalars().all()

            for user in users:
                try:
                    # Check if user has opted out of weekly digest
                    prefs_res = await db.execute(
                        select(NotificationPreferencesDB).where(
                            NotificationPreferencesDB.user_id == user.id
                        )
                    )
                    prefs = prefs_res.scalar_one_or_none()
                    # Skip if digest is disabled or set to daily (daily users get their own digest)
                    if prefs:
                        if prefs.digest_frequency in ("none", "daily"):
                            continue
                    else:
                        # No prefs row means default (weekly) — proceed

                    # User's tasks this week
                    tasks_created = await db.scalar(
                        select(sqlfunc.count(TaskDB.id)).where(
                            TaskDB.user_id == user.id,
                            TaskDB.created_at >= week_start,
                        )
                    ) or 0
                    tasks_completed = await db.scalar(
                        select(sqlfunc.count(TaskDB.id)).where(
                            TaskDB.user_id == user.id,
                            TaskDB.status == "completed",
                            TaskDB.updated_at >= week_start,
                        )
                    ) or 0

                    # Credits spent (negative transactions)
                    from models.db import CreditTransactionDB
                    credits_spent = await db.scalar(
                        select(sqlfunc.abs(sqlfunc.sum(CreditTransactionDB.amount))).where(
                            CreditTransactionDB.user_id == user.id,
                            CreditTransactionDB.amount < 0,
                            CreditTransactionDB.created_at >= week_start,
                        )
                    ) or 0

                    # Worker stats
                    worker_tasks = 0
                    worker_earnings = 0
                    worker_xp_gained = 0
                    is_worker = user.role in ("worker", "both")

                    if is_worker:
                        worker_tasks = await db.scalar(
                            select(sqlfunc.count(TaskAssignmentDB.id)).where(
                                TaskAssignmentDB.worker_id == user.id,
                                TaskAssignmentDB.status == "approved",
                                TaskAssignmentDB.submitted_at >= week_start,
                            )
                        ) or 0
                        worker_earnings_res = await db.scalar(
                            select(sqlfunc.sum(TaskAssignmentDB.earnings_credits)).where(
                                TaskAssignmentDB.worker_id == user.id,
                                TaskAssignmentDB.status == "approved",
                                TaskAssignmentDB.submitted_at >= week_start,
                            )
                        )
                        worker_earnings = int(worker_earnings_res or 0)
                        # XP gained this week (approximate from tasks * 10)
                        worker_xp_gained = worker_tasks * 10

                    user_name = user.name or user.email.split("@")[0]
                    await send_weekly_digest(
                        to_email=user.email,
                        user_name=user_name,
                        week_label=week_label,
                        tasks_created=tasks_created,
                        tasks_completed=tasks_completed,
                        credits_spent=int(credits_spent),
                        credits_balance=user.credits,
                        top_workers=top_workers,
                        worker_tasks_done=worker_tasks,
                        worker_earnings=worker_earnings,
                        worker_xp=worker_xp_gained,
                        is_worker=is_worker,
                    )
                    sent += 1
                except Exception:
                    logger.exception("digest.user_error", user_id=str(user.id))

            _last_digest_date = today
            logger.info("digest.sent", count=sent, week=week_label)
        except Exception:
            logger.exception("digest.error")

    return sent


async def send_daily_digests(session_factory) -> int:
    """Send daily digest emails to users who opted in. Returns count sent.

    Runs every day between 8:00–9:00 UTC.
    Only sent if the user has ``digest_frequency='daily'`` and has unread
    notifications from the past 24 hours.
    """
    global _last_daily_digest_date
    now = datetime.now(timezone.utc)
    today = now.date()

    # Only run during 8:00–9:00 UTC
    if now.hour != 8:
        return 0
    if _last_daily_digest_date == today:
        return 0  # Already sent today

    from core.email import send_daily_digest
    sent = 0
    since = now - timedelta(hours=24)
    date_label = now.strftime("%A, %B %d, %Y")

    async with session_factory() as db:
        try:
            from sqlalchemy import func as sqlfunc

            # Get users with digest_frequency='daily' who have unread notifs
            prefs_res = await db.execute(
                select(NotificationPreferencesDB).where(
                    NotificationPreferencesDB.digest_frequency == "daily"
                )
            )
            all_daily_prefs = prefs_res.scalars().all()

            for prefs in all_daily_prefs:
                try:
                    user_res = await db.execute(
                        select(UserDB).where(
                            UserDB.id == prefs.user_id,
                            UserDB.is_active == True,
                            UserDB.is_banned == False,
                        )
                    )
                    user = user_res.scalar_one_or_none()
                    if not user:
                        continue

                    # Check unread notifications in last 24h
                    unread_count = await db.scalar(
                        select(sqlfunc.count(NotificationDB.id)).where(
                            NotificationDB.user_id == user.id,
                            NotificationDB.is_read == False,
                            NotificationDB.created_at >= since,
                        )
                    ) or 0

                    if unread_count == 0:
                        continue  # Nothing to report

                    # Get top 8 unread notifications as highlights
                    notifs_res = await db.execute(
                        select(NotificationDB)
                        .where(
                            NotificationDB.user_id == user.id,
                            NotificationDB.is_read == False,
                            NotificationDB.created_at >= since,
                        )
                        .order_by(NotificationDB.created_at.desc())
                        .limit(8)
                    )
                    notifs = notifs_res.scalars().all()
                    highlights = [
                        {
                            "title": n.title or n.notif_type,
                            "body": n.body or "",
                            "link": n.link or "/dashboard/notifications",
                        }
                        for n in notifs
                    ]

                    user_name = user.name or user.email.split("@")[0]

                    # Update last_digest_sent_at on user
                    await db.execute(
                        __import__("sqlalchemy").update(UserDB)
                        .where(UserDB.id == user.id)
                        .values(last_digest_sent_at=now)
                    )

                    await send_daily_digest(
                        to_email=user.email,
                        user_name=user_name,
                        date_label=date_label,
                        unread_count=unread_count,
                        highlights=highlights,
                        credits_balance=user.credits,
                    )
                    sent += 1
                except Exception:
                    logger.exception("daily_digest.user_error", user_id=str(prefs.user_id))

            await db.commit()
            _last_daily_digest_date = today
            logger.info("daily_digest.sent", count=sent, date=date_label)
        except Exception:
            logger.exception("daily_digest.error")

    return sent


async def _sweep_scheduled_tasks(session_factory: async_sessionmaker) -> int:
    """
    Activate scheduled tasks whose scheduled_at time has arrived.

    - AI tasks: pending → queued (background executor picks them up)
    - Human tasks: pending → open (appear in the worker marketplace)
    """
    from workers.router import execute_task
    activated = 0
    now = datetime.now(timezone.utc)

    async with session_factory() as db:
        try:
            result = await db.execute(
                select(TaskDB).where(
                    TaskDB.status == "pending",
                    TaskDB.scheduled_at.isnot(None),
                    TaskDB.scheduled_at <= now,
                )
            )
            tasks = result.scalars().all()

            for task in tasks:
                try:
                    if task.execution_mode == "ai":
                        task.status = "queued"
                        await db.commit()
                        # Fire off AI execution
                        asyncio.create_task(_run_scheduled_ai_task(str(task.id), str(task.user_id)))
                    else:
                        # Human task → publish to marketplace
                        task.status = "open"
                        await db.commit()
                        # Notify workers whose saved searches match
                        asyncio.create_task(_notify_scheduled_human_task(
                            task_type=task.type,
                            priority=task.priority,
                            reward_credits=task.worker_reward_credits,
                        ))
                    activated += 1
                    logger.info(
                        "sweeper.scheduled_task_activated",
                        task_id=str(task.id),
                        task_type=task.type,
                        mode=task.execution_mode,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("sweeper.scheduled_task_error", task_id=str(task.id))
                    await db.rollback()

        except Exception:  # noqa: BLE001
            logger.exception("sweeper.scheduled_sweep_error")
            await db.rollback()

    return activated


async def _run_scheduled_ai_task(task_id: str, user_id: str) -> None:
    """Thin wrapper to run a scheduled AI task (mirrors _run_task in tasks router)."""
    from core.database import AsyncSessionLocal
    from workers.router import execute_task, TASK_CREDITS
    from core.webhooks import fire_webhook_for_task, fire_persistent_endpoints
    from models.db import TaskDB, CreditTransactionDB
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
            task = result.scalar_one_or_none()
            if not task:
                return
            task.status = "running"
            task.started_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(task)
            # Execute
            import time as _time
            t0 = _time.perf_counter()
            output = await execute_task(task.type, task.input)
            duration_ms = int((_time.perf_counter() - t0) * 1000)
            task.status = "completed"
            task.output = output
            task.duration_ms = duration_ms
            task.completed_at = datetime.now(timezone.utc)

            # Build preview snippet for notifications
            from routers.tasks import _result_preview
            preview = _result_preview(output)
            task_label = task.type.replace("_", " ")
            notif_body = (
                f"Your scheduled {task_label} task finished in {duration_ms}ms."
                + (f" — {preview}" if preview else "")
            )
            from core.notify import create_notification, NotifType
            await create_notification(
                db, task.user_id,
                NotifType.TASK_COMPLETED,
                "Scheduled task completed ✅",
                notif_body,
                link=f"/dashboard/tasks/{task_id}",
            )
            await db.commit()

            # Fire webhooks with result_preview
            wh_extra = {
                "type": task.type,
                "duration_ms": duration_ms,
                **({"result_preview": preview} if preview else {}),
            }
            if task.webhook_url:
                asyncio.create_task(fire_webhook_for_task(
                    task=task, event_type="task.completed", extra=wh_extra,
                ))
            asyncio.create_task(fire_persistent_endpoints(
                user_id=str(task.user_id),
                task_id=str(task_id),
                event_type="task.completed",
                extra=wh_extra,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.exception("sweeper.scheduled_ai_task_failed", task_id=task_id)
            async with AsyncSessionLocal() as db2:
                r2 = await db2.execute(select(TaskDB).where(TaskDB.id == task_id))
                t2 = r2.scalar_one_or_none()
                if t2:
                    t2.status = "failed"
                    t2.error = str(exc)
                    await db2.commit()


async def _notify_scheduled_human_task(task_type: str, priority: str, reward_credits) -> None:
    from core.database import AsyncSessionLocal
    from routers.saved_searches import notify_matching_saved_searches
    async with AsyncSessionLocal() as db:
        try:
            await notify_matching_saved_searches(task_type, priority, reward_credits, db)
        except Exception:
            pass


async def _sweep_task_dependencies(session_factory: async_sessionmaker) -> int:
    """
    Unblock pending tasks whose dependency tasks have all reached a terminal state
    (completed or failed).

    For each pending task that has at least one dependency edge, check if every
    upstream task is in {completed, failed}.  If so, unblock the dependent task:
      - ai  → queued  (background executor picks it up)
      - human → open  (visible in marketplace)

    Returns the number of tasks unblocked this sweep.
    """
    from workers.router import execute_task
    unblocked = 0

    async with session_factory() as db:
        try:
            # Find all pending tasks that have at least one dependency
            pending_with_deps_result = await db.execute(
                select(TaskDependencyDB.task_id).distinct()
            )
            dep_task_ids = [r for r, in pending_with_deps_result.fetchall()]
            if not dep_task_ids:
                return 0

            tasks_result = await db.execute(
                select(TaskDB).where(
                    TaskDB.id.in_(dep_task_ids),
                    TaskDB.status == "pending",
                )
            )
            pending_tasks = tasks_result.scalars().all()

            TERMINAL = {"completed", "failed", "cancelled"}

            for task in pending_tasks:
                # Load all upstream dep statuses
                deps_result = await db.execute(
                    select(TaskDependencyDB, TaskDB)
                    .join(TaskDB, TaskDependencyDB.depends_on_id == TaskDB.id)
                    .where(TaskDependencyDB.task_id == task.id)
                )
                pairs = deps_result.all()
                if not pairs:
                    continue  # no deps — should not be here

                # All upstreams must be terminal
                all_done = all(upstream.status in TERMINAL for _, upstream in pairs)
                if not all_done:
                    continue

                # Unblock!
                try:
                    if task.execution_mode == "ai":
                        task.status = "queued"
                        await db.commit()
                        asyncio.create_task(_run_scheduled_ai_task(str(task.id), str(task.user_id)))
                    else:
                        task.status = "open"
                        await db.commit()
                    unblocked += 1
                    logger.info(
                        "sweeper.dependency_unblocked",
                        task_id=str(task.id),
                        mode=task.execution_mode,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("sweeper.dependency_unblock_error", task_id=str(task.id))
                    await db.rollback()

        except Exception:  # noqa: BLE001
            logger.exception("sweeper.dependency_sweep_error")

    return unblocked


async def _sweep_priority_escalation(session_factory: async_sessionmaker) -> int:
    """Auto-escalate priority for tasks that have been open/pending past SLA thresholds.

    Thresholds:
      low    open for 48 h → normal
      normal open for 24 h → high
      high   open for 12 h → critical
      critical: no escalation

    Each task is only escalated once (tracked via priority_escalated_at).
    Fires an in-app notification and a task.priority_escalated webhook event.
    """
    from core.notify import create_notification, NotifType
    from core.webhooks import fire_persistent_endpoints

    ESCALATION_MAP = {
        "low":    ("normal",   timedelta(hours=48)),
        "normal": ("high",     timedelta(hours=24)),
        "high":   ("critical", timedelta(hours=12)),
    }

    escalated = 0
    now = datetime.now(timezone.utc)

    async with session_factory() as db:
        try:
            result = await db.execute(
                select(TaskDB).where(
                    TaskDB.status.in_(["open", "pending"]),
                    TaskDB.priority_escalated_at.is_(None),  # only escalate once
                )
            )
            tasks = result.scalars().all()

            for task in tasks:
                priority = task.priority or "normal"
                if priority not in ESCALATION_MAP:
                    continue  # critical or unknown — skip

                new_priority, threshold = ESCALATION_MAP[priority]
                age = now - (task.created_at if task.created_at.tzinfo else task.created_at.replace(tzinfo=timezone.utc))
                if age < threshold:
                    continue  # not yet past SLA

                try:
                    old_priority = task.priority
                    task.priority = new_priority
                    task.priority_escalated_at = now

                    # In-app notification for task owner
                    await create_notification(
                        db,
                        task.user_id,
                        NotifType.SYSTEM,
                        "Task priority auto-escalated",
                        f"Task priority auto-escalated to {new_priority}",
                        link=f"/dashboard/tasks/{task.id}",
                    )

                    await db.commit()

                    # Fire webhook event
                    wh_extra = {
                        "old_priority": old_priority,
                        "new_priority": new_priority,
                        "escalated_at": now.isoformat(),
                        "task_type": task.type,
                    }
                    if task.webhook_url:
                        asyncio.create_task(fire_webhook_for_task(
                            task=task,
                            event_type="task.priority_escalated",
                            extra=wh_extra,
                        ))
                    asyncio.create_task(fire_persistent_endpoints(
                        user_id=str(task.user_id),
                        task_id=str(task.id),
                        event_type="task.priority_escalated",
                        extra=wh_extra,
                    ))

                    escalated += 1
                    logger.info(
                        "sweeper.priority_escalated",
                        task_id=str(task.id),
                        old_priority=old_priority,
                        new_priority=new_priority,
                    )

                except Exception:  # noqa: BLE001
                    logger.exception("sweeper.priority_escalation_error", task_id=str(task.id))
                    await db.rollback()

        except Exception:  # noqa: BLE001
            logger.exception("sweeper.priority_escalation_sweep_error")
            await db.rollback()

    return escalated


async def _sweep_watchlist_notifications(session_factory: async_sessionmaker) -> int:
    """Notify workers when tasks they bookmarked have become open/pending again.

    Fires once per (worker, task) pair — won't re-notify if already sent within 24 h.
    """
    notified = 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    async with session_factory() as db:
        try:
            # Find watchlist items pointing to open/pending tasks
            result = await db.execute(
                select(TaskWatchlistDB)
                .join(TaskDB, TaskDB.id == TaskWatchlistDB.task_id)
                .where(
                    TaskDB.status.in_(["open", "pending"]),
                    # Either never notified or notified > 24 h ago
                    (
                        (TaskWatchlistDB.notified_at == None)  # noqa: E711
                        | (TaskWatchlistDB.notified_at < cutoff)
                    ),
                )
            )
            items = result.scalars().all()

            for item in items:
                try:
                    task_r = await db.execute(
                        select(TaskDB).where(TaskDB.id == item.task_id)
                    )
                    task = task_r.scalar_one_or_none()
                    if not task:
                        continue

                    notif = NotificationDB(
                        user_id=item.worker_id,
                        type="watchlist_alert",
                        title="A task on your watchlist is available!",
                        body=(
                            f"'{task.type.replace('_', ' ').title()}' is now "
                            f"{task.status} and ready to claim."
                        ),
                        link=f"/worker/marketplace",
                        is_read=False,
                    )
                    db.add(notif)
                    item.notified_at = now
                    notified += 1
                except Exception:
                    logger.exception(
                        "sweeper.watchlist_notify_error",
                        watchlist_id=str(item.id),
                    )

            if notified:
                await db.commit()
        except Exception:
            logger.exception("sweeper.watchlist_sweep_error")
            await db.rollback()

    return notified


async def run_sweeper(session_factory: async_sessionmaker, interval: int = SWEEP_INTERVAL_SECONDS):
    """Infinite loop: sweep, sleep, repeat. Designed to run as an asyncio background task."""
    global _LAST_SWEEP_AT  # noqa: PLW0603
    logger.info("sweeper.started", interval_seconds=interval)
    # Track which cycle we're on so schedule triggers run more frequently (every 60s)
    trigger_check_interval = 60  # seconds
    last_trigger_check = 0.0

    while True:
        _LAST_SWEEP_AT = datetime.now(timezone.utc)
        try:
            await sweep_once(session_factory)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.unhandled_error")

        # Activate any tasks whose scheduled_at has arrived
        try:
            activated = await _sweep_scheduled_tasks(session_factory)
            if activated:
                logger.info("sweeper.scheduled_activated", count=activated)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.scheduled_check_error")

        # Unblock tasks whose dependencies are all complete
        try:
            unblocked = await _sweep_task_dependencies(session_factory)
            if unblocked:
                logger.info("sweeper.dependency_unblocked_total", count=unblocked)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.dependency_check_error")

        # Check SLA breaches on every sweep pass
        try:
            await _sweep_sla_breaches(session_factory)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.sla_check_error")

        # Auto-escalate task priorities past SLA thresholds
        try:
            escalated = await _sweep_priority_escalation(session_factory)
            if escalated:
                logger.info("sweeper.priority_escalated_total", count=escalated)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.priority_escalation_check_error")

        # Notify workers about watchlisted tasks becoming available
        try:
            watched = await _sweep_watchlist_notifications(session_factory)
            if watched:
                logger.info("sweeper.watchlist_notified", count=watched)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.watchlist_check_error")

        # Send weekly digest on Monday mornings
        try:
            digest_count = await send_weekly_digests(session_factory)
            if digest_count:
                logger.info("digest.weekly_sent", count=digest_count)
        except Exception:
            logger.exception("digest.weekly_error")

        # Send daily digest every morning at 8am UTC
        try:
            daily_count = await send_daily_digests(session_factory)
            if daily_count:
                logger.info("digest.daily_sent", count=daily_count)
        except Exception:
            logger.exception("digest.daily_error")

        # Check schedule triggers every ~60 seconds (regardless of sweep interval)
        import time
        now_ts = time.monotonic()
        if now_ts - last_trigger_check >= trigger_check_interval:
            try:
                from routers.triggers import run_due_schedule_triggers
                fired = await run_due_schedule_triggers(session_factory)
                if fired:
                    logger.info("sweeper.triggers_fired", count=fired)
            except Exception:  # noqa: BLE001
                logger.exception("sweeper.trigger_check_error")
            last_trigger_check = now_ts

        await asyncio.sleep(min(interval, trigger_check_interval))


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
