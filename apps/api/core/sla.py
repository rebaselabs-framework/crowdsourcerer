"""SLA (Service Level Agreement) definitions and helpers.

SLA hours per plan + priority combination.
AI tasks complete immediately; SLA applies to human tasks in the marketplace.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional

# Base SLA hours by plan (time for a human task to be claimed + completed)
_PLAN_BASE_HOURS: dict[str, float] = {
    "free": 72.0,
    "starter": 24.0,
    "pro": 8.0,
    "enterprise": 2.0,
}

# Priority multiplier (lower = faster deadline)
_PRIORITY_MULTIPLIER: dict[str, float] = {
    "low": 2.0,      # double the base (more relaxed)
    "normal": 1.0,   # base SLA
    "high": 0.5,     # half the base (twice as fast)
    "urgent": 0.25,  # quarter of the base (4× urgency)
}

# Priority credit multiplier for requester (urgent tasks cost more to incentivise workers)
PRIORITY_CREDIT_MULTIPLIER: dict[str, float] = {
    "low": 0.75,
    "normal": 1.0,
    "high": 1.25,
    "urgent": 1.75,
}


def get_sla_hours(plan: str, priority: str = "normal") -> float:
    """Return SLA deadline in hours for a task with the given plan and priority."""
    base = _PLAN_BASE_HOURS.get(plan, 72.0)
    mult = _PRIORITY_MULTIPLIER.get(priority, 1.0)
    return round(base * mult, 2)


def compute_sla_deadline(created_at: datetime, plan: str, priority: str = "normal") -> datetime:
    """Return the absolute datetime by which the task must complete to meet SLA."""
    hours = get_sla_hours(plan, priority)
    return created_at + timedelta(hours=hours)


def is_sla_breached(created_at: datetime, plan: str, priority: str = "normal",
                    now: Optional[datetime] = None) -> bool:
    """Return True if the task has already exceeded its SLA."""
    if now is None:
        now = datetime.now(timezone.utc)
    deadline = compute_sla_deadline(created_at, plan, priority)
    return now > deadline


def sla_status(created_at: datetime, plan: str, priority: str = "normal",
               completed_at: Optional[datetime] = None) -> dict:
    """Return a SLA status dict for a task."""
    now = datetime.now(timezone.utc)
    deadline = compute_sla_deadline(created_at, plan, priority)
    sla_hours = get_sla_hours(plan, priority)

    if completed_at:
        met = completed_at <= deadline
        return {
            "status": "met" if met else "breached",
            "sla_hours": sla_hours,
            "deadline": deadline.isoformat(),
            "completed_at": completed_at.isoformat(),
        }
    elif now > deadline:
        overdue_hours = round((now - deadline).total_seconds() / 3600, 2)
        return {
            "status": "breached",
            "sla_hours": sla_hours,
            "deadline": deadline.isoformat(),
            "overdue_hours": overdue_hours,
        }
    else:
        remaining_hours = round((deadline - now).total_seconds() / 3600, 2)
        pct_elapsed = round((now - created_at).total_seconds() /
                            (deadline - created_at).total_seconds() * 100, 1)
        return {
            "status": "on_track",
            "sla_hours": sla_hours,
            "deadline": deadline.isoformat(),
            "remaining_hours": remaining_hours,
            "pct_elapsed": min(pct_elapsed, 100.0),
        }
