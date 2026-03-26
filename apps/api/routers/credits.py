"""Credits endpoints: balance, transactions, checkout."""
from typing import Optional

import structlog
import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from core.auth import get_current_user_id
from core.config import get_settings
from core.database import get_db
from models.db import UserDB, CreditTransactionDB, StripeEventLogDB
from models.schemas import (
    CheckoutRequest, CheckoutResponse,
    CreditBalanceOut, CreditTransactionOut, PaginatedTransactions
)

logger = structlog.get_logger()
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
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Stripe webhook — credit user after successful payment.

    Note: the canonical webhook handler is at POST /v1/webhooks/stripe which
    handles the full event lifecycle with idempotency. This endpoint handles
    checkout.session.completed only, with its own idempotency guard.
    """
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, settings.stripe_webhook_secret
        )
    except Exception as exc:
        logger.warning("stripe_webhook.invalid_signature", error=str(exc))
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_id = event.get("id", "")
    event_type = event.get("type", "")

    if event_type == "checkout.session.completed":
        # Idempotency guard — reject duplicate deliveries
        if event_id:
            existing = await db.scalar(
                select(func.count()).where(
                    StripeEventLogDB.stripe_event_id == event_id,
                    StripeEventLogDB.processed == True,  # noqa: E712
                )
            )
            if existing:
                logger.info("stripe_webhook.duplicate_event", event_id=event_id)
                return {"status": "ok", "duplicate": True}

        session = event["data"]["object"]
        user_id = session.get("metadata", {}).get("user_id")
        try:
            credits = int(session.get("metadata", {}).get("credits", 0))
        except (ValueError, TypeError):
            logger.error(
                "stripe_webhook.invalid_credits_metadata",
                event_id=event_id,
                raw=session.get("metadata", {}).get("credits"),
            )
            return {"status": "ok"}

        if not user_id or credits <= 0:
            logger.warning(
                "stripe_webhook.skipped",
                event_id=event_id,
                user_id=user_id,
                credits=credits,
            )
            return {"status": "ok"}

        result = await db.execute(select(UserDB).where(UserDB.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            logger.error("stripe_webhook.user_not_found", event_id=event_id, user_id=user_id)
            return {"status": "ok"}

        user.credits += credits
        txn = CreditTransactionDB(
            user_id=user_id,
            amount=credits,
            type="credit",
            description=f"Purchased {credits:,} credits",
            stripe_payment_intent=session.get("payment_intent"),
        )
        db.add(txn)
        # Mark event as processed to prevent double-crediting on replay
        if event_id:
            db.add(StripeEventLogDB(
                stripe_event_id=event_id,
                event_type=event_type,
                processed=True,
            ))
        # Reset low-credit alert if balance recovered
        from core.credit_alerts import reset_credit_alert_if_recovered
        await reset_credit_alert_if_recovered(db, user)

        # In-app payment notification
        from core.notify import create_notification, NotifType
        new_balance = user.credits
        await create_notification(
            db, user_id,
            NotifType.PAYMENT_RECEIVED,
            "Credits added 💳",
            f"{credits:,} credits added. New balance: {new_balance:,} credits.",
            link="/dashboard/billing",
        )
        await db.commit()
        logger.info(
            "stripe_webhook.credits_added",
            event_id=event_id,
            user_id=user_id,
            credits=credits,
            new_balance=new_balance,
        )

    return {"status": "ok"}
