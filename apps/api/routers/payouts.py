"""Payout / withdrawal request endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from core.notify import create_notification, NotifType
from models.db import UserDB, PayoutRequestDB, CreditTransactionDB
from models.schemas import (
    PayoutRequestCreate,
    PayoutRequestOut,
    PayoutReviewRequest,
    PayoutListOut,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/payouts", tags=["payouts"])

# Minimum credits to request a payout (= $10)
MIN_PAYOUT_CREDITS = 1000

ALLOWED_METHODS = {"paypal", "bank_transfer", "crypto"}


async def require_admin(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
) -> str:
    """Dependency: verify the caller is an admin."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_admin:
        raise HTTPException(403, "Admin access required")
    return user_id


# ─── Worker endpoints ──────────────────────────────────────────────────────

@router.post("", response_model=PayoutRequestOut)
async def create_payout_request(
    body: PayoutRequestCreate,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> PayoutRequestOut:
    """Submit a withdrawal request for earned credits."""
    if body.payout_method not in ALLOWED_METHODS:
        raise HTTPException(400, f"payout_method must be one of: {', '.join(ALLOWED_METHODS)}")

    if body.credits_requested < MIN_PAYOUT_CREDITS:
        raise HTTPException(
            400,
            f"Minimum payout is {MIN_PAYOUT_CREDITS} credits (${MIN_PAYOUT_CREDITS / 100:.2f}). "
            f"You requested {body.credits_requested}.",
        )

    # Validate payout_details
    method = body.payout_method
    details = body.payout_details
    if method == "paypal" and "email" not in details:
        raise HTTPException(400, "payout_details must include 'email' for PayPal")
    if method == "bank_transfer" and not all(k in details for k in ("account_name", "iban")):
        raise HTTPException(400, "payout_details must include 'account_name' and 'iban' for bank transfer")
    if method == "crypto" and not all(k in details for k in ("network", "address")):
        raise HTTPException(400, "payout_details must include 'network' and 'address' for crypto")

    # Load user
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    if user.role not in ("worker", "both"):
        raise HTTPException(403, "Only workers can request payouts")

    # Check they have enough credits
    if user.credits < body.credits_requested:
        raise HTTPException(
            400,
            f"Insufficient credits. You have {user.credits} but requested {body.credits_requested}.",
        )

    # Check for pending payout (only one at a time)
    existing = await db.execute(
        select(PayoutRequestDB).where(
            and_(
                PayoutRequestDB.worker_id == user_id,
                PayoutRequestDB.status.in_(["pending", "processing"]),
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "You already have a pending payout request. Wait for it to be processed.")

    # Deduct credits immediately (hold them during processing)
    user.credits -= body.credits_requested
    usd = round(body.credits_requested / 100.0, 2)

    payout = PayoutRequestDB(
        worker_id=user_id,
        credits_requested=body.credits_requested,
        usd_amount=usd,
        status="pending",
        payout_method=method,
        payout_details=details,
    )
    db.add(payout)

    # Record transaction
    txn = CreditTransactionDB(
        user_id=user_id,
        amount=-body.credits_requested,
        type="charge",
        description=f"Payout request — ${usd:.2f} via {method}",
    )
    db.add(txn)
    await db.commit()
    await db.refresh(payout)

    logger.info("payout_requested", worker_id=str(user_id),
                credits=body.credits_requested, usd=usd, method=method)
    return PayoutRequestOut.model_validate(payout)


@router.get("", response_model=PayoutListOut)
async def list_my_payouts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> PayoutListOut:
    """List the current worker's payout requests."""
    offset = (page - 1) * page_size
    q = select(PayoutRequestDB).where(PayoutRequestDB.worker_id == user_id)
    total_res = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_res.scalar_one()

    res = await db.execute(
        q.order_by(PayoutRequestDB.created_at.desc()).offset(offset).limit(page_size)
    )
    items = res.scalars().all()
    return PayoutListOut(
        items=[PayoutRequestOut.model_validate(p) for p in items],
        total=total,
    )


@router.delete("/{payout_id}", status_code=204)
async def cancel_payout_request(
    payout_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Cancel a pending payout and refund the credits."""
    result = await db.execute(
        select(PayoutRequestDB).where(
            and_(PayoutRequestDB.id == payout_id, PayoutRequestDB.worker_id == user_id)
        )
    )
    payout = result.scalar_one_or_none()
    if not payout:
        raise HTTPException(404, "Payout request not found")
    if payout.status not in ("pending",):
        raise HTTPException(409, f"Cannot cancel a payout with status '{payout.status}'")

    # Refund credits
    user_res = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_res.scalar_one()
    user.credits += payout.credits_requested
    payout.status = "rejected"
    payout.admin_note = "Cancelled by worker"
    payout.processed_at = datetime.now(timezone.utc)

    txn = CreditTransactionDB(
        user_id=user_id,
        amount=payout.credits_requested,
        type="refund",
        description=f"Payout cancellation refund",
    )
    db.add(txn)
    await db.commit()


# ─── Admin endpoints ───────────────────────────────────────────────────────

@router.get("/admin/all", response_model=PayoutListOut)
async def admin_list_payouts(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    admin_id: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> PayoutListOut:
    """Admin: list all payout requests."""
    offset = (page - 1) * page_size
    q = select(PayoutRequestDB)
    if status:
        q = q.where(PayoutRequestDB.status == status)
    total_res = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_res.scalar_one()

    res = await db.execute(
        q.order_by(PayoutRequestDB.created_at.desc()).offset(offset).limit(page_size)
    )
    items = res.scalars().all()
    return PayoutListOut(
        items=[PayoutRequestOut.model_validate(p) for p in items],
        total=total,
    )


@router.post("/{payout_id}/review", response_model=PayoutRequestOut)
async def admin_review_payout(
    payout_id: UUID,
    body: PayoutReviewRequest,
    admin_id: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> PayoutRequestOut:
    """Admin: approve (mark as processing/paid) or reject a payout."""
    allowed_transitions = {"processing", "paid", "rejected"}
    if body.status not in allowed_transitions:
        raise HTTPException(400, f"status must be one of: {', '.join(allowed_transitions)}")

    result = await db.execute(
        select(PayoutRequestDB).where(PayoutRequestDB.id == payout_id)
    )
    payout = result.scalar_one_or_none()
    if not payout:
        raise HTTPException(404, "Payout request not found")

    if payout.status == "paid":
        raise HTTPException(409, "Payout already marked as paid")

    # If rejecting — refund the credits
    if body.status == "rejected" and payout.status in ("pending", "processing"):
        user_res = await db.execute(select(UserDB).where(UserDB.id == payout.worker_id))
        user = user_res.scalar_one()
        user.credits += payout.credits_requested
        txn = CreditTransactionDB(
            user_id=payout.worker_id,
            amount=payout.credits_requested,
            type="refund",
            description="Payout request rejected — credits refunded",
        )
        db.add(txn)

    payout.status = body.status
    payout.admin_note = body.admin_note
    payout.processed_at = datetime.now(timezone.utc)

    # ── Notify worker of status change ────────────────────────────────────
    usd_str = f"${payout.usd_amount:.2f}"
    if body.status == "processing":
        await create_notification(
            db, payout.worker_id,
            NotifType.PAYOUT_PROCESSING,
            "Payout in progress",
            f"Your payout request of {usd_str} is now being processed. "
            "Funds will arrive within 1–3 business days.",
            link="/worker/earnings",
        )
    elif body.status == "paid":
        await create_notification(
            db, payout.worker_id,
            NotifType.PAYOUT_PAID,
            "Payout sent! 💸",
            f"Your payout of {usd_str} has been sent. Check your {payout.payout_method.replace('_', ' ')} account.",
            link="/worker/earnings",
        )
    elif body.status == "rejected":
        note_suffix = f" Reason: {body.admin_note}" if body.admin_note else ""
        await create_notification(
            db, payout.worker_id,
            NotifType.PAYOUT_REJECTED,
            "Payout rejected",
            f"Your payout request of {usd_str} was rejected and credits have been refunded to your account.{note_suffix}",
            link="/worker/earnings",
        )

    await db.commit()
    await db.refresh(payout)

    logger.info("payout_reviewed", payout_id=str(payout_id),
                status=body.status, admin_id=str(admin_id))
    return PayoutRequestOut.model_validate(payout)
