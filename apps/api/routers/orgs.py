"""Organization / team management — shared credits, members, invites."""
from __future__ import annotations
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, case

from core.auth import get_current_user_id
from core.database import get_db
from core.notify import create_notification, NotifType
from models.db import OrganizationDB, OrgMemberDB, OrgInviteDB, UserDB, CreditTransactionDB, TaskDB
from models.schemas import (
    OrgCreateRequest, OrgUpdateRequest, OrgOut, OrgMemberOut,
    OrgInviteRequest, OrgInviteOut,
    OrgCreditsTransferRequest,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/orgs", tags=["organizations"])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_org_and_require_role(
    org_id: UUID,
    user_id: str,
    db: AsyncSession,
    min_role: str = "member",
) -> tuple[OrganizationDB, OrgMemberDB]:
    """Fetch org + membership, raising 404/403 if not found or insufficient role."""
    org_result = await db.execute(select(OrganizationDB).where(OrganizationDB.id == org_id))
    org = org_result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    mem_result = await db.execute(
        select(OrgMemberDB).where(
            OrgMemberDB.org_id == org_id,
            OrgMemberDB.user_id == user_id,
        )
    )
    member = mem_result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=403, detail="You are not a member of this organization")

    role_order = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}
    if role_order.get(member.role, 0) < role_order.get(min_role, 0):
        raise HTTPException(
            status_code=403,
            detail=f"Requires '{min_role}' role or higher",
        )

    return org, member


