"""Stripe webhook receiver for CrowdSorcerer.

Handles incoming Stripe events to:
  - Credit user accounts after successful checkout
  - Update subscription plans
  - Downgrade users when subscriptions are cancelled
  - Notify users on payment failures

Signature verification uses HMAC-SHA256 (no stripe Python SDK needed).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import get_db
from core.notify import create_notification
from models.db import CreditTransactionDB, StripeEventLogDB, UserDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


# ── Signature verification ────────────────────────────────────────────────────

def _verify_stripe_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    """Verify Stripe-Signature header using HMAC-SHA256."""
    try:
        parts = dict(item.split("=", 1) for item in sig_header.split(","))
        timestamp = int(parts["t"])
        sig = parts.get("v1", "")
        # Reject stale webhooks (5-minute tolerance)
        if abs(time.time() - timestamp) > 300:
            return False
        signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
        expected = hmac.new(
            secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _price_to_plan(price_id: str) -> Optional[str]:
    """Map Stripe price ID to platform plan name."""
    settings = get_settings()
    if price_id == settings.stripe_price_id_pro:
        return "pro"
    if price_id == settings.stripe_price_id_starter:
        return "starter"
    return None


async def _get_user_by_customer(db: AsyncSession, customer_id: str) -> Optional[UserDB]:
    result = await db.execute(
        select(UserDB).where(UserDB.stripe_customer_id == customer_id)
    )
    return result.scalar_one_or_none()


async def _get_user_by_email(db: AsyncSession, email: str) -> Optional[UserDB]:
    result = await db.execute(
        select(UserDB).where(UserDB.email == email)
    )
    return result.scalar_one_or_none()


async def _add_credits(db: AsyncSession, user: UserDB, amount: int, description: str) -> None:
    """Add credits to a user and log a CreditTransactionDB record."""
    user.credits += amount
    txn = CreditTransactionDB(
        user_id=user.id,
        amount=amount,
        type="credit",
        description=description,
    )
    db.add(txn)


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@router.post("/stripe", status_code=200)
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    stripe_signature: str = Header(None, alias="stripe-signature"),
):
    """Receive and process Stripe webhook events."""
    settings = get_settings()
    raw_body = await request.body()

    # Signature verification
    if settings.stripe_webhook_secret:
        if not stripe_signature:
            raise HTTPException(400, "Missing Stripe-Signature header")
        if not _verify_stripe_signature(raw_body, stripe_signature, settings.stripe_webhook_secret):
            raise HTTPException(400, "Invalid Stripe signature")
    else:
        logger.warning("stripe.webhook.no_secret_configured")

    # Parse event
    try:
        event = json.loads(raw_body)
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    event_id = event.get("id", "")
    event_type = event.get("type", "")
    event_data = event.get("data", {}).get("object", {})

    logger.info("stripe.webhook.received", event_id=event_id, event_type=event_type)

    # Idempotency check — skip if already successfully processed
    existing = await db.scalar(
        select(func.count()).where(
            StripeEventLogDB.stripe_event_id == event_id,
            StripeEventLogDB.processed == True,  # noqa: E712
        )
    )
    if existing:
        logger.info("stripe.webhook.duplicate_ignored", event_id=event_id)
        return {"status": "already_processed"}

    # Log the event (mark unprocessed until we finish)
    log_entry = StripeEventLogDB(
        stripe_event_id=event_id,
        event_type=event_type,
        payload=event_data,
        processed=False,
    )
    db.add(log_entry)
    await db.flush()

    error_msg: Optional[str] = None

    try:
        # ── checkout.session.completed ────────────────────────────────────────
        if event_type == "checkout.session.completed":
            mode = event_data.get("mode", "")
            customer_id = event_data.get("customer")
            customer_email = (
                event_data.get("customer_email")
                or event_data.get("customer_details", {}).get("email")
            )
            amount_total = event_data.get("amount_total", 0)  # cents

            user: Optional[UserDB] = None
            if customer_id:
                user = await _get_user_by_customer(db, customer_id)
            if not user and customer_email:
                user = await _get_user_by_email(db, customer_email)

            if user:
                # Store stripe_customer_id if not already set
                if customer_id and not user.stripe_customer_id:
                    user.stripe_customer_id = customer_id

                if mode == "payment":
                    # One-time credit purchase
                    credits_to_add = int((amount_total / 100) * settings.credits_per_usd)
                    await _add_credits(
                        db, user, credits_to_add,
                        f"Stripe checkout: ${amount_total / 100:.2f}",
                    )
                    log_entry.user_id = user.id
                    await create_notification(
                        db,
                        user_id=user.id,
                        type="system",
                        title="Credits added!",
                        body=(
                            f"+{credits_to_add} credits added to your account"
                            f" (${amount_total / 100:.2f} payment)"
                        ),
                        link="/dashboard/credits",
                    )
                    logger.info(
                        "stripe.checkout.credits_added",
                        user_id=str(user.id),
                        credits=credits_to_add,
                    )
                elif mode == "subscription":
                    # Subscription checkout — plan updated via subscription.created event
                    log_entry.user_id = user.id
                    logger.info("stripe.checkout.subscription_started", user_id=str(user.id))
            else:
                logger.warning(
                    "stripe.checkout.user_not_found",
                    customer=customer_id,
                    email=customer_email,
                )

        # ── customer.subscription.created / updated ───────────────────────────
        elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
            customer_id = event_data.get("customer")
            status = event_data.get("status", "")
            items = event_data.get("items", {}).get("data", [])
            price_id = items[0]["price"]["id"] if items else None

            user = await _get_user_by_customer(db, customer_id) if customer_id else None
            if user and price_id:
                new_plan = _price_to_plan(price_id)
                if new_plan and status in ("active", "trialing"):
                    old_plan = user.plan
                    user.plan = new_plan
                    log_entry.user_id = user.id
                    await create_notification(
                        db,
                        user_id=user.id,
                        type="system",
                        title=f"Plan upgraded to {new_plan.capitalize()}!",
                        body=(
                            f"Your CrowdSorcerer plan is now {new_plan.capitalize()}."
                            " Enjoy expanded quotas!"
                        ),
                        link="/dashboard/quota",
                    )
                    logger.info(
                        "stripe.subscription.plan_updated",
                        user_id=str(user.id),
                        old=old_plan,
                        new=new_plan,
                    )

        # ── customer.subscription.deleted ─────────────────────────────────────
        elif event_type == "customer.subscription.deleted":
            customer_id = event_data.get("customer")
            user = await _get_user_by_customer(db, customer_id) if customer_id else None
            if user:
                user.plan = "free"
                log_entry.user_id = user.id
                await create_notification(
                    db,
                    user_id=user.id,
                    type="system",
                    title="Subscription cancelled",
                    body="Your subscription has ended. You've been moved to the Free plan.",
                    link="/pricing",
                )
                logger.info("stripe.subscription.cancelled", user_id=str(user.id))

        # ── invoice.payment_succeeded ─────────────────────────────────────────
        elif event_type == "invoice.payment_succeeded":
            billing_reason = event_data.get("billing_reason", "")
            customer_id = event_data.get("customer")
            amount_paid = event_data.get("amount_paid", 0)  # cents

            # Only add credits for subscription renewals, skip initial setup invoice
            if billing_reason == "subscription_cycle" and customer_id:
                user = await _get_user_by_customer(db, customer_id)
                if user:
                    # Small bonus credit renewal gift (5% of monthly value, capped at 50)
                    bonus = min(50, int((amount_paid / 100) * settings.credits_per_usd * 0.05))
                    if bonus > 0:
                        await _add_credits(
                            db, user, bonus,
                            f"Monthly renewal bonus: ${amount_paid / 100:.2f}",
                        )
                    log_entry.user_id = user.id
                    logger.info(
                        "stripe.invoice.renewal",
                        user_id=str(user.id),
                        bonus_credits=bonus,
                    )

        # ── invoice.payment_failed ────────────────────────────────────────────
        elif event_type == "invoice.payment_failed":
            customer_id = event_data.get("customer")
            amount_due = event_data.get("amount_due", 0)

            user = await _get_user_by_customer(db, customer_id) if customer_id else None
            if user:
                log_entry.user_id = user.id
                await create_notification(
                    db,
                    user_id=user.id,
                    type="system",
                    title="Payment failed",
                    body=(
                        f"We couldn't process your payment of ${amount_due / 100:.2f}."
                        " Please update your payment method."
                    ),
                    link="https://billing.stripe.com",
                )
                logger.warning(
                    "stripe.invoice.payment_failed",
                    user_id=str(user.id),
                    amount=amount_due,
                )

        else:
            logger.debug("stripe.webhook.unhandled_event", event_type=event_type)

    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)
        logger.exception(
            "stripe.webhook.processing_error",
            event_id=event_id,
            event_type=event_type,
        )

    # Finalize log entry
    log_entry.processed = error_msg is None
    log_entry.error = error_msg

    await db.commit()
    return {"status": "processed" if not error_msg else "error", "event_type": event_type}
