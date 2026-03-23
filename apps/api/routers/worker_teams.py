"""Worker Teams — worker-side collaboration groups with invite system.

Workers (anyone with role 'worker' or 'both') can:
  - Create teams and invite other workers
  - Accept / decline invitations
  - Leave teams or remove members (owners only)
  - View team members and activity

These are distinct from requester orgs — purely worker-to-worker associations.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_

from core.auth import get_current_user_id
from core.database import get_db
from core.notify import create_notification, NotifType
from models.db import (
    WorkerTeamDB, WorkerTeamMemberDB, WorkerTeamInviteDB,
    UserDB, TaskDB,
)
from models.schemas import (
    WorkerTeamOut, WorkerTeamDetailOut, WorkerTeamMemberOut,
    WorkerTeamInviteOut, WorkerTeamCreateRequest, WorkerTeamInviteRequest,
    PaginatedWorkerTeams,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/worker-teams", tags=["worker-teams"])

INVITE_TTL_DAYS = 14  # invites expire after 14 days


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _require_worker(user_id: str, db: AsyncSession) -> UserDB:
    """Return the user; raise 403 if not a worker."""
    result = await db.execute(select(UserDB).where(UserDB.id == UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role not in ("worker", "both"):
        raise HTTPException(status_code=403, detail="Only workers can use worker teams")
    return user


async def _get_team(team_id: UUID, db: AsyncSession) -> WorkerTeamDB:
    result = await db.execute(select(WorkerTeamDB).where(WorkerTeamDB.id == team_id))
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


async def _get_membership(team_id: UUID, user_id: UUID, db: AsyncSession) -> Optional[WorkerTeamMemberDB]:
    result = await db.execute(
        select(WorkerTeamMemberDB).where(
            WorkerTeamMemberDB.team_id == team_id,
            WorkerTeamMemberDB.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


def _fmt_team(team: WorkerTeamDB, member_count: int, my_role: Optional[str]) -> WorkerTeamOut:
    return WorkerTeamOut(
        id=str(team.id),
        name=team.name,
        description=team.description,
        avatar_emoji=team.avatar_emoji or "👥",
        created_by=str(team.created_by),
        member_count=member_count,
        created_at=team.created_at.isoformat(),
        updated_at=team.updated_at.isoformat(),
        my_role=my_role,
    )


async def _fmt_member(m: WorkerTeamMemberDB, db: AsyncSession) -> WorkerTeamMemberOut:
    user_result = await db.execute(select(UserDB).where(UserDB.id == m.user_id))
    user = user_result.scalar_one_or_none()
    return WorkerTeamMemberOut(
        user_id=str(m.user_id),
        name=user.full_name or user.username if user else "Unknown",
        role=m.role,
        joined_at=m.joined_at.isoformat(),
        tasks_completed=getattr(user, "tasks_completed", 0) if user else 0,
        xp=getattr(user, "xp", 0) if user else 0,
        level=getattr(user, "level", 1) if user else 1,
    )


async def _fmt_invite(invite: WorkerTeamInviteDB, db: AsyncSession) -> WorkerTeamInviteOut:
    team_result = await db.execute(select(WorkerTeamDB).where(WorkerTeamDB.id == invite.team_id))
    team = team_result.scalar_one_or_none()
    inviter_result = await db.execute(select(UserDB).where(UserDB.id == invite.invited_by))
    inviter = inviter_result.scalar_one_or_none()
    return WorkerTeamInviteOut(
        id=str(invite.id),
        team_id=str(invite.team_id),
        team_name=team.name if team else "Unknown",
        invitee_id=str(invite.invitee_id),
        invited_by=str(invite.invited_by),
        inviter_name=inviter.full_name or inviter.username if inviter else "Unknown",
        status=invite.status,
        message=invite.message,
        created_at=invite.created_at.isoformat(),
        expires_at=invite.expires_at.isoformat() if invite.expires_at else None,
    )


# ── Team CRUD ─────────────────────────────────────────────────────────────────

@router.post("", response_model=WorkerTeamDetailOut, status_code=201)
async def create_team(
    req: WorkerTeamCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Create a new worker team. The creator becomes the owner."""
    user = await _require_worker(user_id, db)

    # Limit: max 5 teams owned per worker
    owned_count = (await db.execute(
        select(func.count()).where(WorkerTeamDB.created_by == UUID(user_id))
    )).scalar() or 0
    if owned_count >= 5:
        raise HTTPException(status_code=400, detail="You can own at most 5 worker teams")

    team = WorkerTeamDB(
        name=req.name.strip(),
        description=req.description,
        avatar_emoji=req.avatar_emoji or "👥",
        created_by=UUID(user_id),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(team)
    await db.flush()

    # Auto-add creator as owner
    membership = WorkerTeamMemberDB(
        team_id=team.id,
        user_id=UUID(user_id),
        role="owner",
        joined_at=utcnow(),
    )
    db.add(membership)
    await db.commit()
    await db.refresh(team)

    member_out = await _fmt_member(membership, db)
    return WorkerTeamDetailOut(
        id=str(team.id),
        name=team.name,
        description=team.description,
        avatar_emoji=team.avatar_emoji or "👥",
        created_by=str(team.created_by),
        member_count=1,
        created_at=team.created_at.isoformat(),
        updated_at=team.updated_at.isoformat(),
        my_role="owner",
        members=[member_out],
        pending_invites=[],
    )


@router.get("", response_model=PaginatedWorkerTeams)
async def list_teams(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List all teams the current worker belongs to."""
    await _require_worker(user_id, db)
    uid = UUID(user_id)

    # Find teams I'm a member of
    membership_q = select(WorkerTeamMemberDB.team_id).where(
        WorkerTeamMemberDB.user_id == uid
    )
    team_ids_result = await db.execute(membership_q)
    team_ids = [r[0] for r in team_ids_result.fetchall()]

    if not team_ids:
        return PaginatedWorkerTeams(items=[], total=0, page=page, page_size=page_size)

    total = len(team_ids)

    teams_result = await db.execute(
        select(WorkerTeamDB)
        .where(WorkerTeamDB.id.in_(team_ids))
        .order_by(WorkerTeamDB.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    teams = teams_result.scalars().all()

    items = []
    for team in teams:
        mc = (await db.execute(
            select(func.count()).where(WorkerTeamMemberDB.team_id == team.id)
        )).scalar() or 0
        membership = await _get_membership(team.id, uid, db)
        items.append(_fmt_team(team, mc, membership.role if membership else None))

    return PaginatedWorkerTeams(items=items, total=total, page=page, page_size=page_size)


@router.get("/invites/pending", response_model=list[WorkerTeamInviteOut])
async def list_pending_invites(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get all pending invitations for the current worker."""
    await _require_worker(user_id, db)
    uid = UUID(user_id)

    invites_result = await db.execute(
        select(WorkerTeamInviteDB).where(
            WorkerTeamInviteDB.invitee_id == uid,
            WorkerTeamInviteDB.status == "pending",
        ).order_by(WorkerTeamInviteDB.created_at.desc())
    )
    invites = invites_result.scalars().all()

    return [await _fmt_invite(inv, db) for inv in invites]


@router.get("/{team_id}", response_model=WorkerTeamDetailOut)
async def get_team(
    team_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get team detail with members and pending invites (members only)."""
    await _require_worker(user_id, db)
    team = await _get_team(team_id, db)

    membership = await _get_membership(team_id, UUID(user_id), db)
    if not membership:
        raise HTTPException(status_code=403, detail="You are not a member of this team")

    # Load members
    members_result = await db.execute(
        select(WorkerTeamMemberDB)
        .where(WorkerTeamMemberDB.team_id == team_id)
        .order_by(WorkerTeamMemberDB.role.desc(), WorkerTeamMemberDB.joined_at)  # owners first
    )
    members = members_result.scalars().all()

    # Load pending invites (owners see them)
    pending_invites = []
    if membership.role == "owner":
        inv_result = await db.execute(
            select(WorkerTeamInviteDB).where(
                WorkerTeamInviteDB.team_id == team_id,
                WorkerTeamInviteDB.status == "pending",
            ).order_by(WorkerTeamInviteDB.created_at.desc())
        )
        pending_invites = [await _fmt_invite(inv, db) for inv in inv_result.scalars().all()]

    member_outs = [await _fmt_member(m, db) for m in members]
    mc = len(members)

    return WorkerTeamDetailOut(
        id=str(team.id),
        name=team.name,
        description=team.description,
        avatar_emoji=team.avatar_emoji or "👥",
        created_by=str(team.created_by),
        member_count=mc,
        created_at=team.created_at.isoformat(),
        updated_at=team.updated_at.isoformat(),
        my_role=membership.role,
        members=member_outs,
        pending_invites=pending_invites,
    )


@router.delete("/{team_id}", status_code=204)
async def delete_team(
    team_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Delete a team (owner only). Cascades to members and invites."""
    await _require_worker(user_id, db)
    team = await _get_team(team_id, db)

    if str(team.created_by) != user_id:
        raise HTTPException(status_code=403, detail="Only the team owner can delete the team")

    await db.delete(team)
    await db.commit()


# ── Invite Flow ───────────────────────────────────────────────────────────────

@router.post("/{team_id}/invite", response_model=WorkerTeamInviteOut, status_code=201)
async def invite_worker(
    team_id: UUID,
    req: WorkerTeamInviteRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Invite another worker to the team (any member can invite)."""
    await _require_worker(user_id, db)
    team = await _get_team(team_id, db)

    # Must be a team member to invite
    membership = await _get_membership(team_id, UUID(user_id), db)
    if not membership:
        raise HTTPException(status_code=403, detail="You must be a team member to invite others")

    # Find the invitee by username or email
    invitee_result = await db.execute(
        select(UserDB).where(
            or_(
                UserDB.username == req.username.strip(),
                UserDB.email == req.username.strip().lower(),
            )
        )
    )
    invitee = invitee_result.scalar_one_or_none()
    if not invitee:
        raise HTTPException(status_code=404, detail=f"Worker '{req.username}' not found")
    if invitee.role not in ("worker", "both"):
        raise HTTPException(status_code=400, detail="That user is not a worker")
    if str(invitee.id) == user_id:
        raise HTTPException(status_code=400, detail="You cannot invite yourself")

    # Check if already a member
    existing_membership = await _get_membership(team_id, invitee.id, db)
    if existing_membership:
        raise HTTPException(status_code=400, detail="That worker is already a team member")

    # Check for existing pending invite
    existing_invite = await db.execute(
        select(WorkerTeamInviteDB).where(
            WorkerTeamInviteDB.team_id == team_id,
            WorkerTeamInviteDB.invitee_id == invitee.id,
            WorkerTeamInviteDB.status == "pending",
        )
    )
    if existing_invite.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="A pending invite already exists for that worker")

    # Team size limit: 20 members
    current_count = (await db.execute(
        select(func.count()).where(WorkerTeamMemberDB.team_id == team_id)
    )).scalar() or 0
    if current_count >= 20:
        raise HTTPException(status_code=400, detail="Teams are limited to 20 members")

    invite = WorkerTeamInviteDB(
        team_id=team_id,
        invitee_id=invitee.id,
        invited_by=UUID(user_id),
        status="pending",
        message=req.message,
        created_at=utcnow(),
        expires_at=utcnow() + timedelta(days=INVITE_TTL_DAYS),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)

    logger.info("worker_team_invite_sent",
                team_id=str(team_id), invitee=str(invitee.id), inviter=user_id)
    return await _fmt_invite(invite, db)


@router.post("/invites/{invite_id}/accept", response_model=WorkerTeamDetailOut)
async def accept_invite(
    invite_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Accept a team invitation."""
    await _require_worker(user_id, db)

    invite_result = await db.execute(
        select(WorkerTeamInviteDB).where(
            WorkerTeamInviteDB.id == invite_id,
            WorkerTeamInviteDB.invitee_id == UUID(user_id),
        )
    )
    invite = invite_result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.status != "pending":
        raise HTTPException(status_code=400, detail=f"Invite is already {invite.status}")
    if invite.expires_at and invite.expires_at < utcnow():
        invite.status = "declined"
        await db.commit()
        raise HTTPException(status_code=400, detail="This invite has expired")

    # Create membership
    membership = WorkerTeamMemberDB(
        team_id=invite.team_id,
        user_id=UUID(user_id),
        role="member",
        joined_at=utcnow(),
    )
    db.add(membership)
    invite.status = "accepted"
    await db.commit()

    logger.info("worker_team_invite_accepted",
                team_id=str(invite.team_id), user=user_id)

    # Return team detail
    return await get_team(invite.team_id, db, user_id)


@router.post("/invites/{invite_id}/decline", status_code=200)
async def decline_invite(
    invite_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Decline a team invitation."""
    await _require_worker(user_id, db)

    invite_result = await db.execute(
        select(WorkerTeamInviteDB).where(
            WorkerTeamInviteDB.id == invite_id,
            WorkerTeamInviteDB.invitee_id == UUID(user_id),
        )
    )
    invite = invite_result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.status != "pending":
        raise HTTPException(status_code=400, detail=f"Invite is already {invite.status}")

    invite.status = "declined"
    await db.commit()
    return {"message": "Invite declined"}


@router.delete("/{team_id}/members/{member_user_id}", status_code=204)
async def remove_member(
    team_id: UUID,
    member_user_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Remove a member from the team (owner only) or leave the team (self)."""
    await _require_worker(user_id, db)
    team = await _get_team(team_id, db)

    my_membership = await _get_membership(team_id, UUID(user_id), db)
    if not my_membership:
        raise HTTPException(status_code=403, detail="You are not a member of this team")

    is_self = member_user_id == UUID(user_id)

    # Can remove self (leave), or owner can remove others
    if not is_self and my_membership.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can remove other members")

    # Owner cannot leave — must delete team or transfer ownership
    if is_self and my_membership.role == "owner":
        raise HTTPException(
            status_code=400,
            detail="As owner, you cannot leave the team. Delete the team or transfer ownership first.",
        )

    target_membership = await _get_membership(team_id, member_user_id, db)
    if not target_membership:
        raise HTTPException(status_code=404, detail="Member not found in this team")

    await db.delete(target_membership)
    await db.commit()


# ── Team Task Routing ──────────────────────────────────────────────────────────

class AssignTeamRequest(BaseModel):
    team_id: UUID


@router.post("/v1/tasks/{task_id}/assign-team", status_code=200)
async def assign_team_to_task(
    task_id: UUID,
    req: AssignTeamRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Requester assigns a task to a specific worker team."""
    # Load task, verify ownership
    task_result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if str(task.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Only the task owner can assign teams")
    if task.status not in ("open", "pending"):
        raise HTTPException(status_code=400, detail="Task must be open or pending to assign a team")

    # Verify team exists
    team = await _get_team(req.team_id, db)

    task.assigned_team_id = req.team_id
    await db.flush()

    # Notify all team members
    members_result = await db.execute(
        select(WorkerTeamMemberDB).where(WorkerTeamMemberDB.team_id == req.team_id)
    )
    members = members_result.scalars().all()
    for member in members:
        try:
            await create_notification(
                db,
                member.user_id,
                NotifType.TEAM_TASK_ASSIGNED,
                "New team task available",
                f"A task has been assigned to your team '{team.name}'.",
                link=f"/worker/marketplace",
            )
        except Exception:  # noqa: BLE001
            pass

    await db.commit()
    logger.info("team_task.assigned", task_id=str(task_id), team_id=str(req.team_id))
    return {"task_id": str(task_id), "assigned_team_id": str(req.team_id), "team_name": team.name}


@router.delete("/v1/tasks/{task_id}/assign-team", status_code=204)
async def remove_team_from_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Requester removes team assignment from a task."""
    task_result = await db.execute(select(TaskDB).where(TaskDB.id == task_id))
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if str(task.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Only the task owner can remove team assignments")

    task.assigned_team_id = None
    await db.commit()
    logger.info("team_task.unassigned", task_id=str(task_id))


@router.get("/{team_id}/tasks")
async def list_team_tasks(
    team_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List tasks assigned to this team (members only)."""
    await _require_worker(user_id, db)

    # Verify membership
    membership = await _get_membership(team_id, UUID(user_id), db)
    if not membership:
        raise HTTPException(status_code=403, detail="You are not a member of this team")

    total = (await db.scalar(
        select(func.count()).where(TaskDB.assigned_team_id == team_id)
    )) or 0

    tasks_result = await db.execute(
        select(TaskDB)
        .where(TaskDB.assigned_team_id == team_id)
        .order_by(TaskDB.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    tasks = tasks_result.scalars().all()

    items = [
        {
            "id": str(t.id),
            "type": t.type,
            "status": t.status,
            "priority": t.priority,
            "worker_reward_credits": t.worker_reward_credits,
            "task_instructions": t.task_instructions,
            "created_at": t.created_at.isoformat(),
        }
        for t in tasks
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}
