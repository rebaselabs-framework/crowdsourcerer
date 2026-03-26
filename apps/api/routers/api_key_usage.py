"""API Key usage analytics endpoints."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, and_, desc, case
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from models.db import ApiKeyDB, ApiKeyUsageLogDB
from models.schemas import ApiKeyUsageDetailOut, ApiKeyUsageOverviewOut

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/usage/overview", response_model=ApiKeyUsageOverviewOut)
async def get_usage_overview(
    days: int = Query(30, ge=1, le=365),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyUsageOverviewOut:
    """Overall usage summary across all API keys."""
    uid = UUID(user_id)
    since = utcnow() - timedelta(days=days)

    # Get all keys for this user
    keys_res = await db.execute(
        select(ApiKeyDB).where(ApiKeyDB.user_id == uid)
    )
    keys = keys_res.scalars().all()
    key_ids = [k.id for k in keys]

    if not key_ids:
        return ApiKeyUsageOverviewOut(total_requests=0, total_errors=0, total_credits_used=0, keys=[])

    # Total across all keys since `since`
    total_q = await db.execute(
        select(
            func.count().label("total"),
            func.coalesce(func.sum(ApiKeyUsageLogDB.credits_used), 0).label("credits"),
        ).where(
            ApiKeyUsageLogDB.api_key_id.in_(key_ids),
            ApiKeyUsageLogDB.created_at >= since,
        )
    )
    row = total_q.one()
    total_requests = row.total or 0
    total_credits = row.credits or 0

    total_errors_q = await db.execute(
        select(func.count()).where(
            ApiKeyUsageLogDB.api_key_id.in_(key_ids),
            ApiKeyUsageLogDB.created_at >= since,
            ApiKeyUsageLogDB.status_code >= 400,
        )
    )
    total_errors = total_errors_q.scalar_one() or 0

    # Per-key summary — single GROUP BY replaces the previous 2N-query loop.
    kstats_res = await db.execute(
        select(
            ApiKeyUsageLogDB.api_key_id,
            func.count().label("reqs"),
            func.coalesce(func.sum(ApiKeyUsageLogDB.credits_used), 0).label("credits"),
            func.sum(
                case((ApiKeyUsageLogDB.status_code >= 400, 1), else_=0)
            ).label("errs"),
        ).where(
            ApiKeyUsageLogDB.api_key_id.in_(key_ids),
            ApiKeyUsageLogDB.created_at >= since,
        ).group_by(ApiKeyUsageLogDB.api_key_id)
    )
    kstats: dict = {row.api_key_id: row for row in kstats_res}

    keys_summary = []
    for k in keys:
        s = kstats.get(k.id)
        keys_summary.append({
            "id": str(k.id),
            "name": k.name,
            "prefix": k.key_prefix,
            "requests": s.reqs if s else 0,
            "errors": s.errs if s else 0,
            "credits_used": s.credits if s else 0,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        })

    return ApiKeyUsageOverviewOut(
        total_requests=total_requests,
        total_errors=total_errors,
        total_credits_used=total_credits,
        keys=keys_summary,
    )


@router.get("/{key_id}/usage", response_model=ApiKeyUsageDetailOut)
async def get_key_usage(
    key_id: UUID,
    days: int = Query(30, ge=1, le=90),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyUsageDetailOut:
    """Per-key detailed usage breakdown."""
    uid = UUID(user_id)

    # Verify ownership
    key_res = await db.execute(
        select(ApiKeyDB).where(ApiKeyDB.id == key_id, ApiKeyDB.user_id == uid)
    )
    key = key_res.scalar_one_or_none()
    if not key:
        raise HTTPException(404, "API key not found")

    since = utcnow() - timedelta(days=days)

    # Totals
    totals = await db.execute(
        select(
            func.count().label("total"),
            func.coalesce(func.sum(ApiKeyUsageLogDB.credits_used), 0).label("credits"),
            func.avg(ApiKeyUsageLogDB.response_time_ms).label("avg_ms"),
        ).where(
            ApiKeyUsageLogDB.api_key_id == key_id,
            ApiKeyUsageLogDB.created_at >= since,
        )
    )
    t = totals.one()

    errors_q = await db.execute(
        select(func.count()).where(
            ApiKeyUsageLogDB.api_key_id == key_id,
            ApiKeyUsageLogDB.created_at >= since,
            ApiKeyUsageLogDB.status_code >= 400,
        )
    )
    total_errors = errors_q.scalar_one() or 0

    # Daily breakdown — use date truncation
    daily_q = await db.execute(
        select(
            func.date_trunc("day", ApiKeyUsageLogDB.created_at).label("day"),
            func.count().label("reqs"),
            func.sum(
                func.cast(ApiKeyUsageLogDB.status_code >= 400, type_=func.count().type)
            ).label("errs"),
            func.coalesce(func.sum(ApiKeyUsageLogDB.credits_used), 0).label("credits"),
            func.avg(ApiKeyUsageLogDB.response_time_ms).label("avg_ms"),
        ).where(
            ApiKeyUsageLogDB.api_key_id == key_id,
            ApiKeyUsageLogDB.created_at >= since,
        ).group_by("day").order_by("day")
    )
    daily_rows = daily_q.all()
    daily = [
        {
            "date": r.day.strftime("%Y-%m-%d"),
            "requests": r.reqs or 0,
            "errors": r.errs or 0,
            "credits_used": r.credits or 0,
            "avg_response_ms": round(r.avg_ms, 1) if r.avg_ms else None,
        }
        for r in daily_rows
    ]

    # Top endpoints
    endpoints_q = await db.execute(
        select(
            ApiKeyUsageLogDB.endpoint,
            ApiKeyUsageLogDB.method,
            func.count().label("reqs"),
            func.avg(ApiKeyUsageLogDB.response_time_ms).label("avg_ms"),
        ).where(
            ApiKeyUsageLogDB.api_key_id == key_id,
            ApiKeyUsageLogDB.created_at >= since,
        ).group_by(ApiKeyUsageLogDB.endpoint, ApiKeyUsageLogDB.method)
        .order_by(desc("reqs"))
        .limit(10)
    )
    ep_rows = endpoints_q.all()
    top_endpoints = [
        {
            "endpoint": r.endpoint,
            "method": r.method,
            "requests": r.reqs or 0,
            "errors": 0,
            "avg_response_ms": round(r.avg_ms, 1) if r.avg_ms else None,
        }
        for r in ep_rows
    ]

    return ApiKeyUsageDetailOut(
        key_id=key.id,
        key_name=key.name,
        key_prefix=key.key_prefix,
        total_requests=t.total or 0,
        total_errors=total_errors,
        total_credits_used=t.credits or 0,
        last_used_at=key.last_used_at,
        daily=daily,
        top_endpoints=top_endpoints,
    )


# ─── Internal helper ───────────────────────────────────────────────────────

async def log_api_key_usage(
    db: AsyncSession,
    api_key_id: UUID,
    user_id: UUID,
    endpoint: str,
    method: str,
    status_code: int,
    response_time_ms: int,
    credits_used: int = 0,
) -> None:
    """Log a single API key request. Called from middleware."""
    try:
        log = ApiKeyUsageLogDB(
            api_key_id=api_key_id,
            user_id=user_id,
            endpoint=endpoint,
            method=method,
            status_code=status_code,
            response_time_ms=response_time_ms,
            credits_used=credits_used,
        )
        db.add(log)

        # Update cached counters on the key
        key_res = await db.execute(select(ApiKeyDB).where(ApiKeyDB.id == api_key_id))
        key = key_res.scalar_one_or_none()
        if key:
            key.request_count = (key.request_count or 0) + 1
            key.total_credits_used = (key.total_credits_used or 0) + credits_used
            from datetime import datetime, timezone
            key.last_used_at = datetime.now(timezone.utc)

        await db.commit()
    except Exception as exc:
        logger.warning("api_key_usage_log_failed", error=str(exc))
        await db.rollback()