async def _org_to_out(org: OrganizationDB, db: AsyncSession) -> OrgOut:
    member_count = await db.scalar(
        select(func.count()).where(OrgMemberDB.org_id == org.id)
    ) or 0
    return OrgOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        owner_id=org.owner_id,
        credits=org.credits,
        plan=org.plan,
        description=org.description,
        avatar_url=org.avatar_url,
        member_count=member_count,
        created_at=org.created_at,
    )


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("", response_model=OrgOut, status_code=201)
async def create_org(
    req: OrgCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Create a new organization. The caller becomes the owner."""
    # Check slug uniqueness
    existing = await db.scalar(
        select(func.count()).where(OrganizationDB.slug == req.slug)
    )
    if existing:
        raise HTTPException(status_code=409, detail="Slug already taken")

    org = OrganizationDB(
        id=uuid4(),
        name=req.name,
        slug=req.slug,
        owner_id=user_id,
        description=req.description,
        credits=0,
    )
    db.add(org)
    await db.flush()  # Get org.id

    # Add creator as owner member
    member = OrgMemberDB(
        id=uuid4(),
        org_id=org.id,
        user_id=user_id,
        role="owner",
    )
    db.add(member)

    await db.commit()
    await db.refresh(org)

    logger.info("org_created", org_id=str(org.id), owner_id=user_id, slug=req.slug)

    return await _org_to_out(org, db)


@router.get("", response_model=list[OrgOut])
async def list_my_orgs(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List all organizations the current user belongs to."""
    result = await db.execute(
        select(OrganizationDB)
        .join(OrgMemberDB, OrgMemberDB.org_id == OrganizationDB.id)
        .where(OrgMemberDB.user_id == user_id)
        .order_by(OrgMemberDB.joined_at.asc())
    )
    orgs = result.scalars().all()
    return [await _org_to_out(org, db) for org in orgs]


@router.get("/{org_id}", response_model=OrgOut)
async def get_org(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get org details (must be a member)."""
    org, _ = await _get_org_and_require_role(org_id, user_id, db, min_role="viewer")
    return await _org_to_out(org, db)


@router.patch("/{org_id}", response_model=OrgOut)
async def update_org(
    org_id: UUID,
    req: OrgUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Update org name/description/avatar (admin+)."""
    org, _ = await _get_org_and_require_role(org_id, user_id, db, min_role="admin")

    if req.name is not None:
        org.name = req.name
    if req.description is not None:
        org.description = req.description
    if req.avatar_url is not None:
        org.avatar_url = req.avatar_url

    await db.commit()
    await db.refresh(org)
    return await _org_to_out(org, db)


@router.delete("/{org_id}", status_code=204, response_model=None)
async def delete_org(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Delete an org (owner only). Remaining org credits are lost."""
    org, _ = await _get_org_and_require_role(org_id, user_id, db, min_role="owner")
    await db.delete(org)
    await db.commit()
    logger.info("org_deleted", org_id=str(org_id), by=user_id)


# ── Members ───────────────────────────────────────────────────────────────────

@router.get("/{org_id}/members", response_model=list[OrgMemberOut])
async def list_members(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List all members of an org."""
    await _get_org_and_require_role(org_id, user_id, db, min_role="viewer")

    result = await db.execute(
        select(OrgMemberDB, UserDB)
        .join(UserDB, UserDB.id == OrgMemberDB.user_id)
        .where(OrgMemberDB.org_id == org_id)
        .order_by(OrgMemberDB.joined_at.asc())
    )
    rows = result.all()

    return [
        OrgMemberOut(
            id=m.id,
            org_id=m.org_id,
            user_id=m.user_id,
            name=u.name,
            email=u.email,
            role=m.role,
            joined_at=m.joined_at,
        )
        for m, u in rows
    ]


@router.patch("/{org_id}/members/{member_user_id}", response_model=OrgMemberOut)
async def update_member_role(
    org_id: UUID,
    member_user_id: UUID,
    role: str = Query(..., description="New role: admin | member | viewer"),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Change a member's role (admin+ can change members; only owner can change admins)."""
    if role not in ("admin", "member", "viewer"):
        raise HTTPException(status_code=400, detail="role must be admin, member, or viewer")

    _, my_member = await _get_org_and_require_role(org_id, user_id, db, min_role="admin")

    # Can't change the owner
    if str(member_user_id) == user_id and my_member.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot change the owner's role this way")

    target_result = await db.execute(
        select(OrgMemberDB, UserDB)
        .join(UserDB, UserDB.id == OrgMemberDB.user_id)
        .where(
            OrgMemberDB.org_id == org_id,
            OrgMemberDB.user_id == member_user_id,
        )
    )
    row = target_result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Member not found")

    target_member, target_user = row

    if target_member.role == "owner":
        raise HTTPException(status_code=403, detail="Cannot change the owner's role")

    # Only owner can promote to admin
    if role == "admin" and my_member.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can promote to admin")

    target_member.role = role
    await db.commit()
    await db.refresh(target_member)

    return OrgMemberOut(
        id=target_member.id,
        org_id=target_member.org_id,
        user_id=target_member.user_id,
        name=target_user.name,
        email=target_user.email,
        role=target_member.role,
        joined_at=target_member.joined_at,
    )


@router.delete("/{org_id}/members/{member_user_id}", status_code=204, response_model=None)
async def remove_member(
    org_id: UUID,
    member_user_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Remove a member from an org (admin+ or self-removal)."""
    org, my_member = await _get_org_and_require_role(org_id, user_id, db, min_role="member")

    is_self_removal = str(member_user_id) == user_id

    if not is_self_removal:
        # Must be admin+ to remove others
        role_order = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}
        if role_order.get(my_member.role, 0) < role_order.get("admin", 0):
            raise HTTPException(status_code=403, detail="Requires 'admin' role or higher")

    target_result = await db.execute(
        select(OrgMemberDB).where(
            OrgMemberDB.org_id == org_id,
            OrgMemberDB.user_id == member_user_id,
        )
    )
    target = target_result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")

    if target.role == "owner":
        raise HTTPException(status_code=403, detail="Cannot remove the org owner. Transfer ownership first.")

    await db.delete(target)
    await db.commit()


# ── Invites ───────────────────────────────────────────────────────────────────

@router.post("/{org_id}/invites", response_model=OrgInviteOut, status_code=201)
async def invite_member(
    org_id: UUID,
    req: OrgInviteRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Invite someone to join the org by email (admin+)."""
    org, _ = await _get_org_and_require_role(org_id, user_id, db, min_role="admin")

    # Check if already a member
    existing_user = await db.scalar(
        select(func.count())
        .select_from(UserDB)
        .join(OrgMemberDB, OrgMemberDB.user_id == UserDB.id)
        .where(UserDB.email == req.email, OrgMemberDB.org_id == org_id)
    )
    if existing_user:
        raise HTTPException(status_code=409, detail="This email is already a member of the org")

    # Check for pending invite
    pending = await db.scalar(
        select(func.count()).where(
            OrgInviteDB.org_id == org_id,
            OrgInviteDB.email == req.email,
            OrgInviteDB.accepted_at.is_(None),
            OrgInviteDB.expires_at > datetime.now(timezone.utc),
        )
    )
    if pending:
        raise HTTPException(status_code=409, detail="A pending invite already exists for this email")

    token = secrets.token_urlsafe(32)
    invite = OrgInviteDB(
        id=uuid4(),
        org_id=org_id,
        email=req.email,
        role=req.role,
        token=token,
        invited_by=user_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)

    # Notify the invited user if they already have an account
    invited_user = await db.scalar(
        select(UserDB.id).where(UserDB.email == req.email)
    )
    if invited_user:
        await create_notification(
            db=db,
            user_id=str(invited_user),
            type=NotifType.ORG_INVITE,
            title=f"You've been invited to join {org.name}",
            body=f"Accept the invitation to join the '{org.name}' team.",
            link=f"/orgs/join?token={token}",
        )
        await db.commit()

    logger.info("org_invite_sent", org_id=str(org_id), email=req.email, invited_by=user_id)

    # Advance requester onboarding: invite_team step
    import asyncio as _asyncio
    async def _adv_onboarding():
        from core.database import AsyncSessionLocal
        from routers.requester_onboarding import complete_step_internal
        async with AsyncSessionLocal() as _db:
            try:
                await complete_step_internal(str(user_id), "invite_team", _db)
            except Exception:
                pass
    _asyncio.create_task(_adv_onboarding())

    return OrgInviteOut(
        id=invite.id,
        org_id=invite.org_id,
        email=invite.email,
        role=invite.role,
        token=invite.token,
        expires_at=invite.expires_at,
        accepted_at=invite.accepted_at,
        created_at=invite.created_at,
    )


@router.get("/{org_id}/invites", response_model=list[OrgInviteOut])
async def list_invites(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List pending invites for an org (admin+)."""
    await _get_org_and_require_role(org_id, user_id, db, min_role="admin")

    result = await db.execute(
        select(OrgInviteDB).where(
            OrgInviteDB.org_id == org_id,
            OrgInviteDB.accepted_at.is_(None),
        ).order_by(OrgInviteDB.created_at.desc())
    )
    invites = result.scalars().all()
    return [
        OrgInviteOut(
            id=i.id,
            org_id=i.org_id,
            email=i.email,
            role=i.role,
            token=i.token,
            expires_at=i.expires_at,
            accepted_at=i.accepted_at,
            created_at=i.created_at,
        )
        for i in invites
    ]


@router.delete("/{org_id}/invites/{invite_id}", status_code=204, response_model=None)
async def cancel_invite(
    org_id: UUID,
    invite_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Cancel a pending invite (admin+)."""
    await _get_org_and_require_role(org_id, user_id, db, min_role="admin")

    result = await db.execute(
        select(OrgInviteDB).where(
            OrgInviteDB.id == invite_id,
            OrgInviteDB.org_id == org_id,
        )
    )
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    await db.delete(invite)
    await db.commit()


# ── Accept invite (public endpoint — uses token) ──────────────────────────────

@router.post("/join", response_model=OrgOut)
async def accept_invite(
    token: str = Query(..., description="Invite token from email/link"),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Accept an org invitation using a token."""
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(OrgInviteDB).where(
            OrgInviteDB.token == token,
            OrgInviteDB.accepted_at.is_(None),
            OrgInviteDB.expires_at > now,
        )
    )
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invalid or expired invite token")

    # Verify the current user's email matches the invite email
    user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.email.lower() != invite.email.lower():
        raise HTTPException(
            status_code=403,
            detail=f"This invite was sent to {invite.email}. Please log in with that account.",
        )

    # Check if already a member
    already = await db.scalar(
        select(func.count()).where(
            OrgMemberDB.org_id == invite.org_id,
            OrgMemberDB.user_id == user_id,
        )
    )
    if already:
        raise HTTPException(status_code=409, detail="Already a member of this org")

    # Accept invite
    invite.accepted_at = now
    member = OrgMemberDB(
        id=uuid4(),
        org_id=invite.org_id,
        user_id=user_id,
        role=invite.role,
    )
    db.add(member)

    # Notify org owner
    org_result = await db.execute(select(OrganizationDB).where(OrganizationDB.id == invite.org_id))
    org = org_result.scalar_one_or_none()
    if org:
        await create_notification(
            db=db,
            user_id=str(org.owner_id),
            type=NotifType.ORG_MEMBER_JOINED,
            title=f"{user.name or user.email} joined {org.name}",
            body=f"A new member has joined your organization.",
            link=f"/dashboard/team/{invite.org_id}",
        )

    await db.commit()
    await db.refresh(org)

    logger.info("org_invite_accepted", org_id=str(invite.org_id), user_id=user_id)

    return await _org_to_out(org, db)


# ── Credits transfer ──────────────────────────────────────────────────────────

@router.post("/{org_id}/credits/transfer", response_model=OrgOut)
async def transfer_credits(
    org_id: UUID,
    req: OrgCreditsTransferRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Transfer credits between personal account and org pool.
    - to_org: personal → org pool
    - from_org: org pool → personal
    """
    org, member = await _get_org_and_require_role(org_id, user_id, db, min_role="admin")

    user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if req.direction == "to_org":
        if user.credits < req.amount:
            raise HTTPException(
                status_code=402,
                detail={"error": "insufficient_credits", "available": user.credits, "required": req.amount},
            )
        user.credits -= req.amount
        org.credits += req.amount
        desc = f"Transferred {req.amount} credits to org '{org.name}'"
    else:  # from_org
        if org.credits < req.amount:
            raise HTTPException(
                status_code=402,
                detail={"error": "insufficient_org_credits", "available": org.credits, "required": req.amount},
            )
        org.credits -= req.amount
        user.credits += req.amount
        desc = f"Withdrew {req.amount} credits from org '{org.name}'"

    txn = CreditTransactionDB(
        user_id=user_id,
        amount=req.amount if req.direction == "from_org" else -req.amount,
        type="credit" if req.direction == "from_org" else "charge",
        description=desc,
    )
    db.add(txn)

    await db.commit()
    await db.refresh(org)

    logger.info("org_credits_transfer", org_id=str(org_id), direction=req.direction,
                amount=req.amount, by=user_id)

    return await _org_to_out(org, db)


# ── Active org context ────────────────────────────────────────────────────────

@router.post("/{org_id}/activate", response_model=OrgOut)
async def set_active_org(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Set the active org context for the current user session."""
    org, _ = await _get_org_and_require_role(org_id, user_id, db, min_role="viewer")

    user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_result.scalar_one_or_none()
    if user:
        user.active_org_id = org_id
        await db.commit()

    return await _org_to_out(org, db)


@router.post("/deactivate", status_code=204, response_model=None)
async def deactivate_org(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Clear the active org context (switch back to personal mode)."""
    user_result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    user = user_result.scalar_one_or_none()
    if user:
        user.active_org_id = None
        await db.commit()


# ── Org Analytics ──────────────────────────────────────────────────────────────

@router.get("/{org_id}/analytics")
async def org_analytics(
    org_id: UUID,
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Return org-level analytics for the past N days.

    Includes:
    - Aggregate stats: total tasks, completed, failed, credits spent
    - Task type breakdown (count + credits per type)
    - Per-member breakdown: tasks created, completed, credits spent
    - Daily activity series (task counts per day, last 30 days)

    Requires: caller must be a member of the org.
    """
    org, _ = await _get_org_and_require_role(org_id, user_id, db, min_role="member")

    since = datetime.now(timezone.utc) - timedelta(days=days)

    # ── Aggregate task stats ───────────────────────────────────────────────
    agg_result = await db.execute(
        select(
            func.count(TaskDB.id).label("total"),
            func.sum(case((TaskDB.status == "completed", 1), else_=0)).label("completed"),
            func.sum(case((TaskDB.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((TaskDB.status == "running", 1), else_=0)).label("running"),
            func.coalesce(func.sum(TaskDB.credits_used), 0).label("credits_spent"),
            func.avg(TaskDB.duration_ms).label("avg_duration_ms"),
        ).where(
            TaskDB.org_id == org_id,
            TaskDB.created_at >= since,
        )
    )
    agg = agg_result.one()

    # ── Task type breakdown ────────────────────────────────────────────────
    type_result = await db.execute(
        select(
            TaskDB.type,
            func.count(TaskDB.id).label("count"),
            func.coalesce(func.sum(TaskDB.credits_used), 0).label("credits"),
            func.sum(case((TaskDB.status == "completed", 1), else_=0)).label("completed"),
        ).where(
            TaskDB.org_id == org_id,
            TaskDB.created_at >= since,
        ).group_by(TaskDB.type)
        .order_by(func.count(TaskDB.id).desc())
    )
    by_type = [
        {
            "type": r.type,
            "count": r.count,
            "completed": r.completed,
            "credits": int(r.credits),
            "completion_rate": round(r.completed / r.count * 100, 1) if r.count else 0,
        }
        for r in type_result.all()
    ]

    # ── Per-member breakdown ───────────────────────────────────────────────
    member_result = await db.execute(
        select(
            UserDB.id,
            UserDB.name,
            UserDB.email,
            func.count(TaskDB.id).label("tasks_created"),
            func.sum(case((TaskDB.status == "completed", 1), else_=0)).label("tasks_completed"),
            func.coalesce(func.sum(TaskDB.credits_used), 0).label("credits_spent"),
        ).join(TaskDB, and_(TaskDB.user_id == UserDB.id, TaskDB.org_id == org_id, TaskDB.created_at >= since), isouter=True)
        .join(OrgMemberDB, and_(OrgMemberDB.org_id == org_id, OrgMemberDB.user_id == UserDB.id))
        .group_by(UserDB.id, UserDB.name, UserDB.email)
        .order_by(func.count(TaskDB.id).desc())
    )
    by_member = [
        {
            "user_id": str(r.id),
            "name": r.name or r.email,
            "tasks_created": r.tasks_created or 0,
            "tasks_completed": r.tasks_completed or 0,
            "credits_spent": int(r.credits_spent or 0),
        }
        for r in member_result.all()
    ]

    # ── Daily series (last 30 days, bucketed by day) ───────────────────────
    from sqlalchemy import text, cast, Date
    daily_result = await db.execute(
        select(
            cast(TaskDB.created_at, Date).label("day"),
            func.count(TaskDB.id).label("count"),
            func.sum(case((TaskDB.status == "completed", 1), else_=0)).label("completed"),
        ).where(
            TaskDB.org_id == org_id,
            TaskDB.created_at >= since,
        ).group_by(cast(TaskDB.created_at, Date))
        .order_by(cast(TaskDB.created_at, Date))
    )
    daily = [
        {"day": str(r.day), "count": r.count, "completed": r.completed}
        for r in daily_result.all()
    ]

    total = int(agg.total or 0)
    completed = int(agg.completed or 0)

    return {
        "org_id": str(org_id),
        "org_name": org.name,
        "days": days,
        "summary": {
            "total_tasks": total,
            "completed": completed,
            "failed": int(agg.failed or 0),
            "running": int(agg.running or 0),
            "credits_spent": int(agg.credits_spent or 0),
            "completion_rate": round(completed / total * 100, 1) if total else 0.0,
            "avg_duration_ms": round(float(agg.avg_duration_ms)) if agg.avg_duration_ms else None,
        },
        "by_type": by_type,
        "by_member": by_member,
        "daily": daily,
    }
