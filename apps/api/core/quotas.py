"""Per-plan rate limits and quota enforcement.

Plans and their daily limits:
    free       → 10 tasks/day,  2 pipelines total,   2 pipeline runs/day
    starter    → 100 tasks/day, 10 pipelines total,  20 pipeline runs/day
    pro        → 500 tasks/day, unlimited pipelines, 100 pipeline runs/day
    enterprise → unlimited
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import RateLimitBucketDB, UserDB, TaskPipelineDB

logger = structlog.get_logger()

# ── Plan quota definitions ──────────────────────────────────────────────────

PLAN_QUOTAS: dict[str, dict[str, Optional[int]]] = {
    "free": {
        "tasks_per_day": 10,
        "pipelines_total": 2,
        "pipeline_runs_per_day": 2,
        "batch_task_size": 10,        # max tasks per batch call
        "max_worker_assignments": 1,  # max workers per human task
    },
    "starter": {
        "tasks_per_day": 100,
        "pipelines_total": 10,
        "pipeline_runs_per_day": 20,
        "batch_task_size": 25,
        "max_worker_assignments": 5,
    },
    "pro": {
        "tasks_per_day": 500,
        "pipelines_total": None,        # unlimited
        "pipeline_runs_per_day": 100,
        "batch_task_size": 50,
        "max_worker_assignments": 20,
    },
    "enterprise": {
        "tasks_per_day": None,          # unlimited
        "pipelines_total": None,
        "pipeline_runs_per_day": None,
        "batch_task_size": 50,
        "max_worker_assignments": 100,
    },
}

PLAN_DISPLAY = {
    "free": "Free",
    "starter": "Starter",
    "pro": "Pro",
    "enterprise": "Enterprise",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today_key(prefix: str) -> str:
    today = _utcnow().strftime("%Y-%m-%d")
    return f"{prefix}:{today}"


def _month_key(prefix: str) -> str:
    month = _utcnow().strftime("%Y-%m")
    return f"{prefix}:{month}"


def get_plan_quota(plan: str, key: str) -> Optional[int]:
    """Return the limit for a given plan+key. None = unlimited."""
    return PLAN_QUOTAS.get(plan, PLAN_QUOTAS["free"]).get(key)


# ── Bucket increment helper ─────────────────────────────────────────────────

async def _increment_bucket(
    db: AsyncSession,
    user_id: str,
    bucket_key: str,
    reset_at: datetime,
) -> int:
    """Increment and return the new count in a rate-limit bucket."""
    uid = uuid.UUID(user_id)
    result = await db.execute(
        select(RateLimitBucketDB).where(
            RateLimitBucketDB.user_id == uid,
            RateLimitBucketDB.bucket_key == bucket_key,
        )
    )
    bucket = result.scalar_one_or_none()

    if bucket is None:
        bucket = RateLimitBucketDB(
            id=uuid.uuid4(),
            user_id=uid,
            bucket_key=bucket_key,
            count=1,
            reset_at=reset_at,
        )
        db.add(bucket)
        await db.flush()
        return 1
    else:
        # Reset if past reset_at
        if _utcnow() > bucket.reset_at:
            bucket.count = 1
            bucket.reset_at = reset_at
        else:
            bucket.count += 1
        await db.flush()
        return bucket.count


async def _get_bucket_count(
    db: AsyncSession,
    user_id: str,
    bucket_key: str,
) -> int:
    uid = uuid.UUID(user_id)
    result = await db.execute(
        select(RateLimitBucketDB).where(
            RateLimitBucketDB.user_id == uid,
            RateLimitBucketDB.bucket_key == bucket_key,
        )
    )
    bucket = result.scalar_one_or_none()
    if not bucket or _utcnow() > bucket.reset_at:
        return 0
    return bucket.count


# ── Public quota check + enforce functions ─────────────────────────────────

async def enforce_task_creation_quota(
    db: AsyncSession,
    user_id: str,
    user_plan: str,
    task_count: int = 1,
) -> None:
    """Raise HTTP 429 if creating `task_count` tasks would exceed daily limit."""
    limit = get_plan_quota(user_plan, "tasks_per_day")
    if limit is None:
        return  # unlimited

    bucket_key = _today_key("tasks")
    tomorrow = (_utcnow() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # Check BEFORE incrementing so we don't count the rejected request
    current = await _get_bucket_count(db, user_id, bucket_key)
    if current + task_count > limit:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limit_exceeded",
                "message": f"Daily task limit reached for {PLAN_DISPLAY[user_plan]} plan ({limit}/day). "
                           f"You have used {current} today.",
                "limit": limit,
                "used": current,
                "plan": user_plan,
                "resets_at": tomorrow.isoformat(),
                "upgrade_url": "/pricing",
            },
        )


async def record_task_creation(
    db: AsyncSession,
    user_id: str,
    task_count: int = 1,
) -> None:
    """Increment the daily task counter. Call AFTER successful task creation."""
    bucket_key = _today_key("tasks")
    tomorrow = (_utcnow() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    for _ in range(task_count):
        await _increment_bucket(db, user_id, bucket_key, tomorrow)


async def enforce_pipeline_total_quota(
    db: AsyncSession,
    user_id: str,
    user_plan: str,
) -> None:
    """Raise HTTP 429 if user has hit their max pipelines-total limit."""
    limit = get_plan_quota(user_plan, "pipelines_total")
    if limit is None:
        return

    count = await db.scalar(
        select(func.count()).where(TaskPipelineDB.user_id == uuid.UUID(user_id))
    )
    if (count or 0) >= limit:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "pipeline_limit_exceeded",
                "message": f"Pipeline limit reached for {PLAN_DISPLAY[user_plan]} plan ({limit} pipelines). "
                           "Delete an existing pipeline or upgrade your plan.",
                "limit": limit,
                "used": count,
                "plan": user_plan,
                "upgrade_url": "/pricing",
            },
        )


async def enforce_pipeline_run_quota(
    db: AsyncSession,
    user_id: str,
    user_plan: str,
) -> None:
    """Raise HTTP 429 if user has hit their daily pipeline-runs limit."""
    limit = get_plan_quota(user_plan, "pipeline_runs_per_day")
    if limit is None:
        return

    bucket_key = _today_key("pipeline_runs")
    current = await _get_bucket_count(db, user_id, bucket_key)
    if current >= limit:
        tomorrow = (_utcnow() + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "pipeline_run_limit_exceeded",
                "message": f"Daily pipeline run limit reached for {PLAN_DISPLAY[user_plan]} plan ({limit}/day). "
                           f"You have run {current} pipelines today.",
                "limit": limit,
                "used": current,
                "plan": user_plan,
                "resets_at": tomorrow.isoformat(),
                "upgrade_url": "/pricing",
            },
        )


async def record_pipeline_run(
    db: AsyncSession,
    user_id: str,
) -> None:
    """Increment the daily pipeline-run counter. Call AFTER successful run start."""
    bucket_key = _today_key("pipeline_runs")
    tomorrow = (_utcnow() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    await _increment_bucket(db, user_id, bucket_key, tomorrow)


def enforce_batch_size(user_plan: str, task_count: int) -> None:
    """Raise HTTP 400 if batch size exceeds plan limit (sync, no DB needed)."""
    limit = get_plan_quota(user_plan, "batch_task_size") or 50
    if task_count > limit:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "batch_size_exceeded",
                "message": f"Batch size {task_count} exceeds limit of {limit} for {PLAN_DISPLAY[user_plan]} plan.",
                "limit": limit,
                "upgrade_url": "/pricing",
            },
        )


async def get_quota_status(
    db: AsyncSession,
    user_id: str,
    user_plan: str,
) -> dict:
    """Return a full quota status summary for the current user."""
    tasks_limit = get_plan_quota(user_plan, "tasks_per_day")
    tasks_used = await _get_bucket_count(db, user_id, _today_key("tasks"))

    runs_limit = get_plan_quota(user_plan, "pipeline_runs_per_day")
    runs_used = await _get_bucket_count(db, user_id, _today_key("pipeline_runs"))

    pipelines_limit = get_plan_quota(user_plan, "pipelines_total")
    pipelines_used = await db.scalar(
        select(func.count()).where(TaskPipelineDB.user_id == uuid.UUID(user_id))
    ) or 0

    return {
        "plan": user_plan,
        "tasks": {
            "used": tasks_used,
            "limit": tasks_limit,
            "unlimited": tasks_limit is None,
        },
        "pipeline_runs": {
            "used": runs_used,
            "limit": runs_limit,
            "unlimited": runs_limit is None,
        },
        "pipelines_total": {
            "used": pipelines_used,
            "limit": pipelines_limit,
            "unlimited": pipelines_limit is None,
        },
        "batch_task_size": get_plan_quota(user_plan, "batch_task_size"),
        "max_worker_assignments": get_plan_quota(user_plan, "max_worker_assignments"),
    }
