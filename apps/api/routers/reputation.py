"""Worker reputation and moderation endpoints."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id, require_admin  # noqa: F401
from core.database import get_db
from core.sql import esc_like, LIKE_ESC
from core.reputation import compute_reputation, refresh_worker_reputation, reputation_tier
from core.notify import create_notification, NotifType
from models.db import UserDB, WorkerStrikeDB, WorkerCertificationDB

logger = structlog.get_logger()
router = APIRouter(tags=["reputation"])


# ─── Schemas ──────────────────────────────────────────────────────────────────

class StrikeOut(BaseModel):
    id: UUID
    severity: str
    reason: str
    is_active: bool
    expires_at: Optional[datetime]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReputationOut(BaseModel):
    user_id: UUID
    reputation_score: float
    tier: str
    strike_count: int
    active_strikes: list[StrikeOut]
    is_banned: bool
    ban_reason: Optional[str]
    ban_expires_at: Optional[datetime]
    # Component breakdown
    accuracy: Optional[float]
    reliability: Optional[float]
    tasks_completed: int
    level: int
    streak_days: int


class IssueStrikeRequest(BaseModel):
    severity: str = Field(..., pattern="^(warning|minor|major|critical)$")
    reason: str = Field(..., min_length=5, max_length=500)
    expires_at: Optional[datetime] = None


class BanWorkerRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)
    expires_at: Optional[datetime] = None  # None = permanent


class AdminWorkerOut(BaseModel):
    id: UUID
    email: str
    name: Optional[str]
    reputation_score: float
    tier: str
    strike_count: int
    is_banned: bool
    tasks_completed: int
    level: int
    worker_accuracy: Optional[float]
    worker_reliability: Optional[float]
    created_at: datetime


# ─── Helper ───────────────────────────────────────────────────────────────────

async def _get_worker_or_404(worker_id: UUID, db: AsyncSession) -> UserDB:
    result = await db.execute(select(UserDB).where(UserDB.id == worker_id))
    worker = result.scalar_one_or_none()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    if worker.role not in ("worker", "both"):
        raise HTTPException(status_code=400, detail="User is not a worker")
    return worker


# ─── Worker: own reputation ───────────────────────────────────────────────────

@router.get("/v1/reputation/me", response_model=ReputationOut)
async def get_my_reputation(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get the current worker's reputation details."""
    result = await db.execute(select(UserDB).where(UserDB.id == user_id))
    me = result.scalar_one_or_none()
    if not me:
        raise HTTPException(status_code=404, detail="User not found")

    # Fetch active strikes
    strikes_result = await db.execute(
        select(WorkerStrikeDB).where(
            and_(
                WorkerStrikeDB.worker_id == user_id,
                WorkerStrikeDB.is_active == True,  # noqa: E712
            )
        ).order_by(WorkerStrikeDB.created_at.desc())
    )
    active_strikes = strikes_result.scalars().all()

    return ReputationOut(
        user_id=me.id,
        reputation_score=me.reputation_score,
        tier=reputation_tier(me.reputation_score),
        strike_count=me.strike_count,
        active_strikes=[StrikeOut.model_validate(s) for s in active_strikes],
        is_banned=me.is_banned,
        ban_reason=me.ban_reason,
        ban_expires_at=me.ban_expires_at,
        accuracy=me.worker_accuracy,
        reliability=me.worker_reliability,
        tasks_completed=me.worker_tasks_completed,
        level=me.worker_level,
        streak_days=me.worker_streak_days,
    )


# ─── Admin: list workers with reputation ─────────────────────────────────────

