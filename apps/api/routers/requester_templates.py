"""Requester private saved task templates + public template marketplace.

Personal templates allow requesters to quickly recreate tasks with the same
configuration.  Templates can optionally be published to the public marketplace
so other requesters can discover and import them.
"""
from __future__ import annotations
import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.scopes import require_scope, SCOPE_TASKS_READ, SCOPE_TASKS_WRITE
from core.database import get_db
from models.db import RequesterSavedTemplateDB, UserDB
from models.schemas import (
    RequesterTemplateCreateRequest,
    RequesterTemplateUpdateRequest,
    RequesterTemplateOut,
    RequesterTemplateListOut,
    TemplatePublishRequest,
    MarketplaceTemplateOut,
    MarketplaceTemplateListOut,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/task-templates", tags=["tasks"])

# Separate router for the /v1/tasks/templates alias (used by new-task.astro)
tasks_alias_router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

# A separate router mounted at /v1/template-marketplace for public browsing
marketplace_router = APIRouter(prefix="/v1/template-marketplace", tags=["marketplace"])

MAX_TEMPLATES_PER_USER = 50


# ─── List (personal) ──────────────────────────────────────────────────────────

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


# ─── Create ───────────────────────────────────────────────────────────────────

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


# ─── Get one ──────────────────────────────────────────────────────────────────

@router.get("/{template_id}", response_model=RequesterTemplateOut)
async def get_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
):
    """Get a single saved template (must be owner)."""
    tpl = await _get_owned(db, template_id, user_id)
    return tpl


# ─── Update ───────────────────────────────────────────────────────────────────

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


# ─── Delete ───────────────────────────────────────────────────────────────────

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


# ─── Publish to marketplace ───────────────────────────────────────────────────

