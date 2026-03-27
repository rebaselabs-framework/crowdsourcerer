"""A/B Testing Framework — create experiments, assign tasks, view results."""
from __future__ import annotations
import math
import random
import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from models.db import ABExperimentDB, ABVariantDB, ABParticipantDB, TaskDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/experiments", tags=["experiments"])


# ─── Schemas ──────────────────────────────────────────────────────────────────

class VariantCreate(BaseModel):
    name: str = Field(max_length=64)
    description: Optional[str] = None
    traffic_pct: float = Field(default=50.0, ge=0.0, le=100.0)
    task_config: Optional[dict] = None
    is_control: bool = False


class ExperimentCreate(BaseModel):
    name: str = Field(max_length=255)
    description: Optional[str] = None
    hypothesis: Optional[str] = None
    task_type: Optional[str] = None
    primary_metric: str = "completion_rate"
    variants: list[VariantCreate] = Field(default_factory=list, min_length=2, max_length=5)


class VariantOut(BaseModel):
    id: UUID
    experiment_id: UUID
    name: str
    description: Optional[str]
    traffic_pct: float
    task_config: Optional[dict]
    is_control: bool
    participant_count: int
    completion_count: int
    failure_count: int
    # Computed stats
    completion_rate: float
    avg_accuracy: Optional[float]
    avg_duration_ms: Optional[float]
    avg_credits_used: Optional[float]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ExperimentOut(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    description: Optional[str]
    hypothesis: Optional[str]
    status: str
    task_type: Optional[str]
    primary_metric: str
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    winner_variant_id: Optional[UUID]
    variants: list[VariantOut]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EnrollTaskRequest(BaseModel):
    task_id: UUID
    experiment_id: UUID


class EnrollTaskResponse(BaseModel):
    participant_id: UUID
    variant_id: UUID
    variant_name: str
    task_config: Optional[dict]


class ExperimentResultsOut(BaseModel):
    experiment_id: UUID
    name: str
    status: str
    primary_metric: str
    total_participants: int
    variants: list[VariantOut]
    statistical_significance: Optional[float]   # p-value (chi-squared on completion rate)
    winner_variant_id: Optional[UUID]
    recommendation: str


def _variant_stats(v: ABVariantDB) -> VariantOut:
    """Compute derived stats for a variant."""
    n = v.participant_count or 1
    c = v.completion_count
    return VariantOut(
        id=v.id,
        experiment_id=v.experiment_id,
        name=v.name,
        description=v.description,
        traffic_pct=v.traffic_pct,
        task_config=v.task_config,
        is_control=v.is_control,
        participant_count=v.participant_count,
        completion_count=c,
        failure_count=v.failure_count,
        completion_rate=round(c / n * 100, 2),
        avg_accuracy=round(v.total_accuracy / c, 3) if c > 0 else None,
        avg_duration_ms=round(v.total_duration_ms / c, 1) if c > 0 else None,
        avg_credits_used=round(v.total_credits_used / c, 2) if c > 0 else None,
        created_at=v.created_at,
    )


def _chi_squared_p(observed: list[tuple[int, int]]) -> Optional[float]:
    """Simplified chi-squared p-value for 2-variant completion rates.
    Returns None if insufficient data (<5 completions per variant).
    """
    if len(observed) != 2:
        return None
    (c1, n1), (c2, n2) = observed
    if min(c1, c2, n1 - c1, n2 - c2) < 5:
        return None
    total = n1 + n2
    total_complete = c1 + c2
    total_fail = total - total_complete
    if total_complete == 0 or total_fail == 0:
        return None
    e11 = n1 * total_complete / total
    e12 = n1 * total_fail / total
    e21 = n2 * total_complete / total
    e22 = n2 * total_fail / total
    chi2 = ((c1 - e11) ** 2 / e11 + (n1 - c1 - e12) ** 2 / e12 +
            (c2 - e21) ** 2 / e21 + (n2 - c2 - e22) ** 2 / e22)
    # Approximate p-value from chi2 with 1 df
    # Using incomplete gamma (erfc approximation)
    p = math.erfc(math.sqrt(chi2 / 2))
    return round(p, 4)


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("", response_model=ExperimentOut, status_code=201)
async def create_experiment(
    req: ExperimentCreate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Create a new A/B experiment with variants."""
    if abs(sum(v.traffic_pct for v in req.variants) - 100.0) > 0.5:
        raise HTTPException(400, "Variant traffic_pct values must sum to 100")

    exp = ABExperimentDB(
        id=_uuid.uuid4(),
        user_id=UUID(user_id),
        name=req.name,
        description=req.description,
        hypothesis=req.hypothesis,
        task_type=req.task_type,
        primary_metric=req.primary_metric,
        status="draft",
    )
    db.add(exp)
    await db.flush()  # get exp.id

    for v in req.variants:
        db.add(ABVariantDB(
            id=_uuid.uuid4(),
            experiment_id=exp.id,
            name=v.name,
            description=v.description,
            traffic_pct=v.traffic_pct,
            task_config=v.task_config,
            is_control=v.is_control,
        ))

    await db.commit()
    await db.refresh(exp)

    # Load variants
    res = await db.execute(select(ABVariantDB).where(ABVariantDB.experiment_id == exp.id))
    exp.variants = list(res.scalars().all())
    return _experiment_out(exp)


@router.get("", response_model=list[ExperimentOut])
async def list_experiments(
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    q = select(ABExperimentDB).where(ABExperimentDB.user_id == UUID(user_id))
    if status:
        q = q.where(ABExperimentDB.status == status)
    q = q.order_by(ABExperimentDB.created_at.desc()).limit(200)
    res = await db.execute(q)
    exps = list(res.scalars().all())
    if not exps:
        return []

    # Bulk-load all variants for all experiments in a single query
    exp_ids = [e.id for e in exps]
    vres = await db.execute(
        select(ABVariantDB).where(ABVariantDB.experiment_id.in_(exp_ids))
    )
    variants_by_exp: dict[str, list[ABVariantDB]] = {}
    for v in vres.scalars():
        variants_by_exp.setdefault(str(v.experiment_id), []).append(v)

    out = []
    for exp in exps:
        exp.variants = variants_by_exp.get(str(exp.id), [])
        out.append(_experiment_out(exp))
    return out


@router.get("/{experiment_id}", response_model=ExperimentOut)
async def get_experiment(
    experiment_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    exp = await _get_exp(experiment_id, user_id, db)
    vres = await db.execute(select(ABVariantDB).where(ABVariantDB.experiment_id == exp.id))
    exp.variants = list(vres.scalars().all())
    return _experiment_out(exp)


@router.patch("/{experiment_id}/status")
async def update_status(
    experiment_id: UUID,
    status: str = Query(..., pattern="^(running|paused|completed)$"),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Start, pause, or complete an experiment."""
    exp = await _get_exp(experiment_id, user_id, db)
    now = datetime.now(timezone.utc)
    exp.status = status
    if status == "running" and exp.started_at is None:
        exp.started_at = now
    if status == "completed":
        exp.ended_at = now
    await db.commit()
    return {"status": status}


@router.delete("/{experiment_id}", status_code=204, response_model=None)
async def delete_experiment(
    experiment_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    exp = await _get_exp(experiment_id, user_id, db)
    await db.delete(exp)
    await db.commit()


@router.post("/{experiment_id}/enroll", response_model=EnrollTaskResponse)
async def enroll_task(
    experiment_id: UUID,
    req: EnrollTaskRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Enroll a task in an experiment — assigns it to a variant by traffic weighting."""
    exp = await _get_exp(experiment_id, user_id, db)
    if exp.status != "running":
        raise HTTPException(400, "Experiment must be running to enroll tasks")

    # Verify the task being enrolled belongs to this user — prevents enrolling
    # other requesters' tasks into our experiment (IDOR / info-disclosure).
    task_check = await db.execute(
        select(TaskDB).where(TaskDB.id == req.task_id, TaskDB.user_id == UUID(user_id))
    )
    if not task_check.scalar_one_or_none():
        raise HTTPException(404, "Task not found or does not belong to you")

    # Check task not already enrolled
    exist = await db.execute(
        select(ABParticipantDB).where(ABParticipantDB.task_id == req.task_id)
    )
    if exist.scalar_one_or_none():
        raise HTTPException(409, "Task already enrolled in an experiment")

    # Load variants and pick by weighted random
    vres = await db.execute(select(ABVariantDB).where(ABVariantDB.experiment_id == exp.id))
    variants = list(vres.scalars().all())
    if not variants:
        raise HTTPException(400, "Experiment has no variants")

    chosen = _weighted_choice(variants)

    # Create participant record
    p = ABParticipantDB(
        id=_uuid.uuid4(),
        experiment_id=exp.id,
        variant_id=chosen.id,
        task_id=req.task_id,
        user_id=UUID(user_id),
    )
    db.add(p)
    chosen.participant_count = (chosen.participant_count or 0) + 1
    await db.commit()

    return EnrollTaskResponse(
        participant_id=p.id,
        variant_id=chosen.id,
        variant_name=chosen.name,
        task_config=chosen.task_config,
    )


@router.get("/{experiment_id}/results", response_model=ExperimentResultsOut)
async def get_results(
    experiment_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get A/B experiment results with statistical analysis."""
    exp = await _get_exp(experiment_id, user_id, db)
    vres = await db.execute(select(ABVariantDB).where(ABVariantDB.experiment_id == exp.id))
    variants = list(vres.scalars().all())
    exp.variants = variants

    variant_outs = [_variant_stats(v) for v in variants]
    total_participants = sum(v.participant_count for v in variants)

    # Statistical significance (only for 2-variant experiments)
    p_value = None
    if len(variants) == 2:
        p_value = _chi_squared_p([
            (v.completion_count, v.participant_count) for v in variants
        ])

    # Determine winner by primary metric
    winner_id = _pick_winner(variants, exp.primary_metric)
    recommendation = _make_recommendation(variants, exp.primary_metric, p_value, winner_id)

    return ExperimentResultsOut(
        experiment_id=exp.id,
        name=exp.name,
        status=exp.status,
        primary_metric=exp.primary_metric,
        total_participants=total_participants,
        variants=variant_outs,
        statistical_significance=p_value,
        winner_variant_id=winner_id,
        recommendation=recommendation,
    )


@router.post("/{experiment_id}/record-outcome")
async def record_outcome(
    experiment_id: UUID,
    task_id: UUID = Query(...),
    outcome: str = Query(..., pattern="^(completed|failed|cancelled)$"),
    accuracy: Optional[float] = Query(None),
    duration_ms: Optional[int] = Query(None),
    credits_used: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Record the outcome of a task enrolled in the experiment (called internally).

    Requires experiment ownership — only the requester who owns the experiment
    can record outcomes for it. Without this check any authenticated user could
    tamper with another user's experiment statistics.
    """
    # Verify the caller owns this experiment before allowing stat modification.
    await _get_exp(experiment_id, user_id, db)

    res = await db.execute(
        select(ABParticipantDB).where(
            ABParticipantDB.experiment_id == experiment_id,
            ABParticipantDB.task_id == task_id,
        )
    )
    participant = res.scalar_one_or_none()
    if not participant:
        raise HTTPException(404, "Participant not found")

    now = datetime.now(timezone.utc)
    participant.completed_at = now
    participant.outcome = outcome
    participant.accuracy = accuracy
    participant.duration_ms = duration_ms
    participant.credits_used = credits_used

    # Update rolling stats on variant
    vres = await db.execute(
        select(ABVariantDB).where(ABVariantDB.id == participant.variant_id)
    )
    variant = vres.scalar_one_or_none()
    if variant:
        if outcome == "completed":
            variant.completion_count = (variant.completion_count or 0) + 1
            if accuracy is not None:
                variant.total_accuracy = (variant.total_accuracy or 0.0) + accuracy
            if duration_ms is not None:
                variant.total_duration_ms = (variant.total_duration_ms or 0) + duration_ms
            if credits_used is not None:
                variant.total_credits_used = (variant.total_credits_used or 0) + credits_used
        elif outcome == "failed":
            variant.failure_count = (variant.failure_count or 0) + 1

    await db.commit()
    return {"status": "recorded"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _get_exp(experiment_id: UUID, user_id: str, db: AsyncSession) -> ABExperimentDB:
    res = await db.execute(
        select(ABExperimentDB).where(
            ABExperimentDB.id == experiment_id,
            ABExperimentDB.user_id == UUID(user_id),
        )
    )
    exp = res.scalar_one_or_none()
    if not exp:
        raise HTTPException(404, "Experiment not found")
    return exp


def _experiment_out(exp: ABExperimentDB) -> ExperimentOut:
    return ExperimentOut(
        id=exp.id,
        user_id=exp.user_id,
        name=exp.name,
        description=exp.description,
        hypothesis=exp.hypothesis,
        status=exp.status,
        task_type=exp.task_type,
        primary_metric=exp.primary_metric,
        started_at=exp.started_at,
        ended_at=exp.ended_at,
        winner_variant_id=exp.winner_variant_id,
        variants=[_variant_stats(v) for v in exp.variants],
        created_at=exp.created_at,
        updated_at=exp.updated_at,
    )


def _weighted_choice(variants: list[ABVariantDB]) -> ABVariantDB:
    """Pick a variant by traffic_pct weighting."""
    total = sum(v.traffic_pct for v in variants) or 100.0
    weights = [v.traffic_pct / total for v in variants]
    return random.choices(variants, weights=weights, k=1)[0]


def _pick_winner(variants: list[ABVariantDB], metric: str) -> Optional[UUID]:
    """Pick the best variant by the primary metric."""
    if not variants or all(v.participant_count == 0 for v in variants):
        return None
    if metric == "completion_rate":
        best = max(variants, key=lambda v: v.completion_count / max(v.participant_count, 1))
    elif metric == "accuracy":
        best = max(
            [v for v in variants if v.completion_count > 0],
            key=lambda v: v.total_accuracy / v.completion_count,
            default=None,
        )
    elif metric == "avg_time":
        completed = [v for v in variants if v.completion_count > 0]
        if not completed:
            return None
        best = min(completed, key=lambda v: v.total_duration_ms / v.completion_count)
    elif metric == "credits_used":
        completed = [v for v in variants if v.completion_count > 0]
        if not completed:
            return None
        best = min(completed, key=lambda v: v.total_credits_used / v.completion_count)
    else:
        return None
    return best.id if best else None


def _make_recommendation(variants: list[ABVariantDB], metric: str,
                         p_value: Optional[float], winner_id: Optional[UUID]) -> str:
    total = sum(v.participant_count for v in variants)
    if total < 20:
        return f"Insufficient data ({total} participants). Need at least 20 to draw conclusions."
    if winner_id is None:
        return "No clear winner yet."
    winner = next((v for v in variants if v.id == winner_id), None)
    if not winner:
        return "No winner determined."
    if p_value is not None and p_value < 0.05:
        return (f"'{winner.name}' is the statistically significant winner "
                f"(p={p_value}, metric: {metric}). Deploy with confidence.")
    elif p_value is not None:
        return (f"'{winner.name}' leads on {metric} but results are not yet "
                f"statistically significant (p={p_value}). Continue running.")
    else:
        return f"'{winner.name}' leads on {metric}. Run longer for statistical significance."
