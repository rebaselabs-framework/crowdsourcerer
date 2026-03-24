"""Custom webhook payload templates — per-user, per-event-type JSON templates.

Users can define {{field}} placeholder templates that are rendered at webhook
delivery time with task context, allowing fully custom payloads per event.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from core.database import get_db
from core.scopes import require_scope, SCOPE_WEBHOOKS_READ, SCOPE_WEBHOOKS_WRITE
from core.webhooks import ALL_EVENTS
from models.db import WebhookPayloadTemplateDB
from models.schemas import WebhookPayloadTemplate, WebhookPayloadTemplateOut

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/webhooks/payload-templates", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Template rendering helper
# ---------------------------------------------------------------------------

def _render_payload_template(template_str: str, context: dict[str, Any]) -> dict:
    """
    Replace {{key}} placeholders in *template_str* with values from *context*.

    Returns a parsed JSON dict.  Raises ValueError if the result is not valid JSON
    or if the template string itself is malformed.
    """
    import json

    def replacer(match: re.Match) -> str:
        key = match.group(1).strip()
        value = context.get(key, "")
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value) if value is not None else ""

    rendered = re.sub(r"\{\{(\s*\w[\w.]*\s*)\}\}", replacer, template_str)
    try:
        return json.loads(rendered)
    except Exception as exc:
        raise ValueError(f"Rendered template is not valid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("")
async def list_payload_templates(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_WEBHOOKS_READ)),
):
    """List all custom payload templates for the current user."""
    result = await db.execute(
        select(WebhookPayloadTemplateDB)
        .where(WebhookPayloadTemplateDB.user_id == user_id)
        .order_by(WebhookPayloadTemplateDB.event_type)
    )
    templates = result.scalars().all()
    return {
        "items": [
            {
                "id": t.id,
                "user_id": str(t.user_id),
                "event_type": t.event_type,
                "template": t.template,
                "description": t.description,
                "created_at": t.created_at.isoformat(),
                "updated_at": t.updated_at.isoformat(),
            }
            for t in templates
        ],
        "total": len(templates),
    }


@router.post("", status_code=201)
async def upsert_payload_template(
    body: WebhookPayloadTemplate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_WEBHOOKS_WRITE)),
):
    """
    Create or replace the payload template for a specific event type.

    Only one template per (user, event_type) is stored — this is an upsert.
    """
    if body.event_type not in ALL_EVENTS:
        raise HTTPException(
            400,
            f"Unknown event type '{body.event_type}'. Valid: {ALL_EVENTS}",
        )

    # Validate that template is a JSON string (may contain {{placeholders}})
    import json, re as _re
    sanitised = _re.sub(r"\{\{[^}]+\}\}", '"__placeholder__"', body.template)
    try:
        json.loads(sanitised)
    except Exception:
        raise HTTPException(400, "template must be a valid JSON string (with optional {{field}} placeholders)")

    # Upsert: delete existing then insert fresh
    await db.execute(
        delete(WebhookPayloadTemplateDB).where(
            WebhookPayloadTemplateDB.user_id == user_id,
            WebhookPayloadTemplateDB.event_type == body.event_type,
        )
    )

    tpl = WebhookPayloadTemplateDB(
        user_id=user_id,
        event_type=body.event_type,
        template=body.template,
        description=body.description,
    )
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)

    logger.info(
        "webhook_payload_template_upserted",
        user_id=str(user_id),
        event_type=body.event_type,
    )

    return {
        "id": tpl.id,
        "user_id": str(tpl.user_id),
        "event_type": tpl.event_type,
        "template": tpl.template,
        "description": tpl.description,
        "created_at": tpl.created_at.isoformat(),
        "updated_at": tpl.updated_at.isoformat(),
    }


@router.get("/{event_type}")
async def get_payload_template(
    event_type: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_WEBHOOKS_READ)),
):
    """Get the payload template for a specific event type."""
    tpl = await _get_owned_template(event_type, user_id, db)
    return {
        "id": tpl.id,
        "user_id": str(tpl.user_id),
        "event_type": tpl.event_type,
        "template": tpl.template,
        "description": tpl.description,
        "created_at": tpl.created_at.isoformat(),
        "updated_at": tpl.updated_at.isoformat(),
    }


@router.delete("/{event_type}", status_code=204, response_model=None)
async def delete_payload_template(
    event_type: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_WEBHOOKS_WRITE)),
):
    """Delete the payload template for a specific event type."""
    tpl = await _get_owned_template(event_type, user_id, db)
    await db.delete(tpl)
    await db.commit()
    logger.info(
        "webhook_payload_template_deleted",
        user_id=str(user_id),
        event_type=event_type,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_owned_template(
    event_type: str,
    user_id: str,
    db: AsyncSession,
) -> WebhookPayloadTemplateDB:
    result = await db.execute(
        select(WebhookPayloadTemplateDB).where(
            WebhookPayloadTemplateDB.user_id == user_id,
            WebhookPayloadTemplateDB.event_type == event_type,
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(404, f"No payload template found for event type '{event_type}'")
    return tpl


async def get_user_template_for_event(
    user_id: str,
    event_type: str,
    db: AsyncSession,
) -> Optional[WebhookPayloadTemplateDB]:
    """Used by webhook delivery to look up a user's custom template (if any)."""
    result = await db.execute(
        select(WebhookPayloadTemplateDB).where(
            WebhookPayloadTemplateDB.user_id == user_id,
            WebhookPayloadTemplateDB.event_type == event_type,
        )
    )
    return result.scalar_one_or_none()
