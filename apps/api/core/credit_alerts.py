"""Credit burn-rate alert helper.

Call `maybe_fire_credit_alert(db, user)` after any credit deduction.
It fires a notification the first time the user's balance drops below
their configured threshold, then sets `credit_alert_fired = True` so
the notification is not repeated until they top up again.

When credits are topped up (credits router), call `reset_credit_alert(db, user)`
to clear the fired flag so the alert can fire again next time.
"""

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.background import safe_create_task

logger = structlog.get_logger()


async def maybe_fire_credit_alert(db: AsyncSession, user) -> None:
    """Fire a low-credit notification if the user's balance just dropped below threshold.

    Args:
        db: Active async database session.
        user: The UserDB instance (must be already loaded/refreshed).
    """
    threshold = getattr(user, "credit_alert_threshold", None)
    if threshold is None:
        return  # Alert not configured

    if getattr(user, "credit_alert_fired", False):
        return  # Already fired; wait for reset

    if user.credits < threshold:
        # Fire the alert
        from core.notify import create_notification, NotifType
        await create_notification(
            db=db,
            user_id=str(user.id),
            type=NotifType.SYSTEM,
            title="⚠️ Low Credit Balance",
            body=(
                f"Your credit balance ({user.credits} credits) has dropped below your "
                f"alert threshold of {threshold} credits. "
                "Top up now to keep your tasks running."
            ),
            link="/dashboard/billing",
        )
        user.credit_alert_fired = True
        logger.info(
            "credit_alert_fired",
            user_id=str(user.id),
            credits=user.credits,
            threshold=threshold,
        )

        # Also send email if we have the user's email address
        from core.email import notify_low_credits
        from sqlalchemy import select
        from models.db import UserDB
        result = await db.execute(select(UserDB.email, UserDB.name).where(UserDB.id == user.id))
        row = result.first()
        if row and row.email:
            safe_create_task(
                notify_low_credits(
                    to_email=row.email,
                    balance=user.credits,
                    threshold=threshold,
                    name=row.name,
                ),
                name="email.low_credits",
            )


async def reset_credit_alert_if_recovered(db: AsyncSession, user) -> None:
    """Reset the fired flag when the user tops up above their threshold.

    Call this after adding credits to the user's balance.
    """
    threshold = getattr(user, "credit_alert_threshold", None)
    if threshold is None:
        return

    if not getattr(user, "credit_alert_fired", False):
        return  # Nothing to reset

    if user.credits >= threshold:
        user.credit_alert_fired = False
        logger.info(
            "credit_alert_reset",
            user_id=str(user.id),
            credits=user.credits,
            threshold=threshold,
        )
