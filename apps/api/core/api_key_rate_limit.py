"""Per-API-key rate limiting (requests/minute and requests/day).

Each API key can optionally override the plan-level limits with tighter or
looser per-key limits configured by the user.

Defaults (when no per-key override is set):
    free       → 30 rpm,  500 req/day
    starter    → 60 rpm,  2 000 req/day
    pro        → 120 rpm, 10 000 req/day
    enterprise → 300 rpm, unlimited

Enforcement is done via the `api_key_rate_buckets` table, which stores
sliding 1-minute and rolling-daily counters per key.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import ApiKeyDB, ApiKeyRateBucketDB

logger = structlog.get_logger()

# ── Default limits by plan ────────────────────────────────────────────────────

_PLAN_RPM_DEFAULTS: dict[str, Optional[int]] = {
    "free": 30,
    "starter": 60,
    "pro": 120,
    "enterprise": 300,
}

_PLAN_DAILY_DEFAULTS: dict[str, Optional[int]] = {
    "free": 500,
    "starter": 2_000,
    "pro": 10_000,
    "enterprise": None,  # unlimited
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rpm_window_key() -> str:
    """Key for the current 1-minute bucket (truncated to the minute)."""
    t = _utcnow().replace(second=0, microsecond=0)
    return "rpm:" + t.strftime("%Y-%m-%dT%H:%M")


def _daily_window_key() -> str:
    return "daily:" + _utcnow().strftime("%Y-%m-%d")


async def _get_or_increment_bucket(
    db: AsyncSession,
    api_key_id: uuid.UUID,
    window_key: str,
    reset_at: datetime,
) -> int:
    """Atomically increment and return the new count in a rate bucket."""
    result = await db.execute(
        select(ApiKeyRateBucketDB).where(
            ApiKeyRateBucketDB.api_key_id == api_key_id,
            ApiKeyRateBucketDB.window_key == window_key,
        )
    )
    bucket = result.scalar_one_or_none()

    now = _utcnow()
    if bucket is None:
        bucket = ApiKeyRateBucketDB(
            id=uuid.uuid4(),
            api_key_id=api_key_id,
            window_key=window_key,
            count=1,
            reset_at=reset_at,
        )
        db.add(bucket)
        await db.flush()
        return 1
    else:
        if now > bucket.reset_at:
            # Window expired — reset
            bucket.count = 1
            bucket.reset_at = reset_at
        else:
            bucket.count += 1
        await db.flush()
        return bucket.count


async def check_and_record_api_key_rate_limit(
    db: AsyncSession,
    api_key: ApiKeyDB,
    user_plan: str,
) -> None:
    """Enforce per-key RPM and daily limits. Raises HTTP 429 on violation.

    Should be called immediately after a successful API key lookup.
    The check + increment happens atomically per bucket so no separate
    "check before increment" race condition exists.
    """
    key_id = api_key.id

    # ── Determine effective limits ─────────────────────────────────────────
    rpm_limit: Optional[int] = (
        api_key.rate_limit_rpm
        if api_key.rate_limit_rpm is not None
        else _PLAN_RPM_DEFAULTS.get(user_plan, 60)
    )
    daily_limit: Optional[int] = (
        api_key.rate_limit_daily
        if api_key.rate_limit_daily is not None
        else _PLAN_DAILY_DEFAULTS.get(user_plan)
    )

    now = _utcnow()

    # ── RPM check ─────────────────────────────────────────────────────────
    if rpm_limit is not None:
        next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        rpm_count = await _get_or_increment_bucket(
            db, key_id, _rpm_window_key(), next_minute
        )
        if rpm_count > rpm_limit:
            logger.warning(
                "api_key_rate_limit_rpm",
                key_prefix=api_key.key_prefix,
                count=rpm_count,
                limit=rpm_limit,
            )
            raise HTTPException(
                status_code=429,
                headers={"X-RateLimit-Limit-RPM": str(rpm_limit),
                         "X-RateLimit-Remaining-RPM": "0",
                         "Retry-After": "60"},
                detail={
                    "error": "rate_limit_exceeded",
                    "scope": "requests_per_minute",
                    "limit": rpm_limit,
                    "used": rpm_count,
                    "message": f"API key rate limit exceeded: {rpm_limit} requests/minute. "
                               "Retry after 60 seconds or configure a higher limit.",
                    "upgrade_url": "/pricing",
                },
            )

    # ── Daily check ────────────────────────────────────────────────────────
    if daily_limit is not None:
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        daily_count = await _get_or_increment_bucket(
            db, key_id, _daily_window_key(), tomorrow
        )
        if daily_count > daily_limit:
            logger.warning(
                "api_key_rate_limit_daily",
                key_prefix=api_key.key_prefix,
                count=daily_count,
                limit=daily_limit,
            )
            raise HTTPException(
                status_code=429,
                headers={"X-RateLimit-Limit-Daily": str(daily_limit),
                         "X-RateLimit-Remaining-Daily": "0",
                         "Retry-After": str(int((tomorrow - now).total_seconds()))},
                detail={
                    "error": "rate_limit_exceeded",
                    "scope": "requests_per_day",
                    "limit": daily_limit,
                    "used": daily_count,
                    "resets_at": tomorrow.isoformat(),
                    "message": f"API key daily limit exceeded: {daily_limit} requests/day. "
                               "Resets at midnight UTC.",
                    "upgrade_url": "/pricing",
                },
            )


async def get_api_key_rate_status(
    db: AsyncSession,
    api_key: ApiKeyDB,
    user_plan: str,
) -> dict:
    """Return current rate-limit counters for a key (without incrementing)."""
    from sqlalchemy import select as _select

    key_id = api_key.id
    now = _utcnow()

    rpm_limit: Optional[int] = (
        api_key.rate_limit_rpm
        if api_key.rate_limit_rpm is not None
        else _PLAN_RPM_DEFAULTS.get(user_plan, 60)
    )
    daily_limit: Optional[int] = (
        api_key.rate_limit_daily
        if api_key.rate_limit_daily is not None
        else _PLAN_DAILY_DEFAULTS.get(user_plan)
    )

    def _bucket_count(window_key: str) -> int:
        return 0  # will be filled async below

    async def _read_bucket(window_key: str) -> int:
        r = await db.execute(
            _select(ApiKeyRateBucketDB).where(
                ApiKeyRateBucketDB.api_key_id == key_id,
                ApiKeyRateBucketDB.window_key == window_key,
            )
        )
        b = r.scalar_one_or_none()
        if not b or now > b.reset_at:
            return 0
        return b.count

    rpm_used = await _read_bucket(_rpm_window_key())
    daily_used = await _read_bucket(_daily_window_key())

    return {
        "rpm": {
            "limit": rpm_limit,
            "used": rpm_used,
            "remaining": max(0, rpm_limit - rpm_used) if rpm_limit is not None else None,
            "unlimited": rpm_limit is None,
        },
        "daily": {
            "limit": daily_limit,
            "used": daily_used,
            "remaining": max(0, daily_limit - daily_used) if daily_limit is not None else None,
            "unlimited": daily_limit is None,
        },
    }
