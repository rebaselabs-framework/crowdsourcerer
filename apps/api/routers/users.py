"""User profile and API key endpoints."""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.auth import get_current_user_id, generate_api_key
from core.database import get_db
from core.scopes import ALL_SCOPES, SCOPE_DESCRIPTIONS
from models.db import UserDB, ApiKeyDB
from models.schemas import (
    ApiKeyCreateRequest, ApiKeyCreateResponse, ApiKeyOut, UserOut,
    ApiKeyRateLimitUpdate, ApiKeyRateStatusOut,
    CreditAlertOut, CreditAlertUpdate,
)

router = APIRouter(prefix="/v1", tags=["users"])


@router.get("/users/me", response_model=UserOut)
async def get_me(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(
        select(ApiKeyDB).where(ApiKeyDB.user_id == user_id)
    )
    return result.scalars().all()


@router.get("/scopes", tags=["api-keys"])
async def list_scopes():
    """Return the catalogue of available API key scopes.

    Pass any subset of these scope strings when creating an API key.
    An empty ``scopes`` array on a key means **full access** (all scopes granted).
    """
    return [
        {"scope": s, "description": SCOPE_DESCRIPTIONS.get(s, "")}
        for s in ALL_SCOPES
    ]


@router.post("/api-keys", response_model=ApiKeyCreateResponse, status_code=201)
async def create_api_key(
    req: ApiKeyCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    # Validate scopes — reject unknown scope strings
    unknown = [s for s in req.scopes if s not in ALL_SCOPES]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scope(s): {', '.join(unknown)}. "
                   f"Valid scopes: {', '.join(ALL_SCOPES)}",
        )

    plaintext, hashed = generate_api_key()
    prefix = plaintext[:12]  # "csk_" + 8 chars

    key = ApiKeyDB(
        user_id=user_id,
        name=req.name,
        key_hash=hashed,
        key_prefix=prefix,
        scopes=req.scopes,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)

    return ApiKeyCreateResponse(
        id=key.id,
        key=plaintext,
        name=key.name,
        scopes=key.scopes or [],
        created_at=key.created_at,
    )


@router.delete("/api-keys/{key_id}", status_code=204)
async def delete_api_key(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(
        select(ApiKeyDB).where(ApiKeyDB.id == key_id, ApiKeyDB.user_id == user_id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.delete(key)
    await db.commit()


@router.get("/quota")
async def get_my_quota(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return the current user's plan quota usage and limits."""
    from sqlalchemy import select
    from models.db import UserDB
    user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    from core.quotas import get_quota_status
    return await get_quota_status(db, user_id, user.plan)


# ─── Per-API-key rate limit configuration ──────────────────────────────────

@router.patch("/api-keys/{key_id}/rate-limits", response_model=ApiKeyOut)
async def update_api_key_rate_limits(
    key_id: UUID,
    body: ApiKeyRateLimitUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Configure per-key rate limits (requests/minute and requests/day).

    Pass `null` for either field to revert to the plan-level default.
    Pro/Enterprise users can raise limits; Free/Starter are capped at plan maximums.
    """
    result = await db.execute(
        select(ApiKeyDB).where(ApiKeyDB.id == key_id, ApiKeyDB.user_id == user_id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")

    # Fetch user plan to enforce caps
    user_res = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_res.scalar_one_or_none()
    plan = user.plan if user else "free"

    # Plan-level RPM caps (can't raise above this)
    _MAX_RPM = {"free": 30, "starter": 120, "pro": 600, "enterprise": 10_000}
    _MAX_DAILY = {"free": 1_000, "starter": 5_000, "pro": 50_000, "enterprise": 1_000_000}

    if body.rate_limit_rpm is not None:
        cap = _MAX_RPM.get(plan, 60)
        if body.rate_limit_rpm > cap:
            raise HTTPException(
                status_code=400,
                detail=f"RPM limit {body.rate_limit_rpm} exceeds plan maximum of {cap}/min for {plan} plan. "
                       "Upgrade your plan or set a lower value.",
            )
    if body.rate_limit_daily is not None:
        cap = _MAX_DAILY.get(plan, 2000)
        if body.rate_limit_daily > cap:
            raise HTTPException(
                status_code=400,
                detail=f"Daily limit {body.rate_limit_daily} exceeds plan maximum of {cap}/day for {plan} plan.",
            )

    key.rate_limit_rpm = body.rate_limit_rpm
    key.rate_limit_daily = body.rate_limit_daily
    await db.commit()
    await db.refresh(key)

    return ApiKeyOut.model_validate(key)


@router.get("/api-keys/{key_id}/rate-status", response_model=ApiKeyRateStatusOut)
async def get_api_key_rate_status(
    key_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Return current rate-limit counters for an API key (non-mutating)."""
    result = await db.execute(
        select(ApiKeyDB).where(ApiKeyDB.id == key_id, ApiKeyDB.user_id == user_id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")

    user_res = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_res.scalar_one_or_none()
    plan = user.plan if user else "free"

    from core.api_key_rate_limit import get_api_key_rate_status
    status = await get_api_key_rate_status(db, key, plan)
    return ApiKeyRateStatusOut(
        key_id=key.id,
        key_prefix=key.key_prefix,
        **status,
    )


# ─── Credit burn-rate alerts ────────────────────────────────────────────────

@router.get("/users/credit-alert", response_model=CreditAlertOut)
async def get_credit_alert(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get the user's credit burn-rate alert threshold."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return CreditAlertOut(
        threshold=user.credit_alert_threshold,
        alert_fired=user.credit_alert_fired or False,
    )


@router.patch("/users/credit-alert", response_model=CreditAlertOut)
async def update_credit_alert(
    body: CreditAlertUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Set or clear the credit burn-rate alert threshold.

    When set, a notification is fired the first time the user's credit
    balance drops below the threshold. The alert resets when credits are
    topped up above the threshold again.
    """
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.credit_alert_threshold = body.threshold
    # Reset the "fired" flag whenever threshold changes
    user.credit_alert_fired = False
    await db.commit()

    return CreditAlertOut(
        threshold=user.credit_alert_threshold,
        alert_fired=False,
    )