@router.post("/{template_id}/publish", response_model=RequesterTemplateOut)
async def publish_template(
    template_id: UUID,
    req: TemplatePublishRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Make a personal template visible in the public marketplace.

    Sets ``is_public=True`` and optionally overrides display metadata
    (title, description, tags).
    """
    tpl = await _get_owned(db, template_id, user_id)

    tpl.is_public = True
    tpl.published_at = datetime.now(timezone.utc)
    if req.marketplace_title is not None:
        tpl.marketplace_title = req.marketplace_title
    if req.marketplace_description is not None:
        tpl.marketplace_description = req.marketplace_description
    if req.marketplace_tags is not None:
        tpl.marketplace_tags = [t.strip().lower() for t in req.marketplace_tags if t.strip()][:10]

    await db.commit()
    await db.refresh(tpl)
    logger.info(
        "template.published",
        template_id=str(template_id),
        user_id=str(user_id),
    )
    return tpl


@router.delete("/{template_id}/publish", status_code=204)
async def unpublish_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Remove a template from the public marketplace (keeps the personal copy)."""
    tpl = await _get_owned(db, template_id, user_id)
    tpl.is_public = False
    tpl.published_at = None
    await db.commit()
    logger.info("template.unpublished", template_id=str(template_id), user_id=str(user_id))


# ─── Marketplace ─────────────────────────────────────────────────────────────

@marketplace_router.get("", response_model=MarketplaceTemplateListOut)
async def browse_marketplace(
    search: Optional[str] = Query(None, description="Search title / description"),
    task_type: Optional[str] = Query(None),
    tag: Optional[str] = Query(None, description="Filter by marketplace tag"),
    sort: str = Query("popular", description="popular | newest | most_used"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List public templates in the marketplace.

    Supports search, task type filter, tag filter, and sorting.
    """
    conditions = [RequesterSavedTemplateDB.is_public.is_(True)]

    if task_type:
        conditions.append(RequesterSavedTemplateDB.task_type == task_type)

    if search:
        q = f"%{search.lower()}%"
        conditions.append(
            or_(
                RequesterSavedTemplateDB.marketplace_title.ilike(q),
                RequesterSavedTemplateDB.name.ilike(q),
                RequesterSavedTemplateDB.marketplace_description.ilike(q),
            )
        )

    # Sort
    if sort == "newest":
        order = RequesterSavedTemplateDB.published_at.desc()
    elif sort == "most_used":
        order = RequesterSavedTemplateDB.use_count.desc()
    else:
        order = RequesterSavedTemplateDB.import_count.desc()

    offset = (page - 1) * page_size
    from sqlalchemy import and_

    total = await db.scalar(
        select(func.count()).where(and_(*conditions))
    )
    result = await db.execute(
        select(RequesterSavedTemplateDB)
        .where(and_(*conditions))
        .order_by(order)
        .offset(offset)
        .limit(page_size)
    )
    templates = result.scalars().all()

    # Load author names
    author_ids = list({str(t.user_id) for t in templates})
    authors: dict[str, UserDB] = {}
    if author_ids:
        author_result = await db.execute(
            select(UserDB).where(UserDB.id.in_(author_ids))
        )
        for u in author_result.scalars().all():
            authors[str(u.id)] = u

    out = []
    for t in templates:
        author = authors.get(str(t.user_id))
        out.append(
            MarketplaceTemplateOut(
                id=t.id,
                name=t.name,
                task_type=t.task_type,
                icon=t.icon,
                use_count=t.use_count,
                import_count=t.import_count,
                marketplace_title=t.marketplace_title,
                marketplace_description=t.marketplace_description,
                marketplace_tags=t.marketplace_tags or [],
                published_at=t.published_at,
                author_name=author.name if author else None,
                author_reputation=getattr(author, "reputation_score", None),
            )
        )

    return MarketplaceTemplateListOut(
        templates=out,
        total=total or 0,
        page=page,
        page_size=page_size,
        has_next=(offset + page_size) < (total or 0),
    )


@marketplace_router.get("/{template_id}", response_model=MarketplaceTemplateOut)
async def get_marketplace_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get a single public marketplace template by ID."""
    result = await db.execute(
        select(RequesterSavedTemplateDB).where(
            RequesterSavedTemplateDB.id == template_id,
            RequesterSavedTemplateDB.is_public.is_(True),
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found in marketplace")

    author_result = await db.execute(
        select(UserDB).where(UserDB.id == tpl.user_id)
    )
    author = author_result.scalar_one_or_none()

    return MarketplaceTemplateOut(
        id=tpl.id,
        name=tpl.name,
        task_type=tpl.task_type,
        icon=tpl.icon,
        use_count=tpl.use_count,
        import_count=tpl.import_count,
        marketplace_title=tpl.marketplace_title,
        marketplace_description=tpl.marketplace_description,
        marketplace_tags=tpl.marketplace_tags or [],
        published_at=tpl.published_at,
        author_name=author.name if author else None,
        author_reputation=getattr(author, "reputation_score", None),
    )


@marketplace_router.post("/{template_id}/import", response_model=RequesterTemplateOut, status_code=201)
async def import_marketplace_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
):
    """Import a public marketplace template into your personal library.

    Creates a private copy of the template.  Increments the original's
    ``import_count``.
    """
    # Fetch the original public template
    result = await db.execute(
        select(RequesterSavedTemplateDB).where(
            RequesterSavedTemplateDB.id == template_id,
            RequesterSavedTemplateDB.is_public.is_(True),
        )
    )
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Template not found in marketplace")

    # Enforce per-user limit
    count = await db.scalar(
        select(func.count()).where(RequesterSavedTemplateDB.user_id == user_id)
    )
    if count and count >= MAX_TEMPLATES_PER_USER:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_TEMPLATES_PER_USER} templates per user",
        )

    # Don't allow importing your own template (just use it directly)
    if str(original.user_id) == str(user_id):
        raise HTTPException(status_code=400, detail="You cannot import your own template")

    # Create a private copy
    name = original.marketplace_title or original.name
    copy = RequesterSavedTemplateDB(
        id=_uuid.uuid4(),
        user_id=user_id,
        name=f"{name} (imported)",
        description=original.marketplace_description or original.description,
        task_type=original.task_type,
        task_input=original.task_input,
        task_config=original.task_config,
        icon=original.icon,
        is_public=False,
    )
    db.add(copy)

    # Increment import_count on the original
    original.import_count += 1

    await db.commit()
    await db.refresh(copy)

    logger.info(
        "template.imported",
        original_id=str(template_id),
        copy_id=str(copy.id),
        user_id=str(user_id),
    )
    return copy


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
