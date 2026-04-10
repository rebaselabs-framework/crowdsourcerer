"""Credits endpoints: balance, transactions, checkout."""
from typing import Optional

import structlog
import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from core.auth import get_current_user_id
from core.config import get_settings
from core.database import get_db
from models.db import UserDB, CreditTransactionDB, StripeEventLogDB
from models.schemas import (
    CheckoutRequest, CheckoutResponse,
    CreditBalanceOut, CreditTransactionOut, PaginatedTransactions
)

logger = structlog.get_logger()
limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/v1/credits", tags=["credits"])
settings = get_settings()

# Bonus credits awarded for quick-buy bundles (credits purchased → bonus credits)
# These must match the frontend EXACTLY so webhook credits total is correct.
_CREDIT_BONUSES: dict[int, int] = {
    2500:  50,
    5000:  150,
    10000: 500,
}


@router.get("", response_model=CreditBalanceOut)
async def get_credits(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Sum all charges for total_used
    total_charged = await db.execute(
        select(func.sum(CreditTransactionDB.amount))
        .where(CreditTransactionDB.user_id == user_id)
        .where(CreditTransactionDB.type == "charge")
    )
    total_used = abs(total_charged.scalar() or 0)

    return CreditBalanceOut(
        available=user.credits,
        reserved=0,
        total_used=total_used,
        plan=user.plan,
    )


@router.get("/transactions", response_model=PaginatedTransactions)
async def list_transactions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    q = select(CreditTransactionDB).where(CreditTransactionDB.user_id == user_id)
    total_result = await db.execute(
        select(func.count()).select_from(q.subquery())
    )
    total = total_result.scalar() or 0

    q = q.order_by(CreditTransactionDB.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    txns = result.scalars().all()

    return PaginatedTransactions(
        items=txns,
        total=total,
        page=page,
        page_size=page_size,
        has_next=(page * page_size) < total,
    )


@router.post("/checkout", response_model=CheckoutResponse)
@limiter.limit("10/minute")
async def create_checkout(
    request: Request,
    req: CheckoutRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Create a Stripe checkout session to buy credits."""
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Stripe not configured")

    stripe.api_key = settings.stripe_secret_key

    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Ensure stripe customer
    if not user.stripe_customer_id:
        customer = stripe.Customer.create(
            email=user.email,
            name=user.name,
            metadata={"user_id": user_id},
        )
        user.stripe_customer_id = customer.id
        await db.commit()
        logger.info("stripe.customer_created", user_id=user_id, customer_id=customer.id)

    usd_amount = req.credits // settings.credits_per_usd  # in dollars
    if usd_amount < 1:
        raise HTTPException(status_code=400, detail="Minimum purchase: 100 credits ($1)")

    # Bonus credits for qualifying bundle sizes
    bonus = _CREDIT_BONUSES.get(req.credits, 0)
    total_credits = req.credits + bonus

    product_name = f"CrowdSorcerer Credits ({req.credits:,})"
    product_desc = f"{req.credits:,} credits"
    if bonus:
        product_name += f" + {bonus:,} bonus"
        product_desc += f" + {bonus:,} bonus credits = {total_credits:,} total"
    else:
        product_desc += f" at $0.01/credit"

    session = stripe.checkout.Session.create(
        customer=user.stripe_customer_id,
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": product_name,
                    "description": product_desc,
                },
                "unit_amount": usd_amount * 100,  # cents — charged on purchase amount only
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=str(req.success_url),
        cancel_url=str(req.cancel_url),
        metadata={
            "user_id": user_id,
            "credits": str(total_credits),  # includes any bonus
        },
    )

    return CheckoutResponse(checkout_url=session.url, session_id=session.id)


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """DEPRECATED: Use POST /v1/webhooks/stripe instead."""
    raise HTTPException(
        status_code=410,
        detail="This endpoint is deprecated. Use POST /v1/webhooks/stripe instead.",
    )