@router.get("/v1/admin/reputation/workers", response_model=list[AdminWorkerOut])
async def list_workers_reputation(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    sort_by: str = Query("reputation_score", pattern="^(reputation_score|tasks_completed|strike_count|created_at)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    is_banned: Optional[bool] = Query(None),
    min_score: Optional[float] = Query(None),
    max_score: Optional[float] = Query(None),
    search: Optional[str] = Query(None, max_length=200),
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin: list all workers with reputation info, sorted and filtered."""
    q = select(UserDB).where(UserDB.role.in_(["worker", "both"]))
    if is_banned is not None:
        q = q.where(UserDB.is_banned == is_banned)
    if min_score is not None:
        q = q.where(UserDB.reputation_score >= min_score)
    if max_score is not None:
        q = q.where(UserDB.reputation_score <= max_score)
    if search:
        term = f"%{esc_like(search)}%"
        q = q.where(
            UserDB.email.ilike(term, escape=LIKE_ESC) | UserDB.name.ilike(term, escape=LIKE_ESC)
        )

    col = getattr(UserDB, sort_by, UserDB.reputation_score)
    q = q.order_by(col.desc() if order == "desc" else col.asc())
    q = q.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(q)
    workers = result.scalars().all()

    return [
        AdminWorkerOut(
            id=w.id,
            email=w.email,
            name=w.name,
            reputation_score=w.reputation_score,
            tier=reputation_tier(w.reputation_score),
            strike_count=w.strike_count,
            is_banned=w.is_banned,
            tasks_completed=w.worker_tasks_completed,
            level=w.worker_level,
            worker_accuracy=w.worker_accuracy,
            worker_reliability=w.worker_reliability,
            created_at=w.created_at,
        )
        for w in workers
    ]


# ─── Admin: get worker reputation detail ─────────────────────────────────────

@router.get("/v1/admin/reputation/workers/{worker_id}", response_model=ReputationOut)
async def get_worker_reputation(
    worker_id: UUID,
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    worker = await _get_worker_or_404(worker_id, db)
    strikes_result = await db.execute(
        select(WorkerStrikeDB)
        .where(WorkerStrikeDB.worker_id == worker_id)
        .order_by(WorkerStrikeDB.created_at.desc())
    )
    all_strikes = strikes_result.scalars().all()
    active = [s for s in all_strikes if s.is_active]

    return ReputationOut(
        user_id=worker.id,
        reputation_score=worker.reputation_score,
        tier=reputation_tier(worker.reputation_score),
        strike_count=worker.strike_count,
        active_strikes=[StrikeOut.model_validate(s) for s in active],
        is_banned=worker.is_banned,
        ban_reason=worker.ban_reason,
        ban_expires_at=worker.ban_expires_at,
        accuracy=worker.worker_accuracy,
        reliability=worker.worker_reliability,
        tasks_completed=worker.worker_tasks_completed,
        level=worker.worker_level,
        streak_days=worker.worker_streak_days,
    )


# ─── Admin: issue strike ─────────────────────────────────────────────────────

@router.post("/v1/admin/reputation/workers/{worker_id}/strikes", status_code=201)
async def issue_strike(
    worker_id: UUID,
    body: IssueStrikeRequest,
    admin_id: UUID = Depends(get_current_user_id),
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Issue a moderation strike to a worker."""
    worker = await _get_worker_or_404(worker_id, db)

    strike = WorkerStrikeDB(
        worker_id=worker_id,
        issued_by=admin_id,
        severity=body.severity,
        reason=body.reason,
        expires_at=body.expires_at,
    )
    db.add(strike)
    worker.strike_count = (worker.strike_count or 0) + 1

    # Recompute reputation
    await db.flush()
    new_score = await refresh_worker_reputation(worker_id, db)

    # Notify worker
    await create_notification(
        db=db,
        user_id=worker_id,
        type=NotifType.TASK_FAILED,  # Closest available type
        title="⚠️ Moderation Strike Issued",
        body=f"You received a {body.severity} strike: {body.reason}",
        link="/worker/reputation",
    )

    await db.commit()

    logger.info("strike.issued", worker_id=str(worker_id), severity=body.severity,
                admin_id=str(admin_id), new_score=new_score)
    return {"message": "Strike issued", "new_reputation_score": new_score}


# ─── Admin: pardon strike ─────────────────────────────────────────────────────

@router.delete("/v1/admin/reputation/strikes/{strike_id}")
async def pardon_strike(
    strike_id: UUID,
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Pardon (deactivate) a specific strike."""
    result = await db.execute(select(WorkerStrikeDB).where(WorkerStrikeDB.id == strike_id))
    strike = result.scalar_one_or_none()
    if not strike:
        raise HTTPException(status_code=404, detail="Strike not found")

    strike.is_active = False

    # Reduce strike count on worker
    worker_result = await db.execute(select(UserDB).where(UserDB.id == strike.worker_id))
    worker = worker_result.scalar_one_or_none()
    if worker:
        worker.strike_count = max(0, (worker.strike_count or 1) - 1)
        await db.flush()
        new_score = await refresh_worker_reputation(strike.worker_id, db)
    else:
        new_score = None

    await db.commit()
    return {"message": "Strike pardoned", "new_reputation_score": new_score}


# ─── Admin: ban worker ────────────────────────────────────────────────────────

@router.post("/v1/admin/reputation/workers/{worker_id}/ban")
async def ban_worker(
    worker_id: UUID,
    body: BanWorkerRequest,
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Ban a worker from the platform (temporarily or permanently)."""
    worker = await _get_worker_or_404(worker_id, db)
    if worker.is_banned:
        raise HTTPException(status_code=400, detail="Worker is already banned")

    worker.is_banned = True
    worker.ban_reason = body.reason
    worker.ban_expires_at = body.expires_at
    worker.reputation_score = 0.0

    await create_notification(
        db=db,
        user_id=worker_id,
        type=NotifType.TASK_FAILED,
        title="🚫 Account Suspended",
        body=f"Your worker account has been suspended: {body.reason}",
    )

    await db.commit()
    logger.info("worker.banned", worker_id=str(worker_id), reason=body.reason,
                expires_at=str(body.expires_at))
    return {"message": "Worker banned"}


# ─── Admin: unban worker ──────────────────────────────────────────────────────

@router.post("/v1/admin/reputation/workers/{worker_id}/unban")
async def unban_worker(
    worker_id: UUID,
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Lift a ban from a worker."""
    worker = await _get_worker_or_404(worker_id, db)
    if not worker.is_banned:
        raise HTTPException(status_code=400, detail="Worker is not banned")

    worker.is_banned = False
    worker.ban_reason = None
    worker.ban_expires_at = None

    # Recompute reputation on unban
    await db.flush()
    new_score = await refresh_worker_reputation(worker_id, db)

    await create_notification(
        db=db,
        user_id=worker_id,
        type=NotifType.TASK_COMPLETED,
        title="✅ Account Reinstated",
        body="Your worker account suspension has been lifted.",
        link="/worker/marketplace",
    )

    await db.commit()
    logger.info("worker.unbanned", worker_id=str(worker_id), new_score=new_score)
    return {"message": "Worker unbanned", "new_reputation_score": new_score}


# ─── Admin: recalculate all reputations ──────────────────────────────────────

@router.post("/v1/admin/reputation/recalculate")
async def recalculate_all_reputations(
    _admin=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Recalculate reputation scores for ALL workers. Admin only."""
    result = await db.execute(
        select(UserDB).where(UserDB.role.in_(["worker", "both"]))
    )
    workers = result.scalars().all()
    if not workers:
        return {"message": "Recalculated 0 worker reputations"}

    worker_ids = [w.id for w in workers]

    # Bulk-fetch cert counts for all workers in a single GROUP BY query
    cert_res = await db.execute(
        select(WorkerCertificationDB.worker_id, func.count().label("cnt"))
        .where(
            WorkerCertificationDB.worker_id.in_(worker_ids),
            WorkerCertificationDB.passed == True,  # noqa: E712
        )
        .group_by(WorkerCertificationDB.worker_id)
    )
    cert_counts: dict = {str(r.worker_id): r.cnt for r in cert_res}

    # Bulk-fetch active strike severities grouped by worker
    strike_res = await db.execute(
        select(WorkerStrikeDB.worker_id, WorkerStrikeDB.severity)
        .where(
            WorkerStrikeDB.worker_id.in_(worker_ids),
            WorkerStrikeDB.is_active == True,  # noqa: E712
        )
    )
    strikes_by_worker: dict[str, list[str]] = {}
    for row in strike_res:
        strikes_by_worker.setdefault(str(row.worker_id), []).append(row.severity)

    updated = 0
    for w in workers:
        wid = str(w.id)
        score = await compute_reputation(
            w,
            db,
            _cert_count=cert_counts.get(wid, 0),
            _strike_severities=strikes_by_worker.get(wid, []),
        )
        w.reputation_score = score
        updated += 1
    await db.commit()
    return {"message": f"Recalculated {updated} worker reputations"}
