"""In-app notification helper.

Call `create_notification(db, user_id, ...)` from any router/service to
persist a notification for a user. The notification router exposes read/mark
endpoints so the frontend can poll or SSE for updates.
"""
from __future__ import annotations

import uuid as _uuid
from typing import Optional
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import NotificationDB

logger = structlog.get_logger()


# ─── Notification type constants ─────────────────────────────────────────────

class NotifType:
    TASK_COMPLETED       = "task_completed"
    TASK_FAILED          = "task_failed"
    SUBMISSION_RECEIVED  = "submission_received"
    SUBMISSION_APPROVED  = "submission_approved"
    SUBMISSION_REJECTED  = "submission_rejected"
    REFERRAL_BONUS       = "referral_bonus"
    PAYOUT_PROCESSING    = "payout_processing"
    PAYOUT_PAID          = "payout_paid"
    PAYOUT_REJECTED      = "payout_rejected"
    CHALLENGE_COMPLETED  = "challenge_completed"
    BADGE_EARNED         = "badge_earned"
    DISPUTE_FLAGGED      = "dispute_flagged"
    DISPUTE_RESOLVED     = "dispute_resolved"
    ORG_INVITE           = "org_invite"
    ORG_MEMBER_JOINED    = "org_member_joined"
    SYSTEM               = "system"           # Generic system/platform notification
    COMMENT_RECEIVED     = "comment_received" # New comment on a task
    PLAN_UPDATED         = "plan_updated"     # Subscription plan changed
    PAYMENT_RECEIVED     = "payment_received" # Successful payment / credits added
    PAYMENT_FAILED       = "payment_failed"   # Failed payment
    WORKER_INVITED       = "worker_invited"   # Requester invited worker to a task
    INVITE_ACCEPTED      = "invite_accepted"  # Worker accepted an invite
    INVITE_DECLINED      = "invite_declined"  # Worker declined an invite
    WATCHLIST_ALERT      = "watchlist_alert"  # Watched task became available again
    RATING_RECEIVED          = "rating_received"        # Requester rated worker's output
    APPLICATION_RECEIVED     = "application_received"   # Requester got a new application
    APPLICATION_ACCEPTED     = "application_accepted"   # Worker's application was accepted
    APPLICATION_REJECTED     = "application_rejected"   # Worker's application was rejected
    TEAM_TASK_ASSIGNED       = "team_task_assigned"     # A task was assigned to a worker team
    TASK_MESSAGE             = "task_message"           # A direct message about a task
    TASK_TIMED_OUT           = "task_timed_out"         # A worker's assignment timed out; task reopened


async def create_notification(
    db: AsyncSession,
    user_id: UUID,
    type: str,
    title: str,
    body: str,
    link: Optional[str] = None,
) -> NotificationDB:
    """Persist a notification for a user. Fire-and-forget friendly — swallows errors."""
    try:
        notif = NotificationDB(
            id=_uuid.uuid4(),
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            link=link,
            is_read=False,
        )
        db.add(notif)
        await db.flush()  # write without committing (caller commits)
        logger.debug("notification.created", user_id=str(user_id), type=type)
        return notif
    except Exception:
        logger.exception("notification.create_failed", user_id=str(user_id), type=type)
        raise
