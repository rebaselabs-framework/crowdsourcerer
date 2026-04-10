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

import asyncio
import datetime as dt_module
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog
from sqlalchemy import case, select, and_, func, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.db import TaskDB, TaskAssignmentDB, UserDB, SLABreachDB, TaskDependencyDB, NotificationDB, TaskWatchlistDB, NotificationPreferencesDB
from core.background import safe_create_task
from core.webhooks import fire_webhook, fire_webhook_for_task
from core.notify import create_notification, NotifType

logger = structlog.get_logger()

SWEEP_INTERVAL_SECONDS = 300  # 5 minutes

# Module-level state for digest tracking
_last_digest_date: Optional[dt_module.date] = None
_last_daily_digest_date: Optional[dt_module.date] = None
_last_streak_reset_date: Optional[dt_module.date] = None

# Track last sweep time for health dashboard
_LAST_SWEEP_AT: Optional[datetime] = None


async def sweep_once(session_factory: async_sessionmaker) -> dict:
    """Run a single sweep pass. Returns a summary dict.

    Optimised: pre-loads all workers, tasks, and active-assignment counts in
    bulk (3 queries total) rather than issuing per-assignment queries (N+1).
    """
    now = datetime.now(timezone.utc)
    timed_out_assignments: list[str] = []
    reopened_tasks: list[str] = []
    errors: list[str] = []

    async with session_factory() as db:
        try:
            # ── Find expired active assignments ──────────────────────────
            # skip_locked=True prevents two concurrent sweeper instances from
            # both picking up the same expired assignment and firing duplicate
            # timeout notifications / double-reopening tasks.
            result = await db.execute(
                select(TaskAssignmentDB).where(
                    and_(
                        TaskAssignmentDB.status == "active",
                        TaskAssignmentDB.timeout_at != None,  # noqa: E711
                        TaskAssignmentDB.timeout_at <= now,
                    )
                ).with_for_update(skip_locked=True)
            )
            expired = list(result.scalars().all())
            if not expired:
                await db.commit()
                return {
                    "swept_at": now.isoformat(),
                    "timed_out": 0,
                    "reopened": 0,
                    "errors": 0,
                    "assignment_ids": [],
                    "task_ids": [],
                }

            # ── Bulk-load all workers and tasks referenced by expired assignments ──
            worker_ids = list({a.worker_id for a in expired if a.worker_id})
            task_ids_needed = list({a.task_id for a in expired if a.task_id})

            workers_res = await db.execute(
                select(UserDB).where(UserDB.id.in_(worker_ids))
            )
            workers_by_id: dict = {str(u.id): u for u in workers_res.scalars()}

            tasks_res = await db.execute(
                select(TaskDB).where(TaskDB.id.in_(task_ids_needed))
            )
            tasks_by_id: dict = {str(t.id): t for t in tasks_res.scalars()}

            # ── Bulk-load active assignment counts per task ──────────────
            # One query returning (task_id, count) pairs
            active_counts_res = await db.execute(
                select(TaskAssignmentDB.task_id, func.count().label("cnt"))
                .where(
                    and_(
                        TaskAssignmentDB.task_id.in_(task_ids_needed),
                        TaskAssignmentDB.status.in_(["active", "submitted", "approved"]),
                    )
                )
                .group_by(TaskAssignmentDB.task_id)
            )
            active_counts: dict = {str(row.task_id): row.cnt for row in active_counts_res}

            # ── Bulk-load requester emails for notifications ─────────────
            requester_ids = list({tasks_by_id[str(a.task_id)].user_id
                                   for a in expired
                                   if str(a.task_id) in tasks_by_id})
            requesters_res = await db.execute(
                select(UserDB).where(UserDB.id.in_(requester_ids))
            )
            requesters_by_id: dict = {str(u.id): u for u in requesters_res.scalars()}

            # ── Process each expired assignment ──────────────────────────
            for assignment in expired:
                try:
                    assignment.status = "timed_out"
                    assignment.released_at = now
                    timed_out_assignments.append(str(assignment.id))

                    # Penalise worker reliability
                    worker = workers_by_id.get(str(assignment.worker_id))
                    if worker:
                        current_reliability = worker.worker_reliability or 1.0
                        worker.worker_reliability = round(current_reliability * 0.9, 4)

                    # Check the parent task
                    task = tasks_by_id.get(str(assignment.task_id))
                    if task and task.status == "assigned":
                        # After marking this assignment timed_out, how many remain?
                        # Subtract 1 because this assignment is no longer "active"
                        # Update the dict so subsequent iterations for the SAME task
                        # see the decremented count (not the stale original).
                        prev = active_counts.get(str(task.id), 0)
                        active_counts[str(task.id)] = max(0, prev - 1)
                        active_count = active_counts[str(task.id)]

                        if active_count < task.assignments_required:
                            task.status = "open"
                            reopened_tasks.append(str(task.id))
                            logger.info(
                                "sweeper.task_reopened",
                                task_id=str(task.id),
                                task_type=task.type,
                                active_remaining=active_count,
                                required=task.assignments_required,
                            )

                            # Notify the requester in-app
                            worker_name = (worker.name or "a worker") if worker else "a worker"
                            task_label = task.type.replace("_", " ")
                            await create_notification(
                                db,
                                task.user_id,
                                NotifType.TASK_TIMED_OUT,
                                "Worker timed out — task reopened ⏱️",
                                f"{worker_name.capitalize()} didn't complete your {task_label} task in time. "
                                "It's been reopened and is back in the marketplace.",
                                link=f"/dashboard/tasks/{task.id}",
                            )

                            # Fire email to requester (fire-and-forget)
                            requester = requesters_by_id.get(str(task.user_id))
                            if requester and requester.email:
                                safe_create_task(
                                    _notify_timeout_email(
                                        to_email=requester.email,
                                        user_id=str(requester.id),
                                        task_id=str(task.id),
                                        task_type=task.type,
                                        worker_name=worker_name,
                                    )
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


async def _notify_timeout_email(
    to_email: str, user_id: str, task_id: str, task_type: str, worker_name: str
) -> None:
    """Fire-and-forget: send task timeout email to requester."""
    try:
        from core.email import notify_task_timeout_gated
        await notify_task_timeout_gated(
            to_email=to_email,
            user_id=user_id,
            task_id=task_id,
            task_type=task_type,
            worker_name=worker_name,
        )
    except Exception:  # noqa: BLE001
        logger.exception("sweeper.timeout_email_failed", task_id=task_id)


async def _sweep_sla_breaches(session_factory: async_sessionmaker) -> int:
    """Check all open/assigned human tasks for SLA breaches and log them.

    Optimised: loads all relevant users in a single query (no N+1),
    and pre-loads already-breached task IDs to skip them without per-task queries.
    """
    from core.sla import compute_sla_deadline, get_sla_hours
    breached = 0
    now = datetime.now(timezone.utc)

    async with session_factory() as db:
        try:
            # Find human tasks still open/assigned (batch-limited to prevent OOM)
            res = await db.execute(
                select(TaskDB).where(
                    TaskDB.execution_mode == "human",
                    TaskDB.status.in_(["open", "assigned"]),
                ).limit(5000)
            )
            tasks = list(res.scalars().all())
            if not tasks:
                return 0

            # Bulk-load all user plans in a single query — avoid N+1
            user_ids = list({task.user_id for task in tasks})
            user_res = await db.execute(
                select(UserDB.id, UserDB.plan).where(UserDB.id.in_(user_ids))
            )
            user_plans: dict[str, str] = {
                str(row.id): (row.plan or "free") for row in user_res
            }

            # Bulk-load already-breached task IDs — avoid per-task count query
            task_ids = [task.id for task in tasks]
            already_breached_res = await db.execute(
                select(SLABreachDB.task_id).where(SLABreachDB.task_id.in_(task_ids))
            )
            already_breached: set = {row[0] for row in already_breached_res}

            for task in tasks:
                if task.user_id not in user_plans:
                    continue  # user not found / deleted
                if task.id in already_breached:
                    continue  # breach already recorded

                priority = task.priority or "normal"
                plan = user_plans[str(task.user_id)]
                deadline = compute_sla_deadline(task.created_at, plan, priority)

                if now <= deadline:
                    continue  # still within SLA

                breach = SLABreachDB(
                    task_id=task.id,
                    user_id=task.user_id,
                    plan=plan,
                    priority=priority,
                    sla_hours=get_sla_hours(plan, priority),
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
                    safe_create_task(fire_webhook_for_task(
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

            # ── Batch-load all per-user stats in ~5 queries (was N+1: 4-6 per user) ──

            # 1. Get all active users
            users_res = await db.execute(
                select(UserDB).where(UserDB.is_active == True, UserDB.is_banned == False).limit(10_000)
            )
            users = users_res.scalars().all()
            if not users:
                _last_digest_date = today
                return 0

            user_ids = [u.id for u in users]

            # 2. Batch-load notification preferences (skip users who opted out)
            prefs_res = await db.execute(
                select(NotificationPreferencesDB).where(
                    NotificationPreferencesDB.user_id.in_(user_ids)
                )
            )
            prefs_by_user = {p.user_id: p for p in prefs_res.scalars().all()}

            # 3. Batch-aggregate tasks created/completed per user this week
            from models.db import CreditTransactionDB
            task_stats_res = await db.execute(
                select(
                    TaskDB.user_id,
                    sqlfunc.count(TaskDB.id).label("created"),
                    sqlfunc.sum(
                        case((TaskDB.status == "completed", 1), else_=0)
                    ).label("completed"),
                )
                .where(TaskDB.user_id.in_(user_ids), TaskDB.created_at >= week_start)
                .group_by(TaskDB.user_id)
            )
            task_stats = {row.user_id: (row.created, int(row.completed or 0)) for row in task_stats_res}

            # 4. Batch-aggregate credits spent per user this week
            credit_stats_res = await db.execute(
                select(
                    CreditTransactionDB.user_id,
                    sqlfunc.abs(sqlfunc.sum(CreditTransactionDB.amount)).label("spent"),
                )
                .where(
                    CreditTransactionDB.user_id.in_(user_ids),
                    CreditTransactionDB.amount < 0,
                    CreditTransactionDB.created_at >= week_start,
                )
                .group_by(CreditTransactionDB.user_id)
            )
            credit_stats = {row.user_id: int(row.spent or 0) for row in credit_stats_res}

            # 5. Batch-aggregate worker stats (tasks approved + earnings) this week
            worker_stats_res = await db.execute(
                select(
                    TaskAssignmentDB.worker_id,
                    sqlfunc.count(TaskAssignmentDB.id).label("tasks_done"),
                    sqlfunc.sum(TaskAssignmentDB.earnings_credits).label("earnings"),
                )
                .where(
                    TaskAssignmentDB.worker_id.in_(user_ids),
                    TaskAssignmentDB.status == "approved",
                    TaskAssignmentDB.submitted_at >= week_start,
                )
                .group_by(TaskAssignmentDB.worker_id)
            )
            worker_stats = {
                row.worker_id: (row.tasks_done, int(row.earnings or 0))
                for row in worker_stats_res
            }

            # ── Now iterate users and send (no per-user queries) ──
            for user in users:
                try:
                    # Check digest preference
                    prefs = prefs_by_user.get(user.id)
                    if prefs and prefs.digest_frequency in ("none", "daily"):
                        continue

                    tasks_created, tasks_completed = task_stats.get(user.id, (0, 0))
                    credits_spent = credit_stats.get(user.id, 0)

                    is_worker = user.role in ("worker", "both")
                    worker_tasks, worker_earnings = worker_stats.get(user.id, (0, 0)) if is_worker else (0, 0)
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

            # ── Batch-optimized: load users + unread counts in 3 queries (was N+1) ──

            # 1. Get daily-digest user IDs
            prefs_res = await db.execute(
                select(NotificationPreferencesDB.user_id).where(
                    NotificationPreferencesDB.digest_frequency == "daily"
                ).limit(10_000)
            )
            daily_user_ids = [row[0] for row in prefs_res.all()]
            if not daily_user_ids:
                _last_daily_digest_date = today
                return 0

            # 2. Batch-load active users
            users_res = await db.execute(
                select(UserDB).where(
                    UserDB.id.in_(daily_user_ids),
                    UserDB.is_active == True,
                    UserDB.is_banned == False,
                )
            )
            users_by_id = {u.id: u for u in users_res.scalars().all()}

            # 3. Batch-count unread notifications per user in last 24h
            unread_counts_res = await db.execute(
                select(
                    NotificationDB.user_id,
                    sqlfunc.count(NotificationDB.id).label("cnt"),
                )
                .where(
                    NotificationDB.user_id.in_(list(users_by_id.keys())),
                    NotificationDB.is_read == False,
                    NotificationDB.created_at >= since,
                )
                .group_by(NotificationDB.user_id)
            )
            unread_counts = {row.user_id: row.cnt for row in unread_counts_res}

            from sqlalchemy import update as sa_update

            digest_sent_uids: list = []

            for uid, user in users_by_id.items():
                try:
                    unread_count = unread_counts.get(uid, 0)
                    if unread_count == 0:
                        continue  # Nothing to report

                    # Per-user: load top 8 highlights (can't batch LIMIT-per-user)
                    notifs_res = await db.execute(
                        select(NotificationDB)
                        .where(
                            NotificationDB.user_id == uid,
                            NotificationDB.is_read == False,
                            NotificationDB.created_at >= since,
                        )
                        .order_by(NotificationDB.created_at.desc())
                        .limit(8)
                    )
                    notifs = notifs_res.scalars().all()
                    highlights = [
                        {
                            "title": n.title or n.type,
                            "body": n.body or "",
                            "link": n.link or "/dashboard/notifications",
                        }
                        for n in notifs
                    ]

                    user_name = user.name or user.email.split("@")[0]
                    await send_daily_digest(
                        to_email=user.email,
                        user_name=user_name,
                        date_label=date_label,
                        unread_count=unread_count,
                        highlights=highlights,
                        credits_balance=user.credits,
                    )
                    digest_sent_uids.append(uid)
                    sent += 1
                except Exception:
                    logger.exception("daily_digest.user_error", user_id=str(uid))

            # Batch-update last_digest_sent_at (1 query instead of N)
            if digest_sent_uids:
                await db.execute(
                    sa_update(UserDB)
                    .where(UserDB.id.in_(digest_sent_uids))
                    .values(last_digest_sent_at=now)
                )

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
            # skip_locked=True: if two sweeper processes run concurrently, each
            # will only process rows the other hasn't locked yet, preventing
            # double-activation of the same scheduled task.
            result = await db.execute(
                select(TaskDB).where(
                    TaskDB.status == "pending",
                    TaskDB.scheduled_at.isnot(None),
                    TaskDB.scheduled_at <= now,
                ).with_for_update(skip_locked=True)
            )
            tasks = result.scalars().all()

            # Collect info for background tasks BEFORE commit (objects
            # expire after commit so we capture ids/fields up front).
            bg_tasks: list[tuple[str, str, str, str, str | None, int | None]] = []

            for task in tasks:
                if task.execution_mode == "ai":
                    task.status = "queued"
                else:
                    task.status = "open"
                bg_tasks.append((
                    str(task.id),
                    str(task.user_id),
                    task.execution_mode,
                    task.type,
                    task.priority,
                    task.worker_reward_credits,
                ))
                activated += 1
                logger.info(
                    "sweeper.scheduled_task_activated",
                    task_id=str(task.id),
                    task_type=task.type,
                    mode=task.execution_mode,
                )

            if activated:
                await db.commit()

            # Fire background tasks AFTER commit (locks released, rows
            # now visible to the background workers' own sessions).
            for tid, uid, mode, ttype, pri, reward in bg_tasks:
                if mode == "ai":
                    safe_create_task(_run_scheduled_ai_task(tid, uid))
                else:
                    safe_create_task(_notify_scheduled_human_task(
                        task_type=ttype,
                        priority=pri,
                        reward_credits=reward,
                    ))

        except Exception:  # noqa: BLE001
            logger.exception("sweeper.scheduled_sweep_error")
            await db.rollback()

    return activated


async def _run_scheduled_ai_task(task_id: str, user_id: str) -> None:
    """Thin wrapper to run a scheduled AI task (mirrors _run_task in tasks router)."""
    from core.database import AsyncSessionLocal
    from workers.base import get_rebasekit_client
    from workers.router import execute_task, TASK_CREDITS
    from core.webhooks import fire_webhook_for_task, fire_persistent_endpoints
    from models.db import TaskDB, UserDB, CreditTransactionDB
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
            client = get_rebasekit_client()
            output = await execute_task(task.type, task.input, client)
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
                safe_create_task(fire_webhook_for_task(
                    task=task, event_type="task.completed", extra=wh_extra,
                ))
            safe_create_task(fire_persistent_endpoints(
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
                    t2.completed_at = datetime.now(timezone.utc)

                    # Refund credits — mirrors _run_task error handling
                    refund_amount = TASK_CREDITS.get(t2.type, 5)
                    if t2.org_id:
                        from models.db import OrganizationDB
                        org_r = await db2.execute(
                            select(OrganizationDB).where(
                                OrganizationDB.id == t2.org_id
                            ).with_for_update()
                        )
                        org = org_r.scalar_one_or_none()
                        if org:
                            org.credits += refund_amount
                    else:
                        user_r = await db2.execute(
                            select(UserDB).where(
                                UserDB.id == user_id
                            ).with_for_update()
                        )
                        user = user_r.scalar_one_or_none()
                        if user:
                            user.credits += refund_amount

                    txn = CreditTransactionDB(
                        user_id=user_id,
                        task_id=t2.id,
                        amount=refund_amount,
                        type="refund",
                        description=f"Task failed: {t2.type}",
                    )
                    db2.add(txn)
                    logger.info(
                        "sweeper.scheduled_task_credits_refunded",
                        task_id=task_id,
                        amount=refund_amount,
                    )

                    # In-app notification for task owner
                    from core.notify import create_notification, NotifType
                    task_label = t2.type.replace("_", " ")
                    await create_notification(
                        db2, user_id,
                        NotifType.TASK_FAILED,
                        "Scheduled task failed — credits refunded ❌",
                        f"Your scheduled {task_label} task failed: {str(exc)[:100]}. "
                        f"{refund_amount} credits refunded.",
                        link=f"/dashboard/tasks/{task_id}",
                    )

                    await db2.commit()


async def _notify_scheduled_human_task(task_type: str, priority: str, reward_credits) -> None:
    from core.database import AsyncSessionLocal
    from routers.saved_searches import notify_matching_saved_searches
    async with AsyncSessionLocal() as db:
        try:
            await notify_matching_saved_searches(task_type, priority, reward_credits, db)
        except Exception:
            logger.warning(
                "sweeper.saved_search_notification_failed",
                task_type=task_type,
                exc_info=True,
            )


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
                ).with_for_update(skip_locked=True)
            )
            pending_tasks = tasks_result.scalars().all()

            TERMINAL = {"completed", "failed", "cancelled"}

            # Collect background tasks to fire after commit
            bg_ai_tasks: list[tuple[str, str]] = []

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
                if task.execution_mode == "ai":
                    task.status = "queued"
                    bg_ai_tasks.append((str(task.id), str(task.user_id)))
                else:
                    task.status = "open"
                unblocked += 1
                logger.info(
                    "sweeper.dependency_unblocked",
                    task_id=str(task.id),
                    mode=task.execution_mode,
                )

            if unblocked:
                await db.commit()

            # Fire background tasks after commit
            for tid, uid in bg_ai_tasks:
                safe_create_task(_run_scheduled_ai_task(tid, uid))

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
                ).with_for_update(skip_locked=True).limit(1_000)
            )
            tasks = result.scalars().all()

            # Collect webhook payloads before commit (task objects expire
            # after commit, so we capture all needed fields up front).
            webhook_payloads: list[dict] = []

            for task in tasks:
                priority = task.priority or "normal"
                if priority not in ESCALATION_MAP:
                    continue  # critical or unknown — skip

                new_priority, threshold = ESCALATION_MAP[priority]
                age = now - (task.created_at if task.created_at.tzinfo else task.created_at.replace(tzinfo=timezone.utc))
                if age < threshold:
                    continue  # not yet past SLA

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

                # Capture webhook info before commit (task objects expire
                # after commit so we capture all needed fields here).
                webhook_payloads.append({
                    "task_id": str(task.id),
                    "user_id": str(task.user_id),
                    "webhook_url": task.webhook_url,
                    "webhook_events": task.webhook_events,
                    "old_priority": old_priority,
                    "new_priority": new_priority,
                    "task_type": task.type,
                })

                escalated += 1
                logger.info(
                    "sweeper.priority_escalated",
                    task_id=str(task.id),
                    old_priority=old_priority,
                    new_priority=new_priority,
                )

            if escalated:
                await db.commit()

            # Fire webhook events after commit
            for wp in webhook_payloads:
                wh_extra = {
                    "old_priority": wp["old_priority"],
                    "new_priority": wp["new_priority"],
                    "escalated_at": now.isoformat(),
                    "task_type": wp["task_type"],
                }
                if wp["webhook_url"]:
                    safe_create_task(fire_webhook(
                        task_id=wp["task_id"],
                        user_id=wp["user_id"],
                        webhook_url=wp["webhook_url"],
                        webhook_events=wp["webhook_events"],
                        event_type="task.priority_escalated",
                        extra=wh_extra,
                    ))
                safe_create_task(fire_persistent_endpoints(
                    user_id=wp["user_id"],
                    task_id=wp["task_id"],
                    event_type="task.priority_escalated",
                    extra=wh_extra,
                ))

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


async def _sweep_stale_streaks(session_factory: async_sessionmaker) -> int:
    """Reset streak_days to 0 for workers who haven't been active since yesterday.

    Runs once per day (tracked via _last_streak_reset_date). Without this,
    a worker's streak count stays inflated indefinitely until they submit
    another task, which is misleading on leaderboards and profiles.
    """
    global _last_streak_reset_date  # noqa: PLW0603

    today = datetime.now(timezone.utc).date()
    if _last_streak_reset_date == today:
        return 0

    yesterday = today - timedelta(days=1)
    reset_count = 0

    try:
        async with session_factory() as db:
            # Find workers with active streaks whose last activity was before yesterday
            result = await db.execute(
                select(UserDB).where(
                    UserDB.worker_streak_days > 0,
                    UserDB.worker_last_active_date != None,  # noqa: E711
                    func.date(UserDB.worker_last_active_date) < yesterday,
                )
            )
            stale_workers = result.scalars().all()

            for worker in stale_workers:
                freezes = getattr(worker, "streak_freezes", 0) or 0
                if freezes > 0:
                    # Consume a streak freeze — preserve the streak
                    worker.streak_freezes = freezes - 1
                    worker.streak_freezes_used = (getattr(worker, "streak_freezes_used", 0) or 0) + 1
                    logger.info(
                        "sweeper.streak_freeze_used",
                        user_id=str(worker.id),
                        streak=worker.worker_streak_days,
                        remaining_freezes=worker.streak_freezes,
                    )
                else:
                    worker.worker_streak_days = 0
                    reset_count += 1

            if stale_workers:
                await db.commit()
                logger.info("sweeper.stale_streaks_processed",
                            total=len(stale_workers), reset=reset_count)

        _last_streak_reset_date = today
    except Exception:
        logger.exception("sweeper.stale_streaks_error")

    return reset_count


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
            from core.system_alerts import record_sweep_success  # noqa: PLC0415
            record_sweep_success()
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.unhandled_error")
            from core.system_alerts import record_sweep_error  # noqa: PLC0415
            record_sweep_error()

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

        # Clean up expired refresh tokens (once per cycle, lightweight)
        try:
            from core.refresh_tokens import cleanup_expired_tokens
            async with session_factory() as sess:
                cleaned = await cleanup_expired_tokens(sess)
                if cleaned:
                    logger.info("sweeper.refresh_tokens_cleaned", count=cleaned)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.refresh_token_cleanup_error")

        # Check and fire system health alerts every cycle
        try:
            from core.system_alerts import check_and_fire_alerts  # noqa: PLC0415
            await check_and_fire_alerts(session_factory)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.alert_check_error")

        # Reset stale streaks once per day
        try:
            reset = await _sweep_stale_streaks(session_factory)
            if reset:
                logger.info("sweeper.streaks_reset_done", count=reset)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.streak_reset_error")

        # Process league season transitions (Monday mornings)
        try:
            from routers.leagues import process_season_end
            league_processed = await process_season_end(session_factory)
            if league_processed:
                logger.info("sweeper.league_season_processed", count=league_processed)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper.league_season_error")

        await asyncio.sleep(min(interval, trigger_check_interval))


# ── Module-level reference so we can cancel/inspect from outside ──────────
_sweeper_task: Optional[asyncio.Task] = None


def start_sweeper(session_factory: async_sessionmaker, interval: int = SWEEP_INTERVAL_SECONDS):
    """Start the sweeper as an asyncio background task. Call once at startup."""
    global _sweeper_task  # noqa: PLW0603
    _sweeper_task = safe_create_task(
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
