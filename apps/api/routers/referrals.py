"""Referral / invite system endpoints."""
from __future__ import annotations

import secrets
import string
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from core.notify import create_notification, NotifType
from models.db import UserDB, ReferralDB, CreditTransactionDB
from models.schemas import ReferralStatsOut, ReferralOut

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/referrals", tags=["referrals"])

# Credits given to referrer when the referred user completes their first task
REFERRER_BONUS = 50
# Extra credits given to new user on signup (on top of base 100)
REFERRED_BONUS = 50

_ALPHABET = string.ascii_letters + string.digits


def _gen_referral_code(length: int = 8) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


async def _ensure_referral_code(user: UserDB, db: AsyncSession) -> str:
    """Generate and persist a referral code if the user doesn't have one yet."""
    if user.referral_code:
        return user.referral_code

    # Generate a unique code
    for _ in range(10):
        code = _gen_referral_code()
        existing = await db.execute(
            select(UserDB).where(UserDB.referral_code == code)
        )
        if not existing.scalar_one_or_none():
            user.referral_code = code
            await db.commit()
            return code

    raise HTTPException(500, "Failed to generate a unique referral code — try again")


@router.get("/stats", response_model=ReferralStatsOut)
async def get_referral_stats(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ReferralStatsOut:
    """Get the current user's referral code and stats."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    code = await _ensure_referral_code(user, db)

    # Count referrals
    refs_res = await db.execute(
        select(func.count()).where(ReferralDB.referrer_id == user_id)
    )
    total_referrals = refs_res.scalar_one()

    # Sum paid referral bonuses
    paid_res = await db.execute(
        select(func.coalesce(func.sum(ReferralDB.referrer_bonus_credits), 0)).where(
            ReferralDB.referrer_id == user_id,
            ReferralDB.bonus_paid == True,  # noqa: E712
        )
    )
    paid_bonus = paid_res.scalar_one()

    # Pending = user's credits_pending field
    pending_bonus = user.credits_pending

    # Use the app's public URL (from env or a sensible default)
    import os
    base_url = os.getenv("PUBLIC_URL", "https://crowdsourcerer.rebaselabs.online")
    referral_url = f"{base_url}/register?ref={code}"

    return ReferralStatsOut(
        referral_code=code,
        referral_url=referral_url,
        total_referrals=total_referrals,
        pending_bonus_credits=pending_bonus,
        paid_bonus_credits=paid_bonus,
    )


@router.get("", response_model=list[ReferralOut])
async def list_referrals(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[ReferralOut]:
    """List users I referred."""
    offset = (page - 1) * page_size
    res = await db.execute(
        select(ReferralDB, UserDB)
        .join(UserDB, UserDB.id == ReferralDB.referred_id)
        .where(ReferralDB.referrer_id == user_id)
        .order_by(ReferralDB.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = res.all()
    out = []
    for ref, referred_user in rows:
        # Mask email for privacy
        email = referred_user.email if referred_user else None
        if email:
            parts = email.split("@")
            masked = parts[0][:2] + "***@" + parts[1] if len(parts) == 2 else "***"
        else:
            masked = None
        out.append(
            ReferralOut(
                id=ref.id,
                referred_email=masked,
                bonus_paid=ref.bonus_paid,
                referrer_bonus_credits=ref.referrer_bonus_credits,
                created_at=ref.created_at,
            )
        )
    return out


# ─── Internal helper — called from auth.py on registration ────────────────

async def apply_referral_on_signup(
    referred_user_id: UUID | str,
    referral_code: str,
    db: AsyncSession,
) -> None:
    """
    Link a new user to their referrer.
    Called during registration when a valid referral code is provided.
    Gives the new user REFERRED_BONUS extra credits (beyond the base 100).
    Referrer bonus is paid after the referred user completes their first task.
    """
    # Find referrer — lock row to prevent concurrent credits_pending lost-update
    # when multiple referred users sign up simultaneously using the same code.
    res = await db.execute(
        select(UserDB).where(UserDB.referral_code == referral_code).with_for_update()
    )
    referrer = res.scalar_one_or_none()
    if not referrer:
        return  # silently ignore invalid codes

    # Don't allow self-referral
    if str(referrer.id) == str(referred_user_id):
        return

    # Create referral record
    referral = ReferralDB(
        referrer_id=referrer.id,
        referred_id=referred_user_id,
        referrer_bonus_credits=REFERRER_BONUS,
        referred_bonus_credits=REFERRED_BONUS,
        bonus_paid=False,
    )
    db.add(referral)

    # Give referred user their signup bonus — lock row before incrementing credits
    res2 = await db.execute(
        select(UserDB).where(UserDB.id == referred_user_id).with_for_update()
    )
    new_user = res2.scalar_one_or_none()
    if new_user:
        new_user.credits += REFERRED_BONUS
        txn = CreditTransactionDB(
            user_id=referred_user_id,
            amount=REFERRED_BONUS,
            type="credit",
            description=f"Referral signup bonus from {referrer.email}",
        )
        db.add(txn)

    # Put referrer's bonus in pending (credited after first task)
    referrer.credits_pending += REFERRER_BONUS

    logger.info("referral_applied", referrer_id=str(referrer.id),
                referred_id=str(referred_user_id))


async def pay_referral_bonus_on_first_task(
    worker_id: UUID | str,
    db: AsyncSession,
) -> None:
    """
    Called when a worker completes their first task.
    Pays the referrer's pending bonus and marks it as paid.
    """
    # Lock the referral row first — prevents double-payment if this function
    # is called concurrently (e.g. two fast task completions racing on first-task).
    res = await db.execute(
        select(ReferralDB).where(
            ReferralDB.referred_id == worker_id,
            ReferralDB.bonus_paid == False,  # noqa: E712
        ).with_for_update()
    )
    referral = res.scalar_one_or_none()
    if not referral:
        return

    # Lock referrer row before credit mutations to prevent lost-update race
    ref_res = await db.execute(
        select(UserDB).where(UserDB.id == referral.referrer_id).with_for_update()
    )
    referrer = ref_res.scalar_one_or_none()
    if referrer:
        referrer.credits += referral.referrer_bonus_credits
        referrer.credits_pending = max(0, referrer.credits_pending - referral.referrer_bonus_credits)
        txn = CreditTransactionDB(
            user_id=referral.referrer_id,
            amount=referral.referrer_bonus_credits,
            type="credit",
            description="Referral bonus — your invite completed their first task!",
        )
        db.add(txn)

    referral.bonus_paid = True

    # In-app notification to referrer
    if referrer:
        try:
            await create_notification(
                db, referral.referrer_id,
                NotifType.REFERRAL_BONUS,
                "Referral bonus earned! 🎁",
                f"Your referral completed their first task — you earned +{referral.referrer_bonus_credits} credits!",
                link="/dashboard/referrals",
            )
        except Exception:
            logger.warning(
                "referrals.bonus_notification_failed",
                referral_id=str(referral.id),
                referrer_id=str(referral.referrer_id),
                exc_info=True,
            )

    logger.info("referral_bonus_paid", referral_id=str(referral.id),
                referrer_id=str(referral.referrer_id))
