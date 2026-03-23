"""Credits endpoints: balance, transactions, checkout."""
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.config import get_settings
from core.database import get_db
from models.db import UserDB, CreditTransactionDB
from models.schemas import (
    CheckoutRequest, CheckoutResponse,
    CreditBalanceOut, CreditTransactionOut, PaginatedTransactions
)

router = APIRouter(prefix="/v1/credits", tags=["credits"])
settings = get_settings()


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
async def create_checkout(
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

    usd_amount = req.credits // settings.credits_per_usd  # in dollars
    if usd_amount < 1:
        raise HTTPException(status_code=400, detail="Minimum purchase: 100 credits ($1)")

    session = stripe.checkout.Session.create(
        customer=user.stripe_customer_id,
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": f"CrowdSorcerer Credits ({req.credits:,})",
                    "description": f"{req.credits} credits at $0.01/credit",
                },
                "unit_amount": usd_amount * 100,  # cents
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=req.success_url,
        cancel_url=req.cancel_url,
        metadata={
            "user_id": user_id,
            "credits": str(req.credits),
        },
    )

    return CheckoutResponse(checkout_url=session.url, session_id=session.id)


@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Stripe webhook — credit user after successful payment."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, settings.stripe_webhook_secret
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("metadata", {}).get("user_id")
        credits = int(session.get("metadata", {}).get("credits", 0))

        if user_id and credits > 0:
            result = await db.execute(select(UserDB).where(UserDB.id == user_id))
            user = result.scalar_one_or_none()
            if user:
                user.credits += credits
                txn = CreditTransactionDB(
                    user_id=user_id,
                    amount=credits,
                    type="credit",
                    description=f"Purchased {credits} credits",
                    stripe_payment_intent=session.get("payment_intent"),
                )
                db.add(txn)
                await db.commit()

    return {"status": "ok"}
