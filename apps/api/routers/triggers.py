"""Pipeline Triggers — schedule-based and webhook-triggered pipeline runs.

Two trigger types:
- schedule: fires the pipeline on a cron expression (e.g. "0 9 * * 1" = every Monday 9am UTC)
- webhook: fires the pipeline when a secret URL is hit via HTTP POST

Background polling is integrated with the sweeper (runs every 60s, checks due schedule triggers).
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.auth import get_current_user_id
from core.database import get_db
from models.db import PipelineTriggerDB, TaskPipelineDB, TaskPipelineStepDB, UserDB

logger = structlog.get_logger()
router = APIRouter(tags=["triggers"])

# ─── Schemas ──────────────────────────────────────────────────────────────────

class TriggerCreateRequest(BaseModel):
    trigger_type: str = Field(..., pattern="^(schedule|webhook)$")
    name: Optional[str] = Field(None, max_length=255)
    cron_expression: Optional[str] = Field(
        None,
        description="Cron expression (for schedule triggers). E.g. '0 9 * * 1' = Mon 9am UTC",
    )
    default_input: Optional[dict] = None


class TriggerOut(BaseModel):
    id: UUID
    pipeline_id: UUID
    trigger_type: str
    name: Optional[str]
    is_active: bool
    cron_expression: Optional[str]
    webhook_token: Optional[str]
    webhook_url: Optional[str]
    default_input: Optional[dict]
    last_fired_at: Optional[datetime]
    next_fire_at: Optional[datetime]
    run_count: int
    created_at: datetime


class TriggerUpdateRequest(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None
    cron_expression: Optional[str] = None
    default_input: Optional[dict] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _compute_next_fire(cron_expr: str, after: Optional[datetime] = None) -> Optional[datetime]:
    """Return the next fire datetime for a cron expression."""
    try:
        from croniter import croniter  # type: ignore
        base = after or datetime.now(timezone.utc)
        it = croniter(cron_expr, base)
        nxt = it.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        return nxt
    except Exception:
        return None


def _trigger_to_out(trigger: PipelineTriggerDB, base_url: str = "") -> TriggerOut:
    webhook_url = None
    if trigger.trigger_type == "webhook" and trigger.webhook_token:
        webhook_url = f"{base_url}/v1/pipelines/webhooks/{trigger.webhook_token}"
    return TriggerOut(
        id=trigger.id,
        pipeline_id=trigger.pipeline_id,
        trigger_type=trigger.trigger_type,
        name=trigger.name,
        is_active=trigger.is_active,
        cron_expression=trigger.cron_expression,
        webhook_token=trigger.webhook_token if trigger.trigger_type == "webhook" else None,
        webhook_url=webhook_url,
        default_input=trigger.default_input,
        last_fired_at=trigger.last_fired_at,
        next_fire_at=trigger.next_fire_at,
        run_count=trigger.run_count,
        created_at=trigger.created_at,
    )


async def _fire_trigger(trigger: PipelineTriggerDB, db: AsyncSession,
                         override_input: Optional[dict] = None) -> dict:
    """Execute a pipeline run for a trigger. Returns a summary dict."""
    from routers.pipelines import _execute_pipeline_run, _get_pipeline
    from models.db import TaskPipelineRunDB, TaskPipelineStepDB, TaskPipelineStepRunDB
    from models.schemas import PipelineRunRequest as _PRR
    from core.quotas import enforce_pipeline_run_quota, record_pipeline_run

    run_input = dict(trigger.default_input or {})
    if override_input:
        run_input.update(override_input)

    # Load the pipeline + user
    pipeline_result = await db.execute(
        select(TaskPipelineDB).where(TaskPipelineDB.id == trigger.pipeline_id)
    )
    pipeline = pipeline_result.scalar_one_or_none()
    if not pipeline:
        raise ValueError(f"Pipeline {trigger.pipeline_id} not found")

    user_result = await db.execute(select(UserDB).where(UserDB.id == trigger.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise ValueError(f"Trigger owner {trigger.user_id} not found")

    # Load steps
    steps_result = await db.execute(
        select(TaskPipelineStepDB)
        .where(TaskPipelineStepDB.pipeline_id == pipeline.id)
        .order_by(TaskPipelineStepDB.step_order)
    )
    steps = steps_result.scalars().all()
    if not steps:
        raise ValueError("Pipeline has no steps")

    # Create run record
    now = datetime.now(timezone.utc)
    run = TaskPipelineRunDB(
        pipeline_id=pipeline.id,
        user_id=trigger.user_id,
        status="running",
        input=run_input,
        current_step=0,
        created_at=now,
        started_at=now,
    )
    db.add(run)
    await db.flush()

    for step in steps:
        sr = TaskPipelineStepRunDB(
            run_id=run.id,
            step_id=step.id,
            step_order=step.step_order,
            status="pending",
        )
        db.add(sr)

    await db.flush()

    try:
        await record_pipeline_run(db, str(trigger.user_id))
    except Exception:
        pass  # quota recording failure shouldn't block trigger

    # Fire pipeline execution as asyncio task (non-blocking)
    asyncio.create_task(_execute_pipeline_run(run.id, str(pipeline.id), str(trigger.user_id)))

    trigger.last_fired_at = now
    trigger.run_count = (trigger.run_count or 0) + 1
    if trigger.trigger_type == "schedule" and trigger.cron_expression:
        trigger.next_fire_at = _compute_next_fire(trigger.cron_expression)

    return {"run_id": str(run.id), "status": "running"}


# ─── Create trigger ───────────────────────────────────────────────────────────

@router.post("/v1/pipelines/{pipeline_id}/triggers", response_model=TriggerOut, status_code=201)
async def create_trigger(
    pipeline_id: UUID,
    body: TriggerCreateRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Create a schedule or webhook trigger for a pipeline."""
    # Verify pipeline ownership
    result = await db.execute(
        select(TaskPipelineDB).where(
            TaskPipelineDB.id == pipeline_id,
            TaskPipelineDB.user_id == user_id,
        )
    )
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    if body.trigger_type == "schedule":
        if not body.cron_expression:
            raise HTTPException(status_code=422, detail="cron_expression is required for schedule triggers")
        # Validate cron expression
        try:
            from croniter import croniter  # type: ignore
            if not croniter.is_valid(body.cron_expression):
                raise ValueError("invalid")
        except Exception:
            raise HTTPException(status_code=422, detail=f"Invalid cron expression: '{body.cron_expression}'")

    webhook_token = None
    if body.trigger_type == "webhook":
        webhook_token = secrets.token_urlsafe(32)

    next_fire = None
    if body.trigger_type == "schedule" and body.cron_expression:
        next_fire = _compute_next_fire(body.cron_expression)

    trigger = PipelineTriggerDB(
        pipeline_id=pipeline_id,
        user_id=user_id,
        trigger_type=body.trigger_type,
        name=body.name,
        cron_expression=body.cron_expression,
        webhook_token=webhook_token,
        default_input=body.default_input,
        next_fire_at=next_fire,
    )
    db.add(trigger)
    await db.commit()
    await db.refresh(trigger)

    base_url = str(request.base_url).rstrip("/")
    logger.info("trigger.created", trigger_id=str(trigger.id), type=body.trigger_type,
                pipeline_id=str(pipeline_id))
    return _trigger_to_out(trigger, base_url)


