"""Platform announcements — admin-created broadcast banners shown to all users.

Public endpoint:
  GET  /v1/announcements          — returns currently active, non-expired
                                    announcements (filtered by role if authed)

Admin endpoints (require is_admin):
  POST   /v1/admin/announcements          — create
  PATCH  /v1/admin/announcements/{id}     — update (any field)
  DELETE /v1/admin/announcements/{id}     — hard delete
  GET    /v1/admin/announcements          — list all (including inactive/expired)
"""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_optional_user_id, require_admin
from core.database import get_db
from models.db import PlatformAnnouncementDB, UserDB

logger = structlog.get_logger()

router = APIRouter(tags=["announcements"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class AnnouncementOut(BaseModel):
    id: UUID
    title: str
    message: str
    type: str
    target_role: str
    is_active: bool
    starts_at: datetime
    expires_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AnnouncementCreate(BaseModel):
    title: str = Field(..., max_length=200, description="Short headline")
    message: str = Field(..., description="Banner body text")
    type: str = Field("info", pattern="^(info|warning|maintenance|feature)$")
    target_role: str = Field("all", pattern="^(all|requester|worker)$")
    starts_at: Optional[datetime] = None   # default = now
    expires_at: Optional[datetime] = None  # None = never expires
    is_active: bool = True


class AnnouncementUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    message: Optional[str] = None
    type: Optional[str] = Field(None, pattern="^(info|warning|maintenance|feature)$")
    target_role: Optional[str] = Field(None, pattern="^(all|requester|worker)$")
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    is_active: Optional[bool] = None


# ── Helper ────────────────────────────────────────────────────────────────────

def _active_filter():
    """SQLAlchemy filter clause for live announcements (active + started + not expired)."""
    now = datetime.now(timezone.utc)
    return and_(
        PlatformAnnouncementDB.is_active == True,
        PlatformAnnouncementDB.starts_at <= now,
        or_(
            PlatformAnnouncementDB.expires_at == None,
            PlatformAnnouncementDB.expires_at > now,
        ),
    )


# ── Public endpoint ───────────────────────────────────────────────────────────

@router.get("/v1/announcements", response_model=list[AnnouncementOut])
async def get_active_announcements(
    db: AsyncSession = Depends(get_db),
    user_id: Optional[str] = Depends(get_optional_user_id),
):
    """Return currently active announcements visible to this user.

    - Unauthenticated callers see only `target_role=all` announcements.
    - Authenticated callers see `all` + their own role's announcements.
    """
    role_filter_values = ["all"]
    if user_id:
        # Determine the user's role for filtering
        try:
            user_res = await db.execute(
                select(UserDB.role).where(UserDB.id == user_id)
            )
            row = user_res.scalar_one_or_none()
            if row in ("worker", "both"):
                role_filter_values.append("worker")
            if row in ("requester", "both"):
                role_filter_values.append("requester")
        except Exception:
            pass  # Fall back to "all" only if user lookup fails

    res = await db.execute(
        select(PlatformAnnouncementDB)
        .where(
            _active_filter(),
            PlatformAnnouncementDB.target_role.in_(role_filter_values),
        )
        .order_by(PlatformAnnouncementDB.created_at.desc())
        .limit(10)  # Safety cap — no more than 10 active banners
    )
    return [AnnouncementOut.model_validate(a) for a in res.scalars().all()]


# ── Admin endpoints ───────────────────────────────────────────────────────────

@router.get("/v1/admin/announcements", response_model=list[AnnouncementOut])
async def admin_list_announcements(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Admin: list all announcements (including inactive and expired)."""
    res = await db.execute(
        select(PlatformAnnouncementDB)
        .order_by(PlatformAnnouncementDB.created_at.desc())
        .limit(200)
    )
    return [AnnouncementOut.model_validate(a) for a in res.scalars().all()]


@router.post("/v1/admin/announcements", response_model=AnnouncementOut, status_code=201)
async def create_announcement(
    body: AnnouncementCreate,
    db: AsyncSession = Depends(get_db),
    admin_id: str = Depends(require_admin),
):
    """Admin: create a new announcement."""
    ann = PlatformAnnouncementDB(
        title=body.title,
        message=body.message,
        type=body.type,
        target_role=body.target_role,
        is_active=body.is_active,
        starts_at=body.starts_at or datetime.now(timezone.utc),
        expires_at=body.expires_at,
        created_by_id=admin_id,
    )
    db.add(ann)
    await db.commit()
    await db.refresh(ann)
    logger.info("announcement_created", id=str(ann.id), title=ann.title,
                type=ann.type, admin=admin_id)
    return AnnouncementOut.model_validate(ann)


@router.patch("/v1/admin/announcements/{announcement_id}", response_model=AnnouncementOut)
async def update_announcement(
    announcement_id: UUID,
    body: AnnouncementUpdate,
    db: AsyncSession = Depends(get_db),
    admin_id: str = Depends(require_admin),
):
    """Admin: update an announcement (partial update — only supplied fields are changed)."""
    res = await db.execute(
        select(PlatformAnnouncementDB).where(
            PlatformAnnouncementDB.id == announcement_id
        )
    )
    ann = res.scalar_one_or_none()
    if not ann:
        raise HTTPException(404, "Announcement not found")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(ann, field, value)

    await db.commit()
    await db.refresh(ann)
    logger.info("announcement_updated", id=str(ann.id), admin=admin_id)
    return AnnouncementOut.model_validate(ann)


@router.delete("/v1/admin/announcements/{announcement_id}", status_code=204,
               response_model=None)
async def delete_announcement(
    announcement_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin_id: str = Depends(require_admin),
):
    """Admin: permanently delete an announcement."""
    res = await db.execute(
        select(PlatformAnnouncementDB).where(
            PlatformAnnouncementDB.id == announcement_id
        )
    )
    ann = res.scalar_one_or_none()
    if not ann:
        raise HTTPException(404, "Announcement not found")
    await db.delete(ann)
    await db.commit()
    logger.info("announcement_deleted", id=str(announcement_id), admin=admin_id)
