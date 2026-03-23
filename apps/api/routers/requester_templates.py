"""Requester private saved task templates.

These are personal (not public marketplace) templates that allow requesters
to quickly recreate tasks with the same configuration.
"""
from __future__ import annotations
import uuid as _uuid
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.scopes import require_scope, SCOPE_TASKS_READ, SCOPE_TASKS_WRITE
from core.database import get_db
from models.db import RequesterSavedTemplateDB
from models.schemas import (
    RequesterTemplateCreateRequest,
    RequesterTemplateUpdateRequest,
    RequesterTemplateOut,
    RequesterTemplateListOut,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/task-templates", tags=["tasks"])

# Separate router for the /v1/tasks/templates alias (used by new-task.astro)
tasks_alias_router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

MAX_TEMPLATES_PER_USER = 50


# ─── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=RequesterTemplateListOut)
@tasks_alias_router.get("/templates", response_model=RequesterTemplateListOut)
async def list_my_templates(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """Return all saved task templates for the authenticated requester."""
    offset = (page - 1) * page_size

    total = await db.scalar(
        select(func.count()).where(RequesterSavedTemplateDB.user_id == user_id)
    )
    result = await db.execute(
        select(RequesterSavedTemplateDB)
        .where(RequesterSavedTemplateDB.user_id == user_id)
        .order_by(RequesterSavedTemplateDB.use_count.desc(),
                  RequesterSavedTemplateDB.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = result.scalars().all()
    return RequesterTemplateListOut(templates=rows, total=total or 0)


# ─── Create ────────────────────────────────────────────────────────────────────

@router.post("", response_model=RequesterTemplateOut, status_code=201)
async def create_template(
    req: RequesterTemplateCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Save a new task template."""
    # Enforce per-user limit
    count = await db.scalar(
        select(func.count()).where(RequesterSavedTemplateDB.user_id == user_id)
    )
    if count and count >= MAX_TEMPLATES_PER_USER:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_TEMPLATES_PER_USER} templates per user",
        )

    tpl = RequesterSavedTemplateDB(
        id=_uuid.uuid4(),
        user_id=user_id,
        name=req.name,
        description=req.description,
        task_type=req.task_type,
        task_input=req.task_input,
        task_config=req.task_config,
        icon=req.icon,
    )
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)
    logger.info("template.created", template_id=str(tpl.id), user_id=str(user_id))
    return tpl


# ─── Get one ───────────────────────────────────────────────────────────────────

@router.get("/{template_id}", response_model=RequesterTemplateOut)
async def get_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """Get a single saved template (must be owner)."""
    tpl = await _get_owned(db, template_id, user_id)
    return tpl


# ─── Update ────────────────────────────────────────────────────────────────────

@router.patch("/{template_id}", response_model=RequesterTemplateOut)
async def update_template(
    template_id: UUID,
    req: RequesterTemplateUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Update a saved template."""
    tpl = await _get_owned(db, template_id, user_id)

    if req.name is not None:
        tpl.name = req.name
    if req.description is not None:
        tpl.description = req.description
    if req.task_input is not None:
        tpl.task_input = req.task_input
    if req.task_config is not None:
        tpl.task_config = req.task_config
    if req.icon is not None:
        tpl.icon = req.icon

    await db.commit()
    await db.refresh(tpl)
    return tpl


# ─── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Delete a saved template."""
    tpl = await _get_owned(db, template_id, user_id)
    await db.delete(tpl)
    await db.commit()
    logger.info("template.deleted", template_id=str(template_id), user_id=str(user_id))


# ─── Use (increment counter + return params) ──────────────────────────────────

@router.post("/{template_id}/use", response_model=dict)
async def use_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """Increment the use counter and return the template parameters.

    The frontend calls this when the user clicks a template to pre-fill the
    new-task form.  Returns the same shape the new-task page already expects:
    ``type``, ``default_input``, ``default_settings``.
    """
    tpl = await _get_owned(db, template_id, user_id)
    tpl.use_count += 1
    await db.commit()

    return {
        "id": str(tpl.id),
        "type": tpl.task_type,
        "name": tpl.name,
        "default_input": tpl.task_input,
        "default_settings": tpl.task_config,
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _get_owned(
    db: AsyncSession,
    template_id: UUID,
    user_id: str,
) -> RequesterSavedTemplateDB:
    result = await db.execute(
        select(RequesterSavedTemplateDB).where(
            RequesterSavedTemplateDB.id == template_id,
            RequesterSavedTemplateDB.user_id == user_id,
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl
