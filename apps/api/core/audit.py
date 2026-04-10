"""Admin audit trail helper.

Usage:
    from core.audit import log_admin_action

    await log_admin_action(
        db=db,
        admin_id=admin_user_id,
        action="ban_user",
        target_type="user",
        target_id=str(target_user_id),
        detail={"reason": "spam", "ban_expires_at": None},
        ip_address=request.client.host,
    )
"""

import uuid
from typing import Optional, Any
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

logger = structlog.get_logger()


async def log_admin_action(
    db: AsyncSession,
    admin_id: Any,          # UUID or str
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    detail: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> None:
    """
    Append a row to admin_audit_logs.
    This is fire-and-forget — errors are logged but not raised.
    """
    try:
        from models.db import AdminAuditLogDB
        entry = AdminAuditLogDB(
            id=uuid.uuid4(),
            admin_id=admin_id if admin_id else None,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
            detail=detail,
            ip_address=ip_address,
        )
        db.add(entry)
        # Don't flush here — caller commits their own transaction
        logger.info(
            "audit.admin_action",
            admin_id=str(admin_id) if admin_id else None,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
        )
    except SQLAlchemyError:
        logger.exception("audit.log_error", action=action)