# ─── List triggers ────────────────────────────────────────────────────────────

@router.get("/v1/pipelines/{pipeline_id}/triggers", response_model=list[TriggerOut])
async def list_triggers(
    pipeline_id: UUID,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """List all triggers for a pipeline."""
    result = await db.execute(
        select(TaskPipelineDB).where(
            TaskPipelineDB.id == pipeline_id,
            TaskPipelineDB.user_id == user_id,
        )
    )
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    triggers_result = await db.execute(
        select(PipelineTriggerDB)
        .where(PipelineTriggerDB.pipeline_id == pipeline_id)
        .order_by(PipelineTriggerDB.created_at.asc())
    )
    triggers = triggers_result.scalars().all()
    base_url = str(request.base_url).rstrip("/")
    return [_trigger_to_out(t, base_url) for t in triggers]


# ─── Get trigger ──────────────────────────────────────────────────────────────

@router.get("/v1/pipelines/triggers/{trigger_id}", response_model=TriggerOut)
async def get_trigger(
    trigger_id: UUID,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PipelineTriggerDB).where(PipelineTriggerDB.id == trigger_id)
    )
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
    if str(trigger.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Not your trigger")
    base_url = str(request.base_url).rstrip("/")
    return _trigger_to_out(trigger, base_url)


# ─── Update trigger ───────────────────────────────────────────────────────────

@router.patch("/v1/pipelines/triggers/{trigger_id}", response_model=TriggerOut)
async def update_trigger(
    trigger_id: UUID,
    body: TriggerUpdateRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PipelineTriggerDB).where(PipelineTriggerDB.id == trigger_id)
    )
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
    if str(trigger.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Not your trigger")

    if body.name is not None:
        trigger.name = body.name
    if body.is_active is not None:
        trigger.is_active = body.is_active
    if body.default_input is not None:
        trigger.default_input = body.default_input
    if body.cron_expression is not None:
        if trigger.trigger_type != "schedule":
            raise HTTPException(status_code=422, detail="cron_expression only applies to schedule triggers")
        try:
            from croniter import croniter  # type: ignore
            if not croniter.is_valid(body.cron_expression):
                raise ValueError("invalid")
        except Exception:
            raise HTTPException(status_code=422, detail=f"Invalid cron expression: '{body.cron_expression}'")
        trigger.cron_expression = body.cron_expression
        trigger.next_fire_at = _compute_next_fire(body.cron_expression)

    await db.commit()
    await db.refresh(trigger)
    base_url = str(request.base_url).rstrip("/")
    return _trigger_to_out(trigger, base_url)


# ─── Delete trigger ───────────────────────────────────────────────────────────

@router.delete("/v1/pipelines/triggers/{trigger_id}", status_code=204)
async def delete_trigger(
    trigger_id: UUID,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PipelineTriggerDB).where(PipelineTriggerDB.id == trigger_id)
    )
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
    if str(trigger.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Not your trigger")
    await db.delete(trigger)
    await db.commit()


# ─── Webhook fire endpoint ────────────────────────────────────────────────────

@router.post("/v1/pipelines/webhooks/{token}")
async def fire_webhook_trigger(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Public endpoint — POST here to fire a webhook-type pipeline trigger.

    The request body (JSON) is merged into the pipeline's default_input.
    """
    result = await db.execute(
        select(PipelineTriggerDB).where(
            PipelineTriggerDB.webhook_token == token,
            PipelineTriggerDB.trigger_type == "webhook",
            PipelineTriggerDB.is_active == True,  # noqa: E712
        )
    )
    trigger = result.scalar_one_or_none()
    if not trigger:
        # Return 404 to avoid leaking token existence timing
        raise HTTPException(status_code=404, detail="Trigger not found")

    # Parse optional JSON body
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}

    try:
        summary = await _fire_trigger(trigger, db, override_input=body)
        await db.commit()
        logger.info("trigger.webhook_fired", trigger_id=str(trigger.id), run_id=summary.get("run_id"))
        return {"triggered": True, "run_id": summary.get("run_id")}
    except Exception as exc:
        logger.exception("trigger.webhook_error", trigger_id=str(trigger.id))
        raise HTTPException(status_code=500, detail=f"Trigger execution failed: {exc}")


# ─── Manual fire ─────────────────────────────────────────────────────────────

@router.post("/v1/pipelines/triggers/{trigger_id}/fire")
async def manually_fire_trigger(
    trigger_id: UUID,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Manually fire a trigger (runs the pipeline immediately). Owner only."""
    result = await db.execute(
        select(PipelineTriggerDB).where(PipelineTriggerDB.id == trigger_id)
    )
    trigger = result.scalar_one_or_none()
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
    if str(trigger.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Not your trigger")

    try:
        summary = await _fire_trigger(trigger, db)
        await db.commit()
        return {"triggered": True, "run_id": summary.get("run_id")}
    except Exception as exc:
        logger.exception("trigger.manual_fire_error", trigger_id=str(trigger_id))
        raise HTTPException(status_code=500, detail=f"Trigger execution failed: {exc}")


# ─── Background scheduler (called from sweeper) ───────────────────────────────

async def run_due_schedule_triggers(session_factory: async_sessionmaker) -> int:
    """Find and fire all schedule triggers whose next_fire_at is in the past.

    Returns the number of triggers fired.
    """
    now = datetime.now(timezone.utc)
    fired = 0

    async with session_factory() as db:
        result = await db.execute(
            select(PipelineTriggerDB).where(
                and_(
                    PipelineTriggerDB.trigger_type == "schedule",
                    PipelineTriggerDB.is_active == True,  # noqa: E712
                    PipelineTriggerDB.next_fire_at != None,  # noqa: E711
                    PipelineTriggerDB.next_fire_at <= now,
                )
            )
        )
        due_triggers = result.scalars().all()

        for trigger in due_triggers:
            try:
                await _fire_trigger(trigger, db)
                fired += 1
                logger.info("trigger.schedule_fired", trigger_id=str(trigger.id),
                            pipeline_id=str(trigger.pipeline_id))
            except Exception:
                logger.exception("trigger.schedule_fire_error", trigger_id=str(trigger.id))
                # Still update next_fire_at so it doesn't fire in a tight loop
                if trigger.cron_expression:
                    trigger.next_fire_at = _compute_next_fire(trigger.cron_expression)

        if due_triggers:
            await db.commit()

    return fired
