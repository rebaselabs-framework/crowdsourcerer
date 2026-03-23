"""Task Pipeline endpoints — define and run multi-step task chains."""
from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from core.auth import get_current_user_id
from core.database import get_db
from models.db import (
    TaskPipelineDB, TaskPipelineStepDB, TaskPipelineRunDB,
    TaskPipelineStepRunDB, TaskDB, UserDB, CreditTransactionDB,
)
from models.schemas import (
    PipelineCreateRequest, PipelineOut, PipelineDetailOut,
    PipelineRunRequest, PipelineRunOut, PaginatedPipelines, PaginatedPipelineRuns,
)
from workers.base import get_rebasekit_client, WorkerError
from workers.router import execute_task, TASK_CREDITS

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/pipelines", tags=["pipelines"])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Input Mapping Resolution ───────────────────────────────────────────────────

def _resolve_input(
    mapping: Optional[dict],
    static_config: dict,
    pipeline_input: dict,
    step_outputs: list[Optional[dict]],
) -> dict:
    """Build the actual task input by merging static config with mapped values.

    Mapping keys use JSONPath-lite syntax:
        $.input.key        — from pipeline run's initial input
        $.steps.0.output   — from step index 0's full output
        $.steps.0.key      — from step index 0's output[key]
    """
    result = dict(static_config)  # start with static values

    if not mapping:
        # Auto-pass pipeline input + last step output as "prev"
        result.update(pipeline_input)
        if step_outputs:
            last = next((o for o in reversed(step_outputs) if o is not None), None)
            if last:
                result["prev"] = last
        return result

    for dest_key, src_path in mapping.items():
        value = _extract_path(src_path, pipeline_input, step_outputs)
        if value is not None:
            result[dest_key] = value

    return result


def _extract_path(path: str, pipeline_input: dict, step_outputs: list) -> Any:
    """Resolve a mapping path like '$.input.text' or '$.steps.1.result'."""
    if not path or not path.startswith("$"):
        return path  # literal value

    parts = path.lstrip("$.").split(".")
    if not parts:
        return None

    root = parts[0]
    rest = parts[1:]

    if root == "input":
        obj = pipeline_input
    elif root == "steps":
        if not rest:
            return None
        try:
            idx = int(rest[0])
            rest = rest[1:]
        except (ValueError, IndexError):
            return None
        if idx >= len(step_outputs):
            return None
        obj = step_outputs[idx] or {}
    else:
        return None

    # Traverse remaining keys
    for key in rest:
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj


# ── Pipeline CRUD ─────────────────────────────────────────────────────────────

@router.post("", response_model=PipelineDetailOut, status_code=201)
async def create_pipeline(
    req: PipelineCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Create a new pipeline definition."""
    pipeline = TaskPipelineDB(
        user_id=UUID(user_id),
        org_id=req.org_id,
        name=req.name,
        description=req.description,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(pipeline)
    await db.flush()

    for i, step_data in enumerate(req.steps):
        step = TaskPipelineStepDB(
            pipeline_id=pipeline.id,
            step_order=i,
            name=step_data.name,
            task_type=step_data.task_type,
            execution_mode=step_data.execution_mode,
            task_config=step_data.task_config,
            input_mapping=step_data.input_mapping,
            created_at=utcnow(),
        )
        db.add(step)

    await db.commit()
    await db.refresh(pipeline)

    # Load steps
    steps_result = await db.execute(
        select(TaskPipelineStepDB)
        .where(TaskPipelineStepDB.pipeline_id == pipeline.id)
        .order_by(TaskPipelineStepDB.step_order)
    )
    steps = steps_result.scalars().all()

    out = PipelineDetailOut.model_validate(pipeline)
    out.step_count = len(steps)
    out.run_count = 0
    out.steps = [
        _step_out(s)
        for s in steps
    ]
    return out


@router.get("", response_model=PaginatedPipelines)
async def list_pipelines(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    org_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List pipelines for the current user."""
    uid = UUID(user_id)
    q = select(TaskPipelineDB).where(TaskPipelineDB.user_id == uid)
    if org_id:
        q = q.where(TaskPipelineDB.org_id == org_id)

    total_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(total_q)).scalar() or 0

    q = q.order_by(TaskPipelineDB.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    pipelines = result.scalars().all()

    items = []
    for p in pipelines:
        step_count = (await db.execute(
            select(func.count()).where(TaskPipelineStepDB.pipeline_id == p.id)
        )).scalar() or 0
        run_count = (await db.execute(
            select(func.count()).where(TaskPipelineRunDB.pipeline_id == p.id)
        )).scalar() or 0
        out = PipelineOut.model_validate(p)
        out.step_count = step_count
        out.run_count = run_count
        items.append(out)

    return PaginatedPipelines(items=items, total=total, page=page, page_size=page_size)


@router.get("/{pipeline_id}", response_model=PipelineDetailOut)
async def get_pipeline(
    pipeline_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get a pipeline with all its steps."""
    pipeline = await _get_pipeline(pipeline_id, user_id, db)

    steps_result = await db.execute(
        select(TaskPipelineStepDB)
        .where(TaskPipelineStepDB.pipeline_id == pipeline.id)
        .order_by(TaskPipelineStepDB.step_order)
    )
    steps = steps_result.scalars().all()

    step_count = len(steps)
    run_count = (await db.execute(
        select(func.count()).where(TaskPipelineRunDB.pipeline_id == pipeline.id)
    )).scalar() or 0

    out = PipelineDetailOut.model_validate(pipeline)
    out.step_count = step_count
    out.run_count = run_count
    out.steps = [_step_out(s) for s in steps]
    return out


@router.delete("/{pipeline_id}", status_code=204)
async def delete_pipeline(
    pipeline_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Delete a pipeline definition (and all its runs)."""
    pipeline = await _get_pipeline(pipeline_id, user_id, db)
    await db.delete(pipeline)
    await db.commit()


# ── Pipeline Runs ─────────────────────────────────────────────────────────────

@router.post("/{pipeline_id}/run", response_model=PipelineRunOut, status_code=202)
async def run_pipeline(
    pipeline_id: UUID,
    req: PipelineRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Start a new pipeline run with the given input."""
    pipeline = await _get_pipeline(pipeline_id, user_id, db)

    # Load steps
    steps_result = await db.execute(
        select(TaskPipelineStepDB)
        .where(TaskPipelineStepDB.pipeline_id == pipeline.id)
        .order_by(TaskPipelineStepDB.step_order)
    )
    steps = steps_result.scalars().all()
    if not steps:
        raise HTTPException(status_code=400, detail="Pipeline has no steps")

    # Check user credits
    user_result = await db.execute(select(UserDB).where(UserDB.id == UUID(user_id)))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Estimate credits (sum of AI step costs; human steps handled separately)
    ai_steps = [s for s in steps if s.execution_mode == "ai"]
    estimated_credits = sum(TASK_CREDITS.get(s.task_type, 1) for s in ai_steps)
    if user.credits < estimated_credits:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient credits. Need ~{estimated_credits}, have {user.credits}."
        )

    # Create the run record
    run = TaskPipelineRunDB(
        pipeline_id=pipeline.id,
        user_id=UUID(user_id),
        status="running",
        input=req.input,
        current_step=0,
        created_at=utcnow(),
        started_at=utcnow(),
    )
    db.add(run)
    await db.flush()

    # Create pending step run records
    for step in steps:
        sr = TaskPipelineStepRunDB(
            run_id=run.id,
            step_id=step.id,
            step_order=step.step_order,
            status="pending",
        )
        db.add(sr)

    await db.commit()
    await db.refresh(run)

    # Run the pipeline in the background
    background_tasks.add_task(_execute_pipeline_run, run.id, str(pipeline.id), str(user_id))

    # Return initial state
    step_runs_result = await db.execute(
        select(TaskPipelineStepRunDB)
        .where(TaskPipelineStepRunDB.run_id == run.id)
        .order_by(TaskPipelineStepRunDB.step_order)
    )
    step_runs = step_runs_result.scalars().all()

    out = PipelineRunOut.model_validate(run)
    out.step_runs = [_step_run_out(sr) for sr in step_runs]
    return out


@router.get("/{pipeline_id}/runs", response_model=PaginatedPipelineRuns)
async def list_pipeline_runs(
    pipeline_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """List all runs for a pipeline."""
    pipeline = await _get_pipeline(pipeline_id, user_id, db)

    total = (await db.execute(
        select(func.count()).where(TaskPipelineRunDB.pipeline_id == pipeline.id)
    )).scalar() or 0

    runs_result = await db.execute(
        select(TaskPipelineRunDB)
        .where(TaskPipelineRunDB.pipeline_id == pipeline.id)
        .order_by(TaskPipelineRunDB.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    runs = runs_result.scalars().all()

    items = []
    for run in runs:
        step_runs_result = await db.execute(
            select(TaskPipelineStepRunDB)
            .where(TaskPipelineStepRunDB.run_id == run.id)
            .order_by(TaskPipelineStepRunDB.step_order)
        )
        step_runs = step_runs_result.scalars().all()
        out = PipelineRunOut.model_validate(run)
        out.step_runs = [_step_run_out(sr) for sr in step_runs]
        items.append(out)

    return PaginatedPipelineRuns(items=items, total=total, page=page, page_size=page_size)


@router.get("/runs/{run_id}", response_model=PipelineRunOut)
async def get_pipeline_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get the current state of a pipeline run."""
    run_result = await db.execute(
        select(TaskPipelineRunDB).where(
            TaskPipelineRunDB.id == run_id,
            TaskPipelineRunDB.user_id == UUID(user_id),
        )
    )
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Pipeline run not found")

    step_runs_result = await db.execute(
        select(TaskPipelineStepRunDB)
        .where(TaskPipelineStepRunDB.run_id == run.id)
        .order_by(TaskPipelineStepRunDB.step_order)
    )
    step_runs = step_runs_result.scalars().all()

    out = PipelineRunOut.model_validate(run)
    out.step_runs = [_step_run_out(sr) for sr in step_runs]
    return out


@router.post("/runs/{run_id}/cancel", status_code=200)
async def cancel_pipeline_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Cancel a running pipeline."""
    run_result = await db.execute(
        select(TaskPipelineRunDB).where(
            TaskPipelineRunDB.id == run_id,
            TaskPipelineRunDB.user_id == UUID(user_id),
        )
    )
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    if run.status not in ("pending", "running"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel a {run.status} run")

    run.status = "cancelled"
    run.completed_at = utcnow()
    await db.commit()
    return {"message": "Pipeline run cancelled"}


# ── Background Execution ───────────────────────────────────────────────────────

async def _execute_pipeline_run(run_id: UUID, pipeline_id: str, user_id: str) -> None:
    """Execute each step of a pipeline run sequentially."""
    from core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        try:
            run_result = await db.execute(select(TaskPipelineRunDB).where(TaskPipelineRunDB.id == run_id))
            run = run_result.scalar_one_or_none()
            if not run or run.status == "cancelled":
                return

            # Load steps ordered
            steps_result = await db.execute(
                select(TaskPipelineStepDB)
                .where(TaskPipelineStepDB.pipeline_id == UUID(pipeline_id))
                .order_by(TaskPipelineStepDB.step_order)
            )
            steps = steps_result.scalars().all()

            # Load step runs
            step_runs_result = await db.execute(
                select(TaskPipelineStepRunDB)
                .where(TaskPipelineStepRunDB.run_id == run_id)
                .order_by(TaskPipelineStepRunDB.step_order)
            )
            step_runs = step_runs_result.scalars().all()

            step_outputs: list[Optional[dict]] = [None] * len(steps)

            for i, (step, sr) in enumerate(zip(steps, step_runs)):
                # Check if cancelled
                await db.refresh(run)
                if run.status == "cancelled":
                    return

                # Mark step running
                sr.status = "running"
                sr.started_at = utcnow()
                run.current_step = i
                await db.commit()

                # Resolve input
                task_input = _resolve_input(
                    step.input_mapping,
                    step.task_config,
                    run.input,
                    step_outputs,
                )

                if step.execution_mode == "ai":
                    # Execute AI task directly
                    try:
                        client = get_rebasekit_client()
                        output = await execute_task(step.task_type, task_input, client)

                        # Create a task record for traceability
                        task = TaskDB(
                            user_id=UUID(user_id),
                            type=step.task_type,
                            status="completed",
                            execution_mode="ai",
                            input=task_input,
                            output=output,
                            credits_used=TASK_CREDITS.get(step.task_type, 1),
                            created_at=utcnow(),
                            started_at=utcnow(),
                            completed_at=utcnow(),
                        )
                        db.add(task)
                        await db.flush()

                        # Deduct credits
                        cost = TASK_CREDITS.get(step.task_type, 1)
                        user_result = await db.execute(select(UserDB).where(UserDB.id == UUID(user_id)))
                        user = user_result.scalar_one()
                        if user.credits >= cost:
                            user.credits -= cost
                            txn = CreditTransactionDB(
                                user_id=user.id,
                                amount=-cost,
                                type="charge",
                                description=f"Pipeline step: {step.name} ({step.task_type})",
                                task_id=task.id,
                            )
                            db.add(txn)

                        sr.task_id = task.id
                        sr.output = output
                        sr.status = "completed"
                        sr.completed_at = utcnow()
                        step_outputs[i] = output
                        await db.commit()

                    except (WorkerError, Exception) as e:
                        sr.status = "failed"
                        sr.completed_at = utcnow()
                        run.status = "failed"
                        run.error = str(e)
                        run.completed_at = utcnow()
                        await db.commit()
                        return

                else:
                    # Human step — create an open task and mark step as "running"
                    # The step will remain in 'running' state until the task completes
                    task = TaskDB(
                        user_id=UUID(user_id),
                        type=step.task_type,
                        status="open",
                        execution_mode="human",
                        input=task_input,
                        task_instructions=task_input.get("instructions"),
                        worker_reward_credits=task_input.get("reward_credits", 5),
                        assignments_required=task_input.get("workers", 1),
                        created_at=utcnow(),
                    )
                    db.add(task)
                    await db.flush()

                    sr.task_id = task.id
                    sr.input = task_input
                    sr.status = "running"
                    await db.commit()

                    # For human tasks we stop here — pipeline remains "running"
                    # A webhook or polling mechanism would advance it when complete.
                    # For now, we pause and return to allow human work to complete.
                    logger.info("pipeline_waiting_for_human_step",
                                run_id=str(run_id), step=i, task_id=str(task.id))
                    return  # Remain in "running" status

            # All steps completed
            last_output = next((o for o in reversed(step_outputs) if o is not None), None)
            run.status = "completed"
            run.output = last_output
            run.completed_at = utcnow()
            await db.commit()
            logger.info("pipeline_run_completed", run_id=str(run_id))

        except Exception as e:
            logger.error("pipeline_run_error", run_id=str(run_id), error=str(e))
            try:
                run_result = await db.execute(select(TaskPipelineRunDB).where(TaskPipelineRunDB.id == run_id))
                run = run_result.scalar_one_or_none()
                if run:
                    run.status = "failed"
                    run.error = str(e)
                    run.completed_at = utcnow()
                    await db.commit()
            except Exception:
                pass


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_pipeline(pipeline_id: UUID, user_id: str, db: AsyncSession) -> TaskPipelineDB:
    result = await db.execute(
        select(TaskPipelineDB).where(
            TaskPipelineDB.id == pipeline_id,
            TaskPipelineDB.user_id == UUID(user_id),
        )
    )
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    return pipeline


def _step_out(step: TaskPipelineStepDB):
    from models.schemas import PipelineStepOut
    return PipelineStepOut(
        id=step.id,
        pipeline_id=step.pipeline_id,
        step_order=step.step_order,
        name=step.name,
        task_type=step.task_type,
        execution_mode=step.execution_mode,
        task_config=step.task_config or {},
        input_mapping=step.input_mapping,
        created_at=step.created_at,
    )


def _step_run_out(sr: TaskPipelineStepRunDB):
    from models.schemas import PipelineStepRunOut
    return PipelineStepRunOut(
        id=sr.id,
        step_order=sr.step_order,
        task_id=sr.task_id,
        status=sr.status,
        input=sr.input,
        output=sr.output,
        started_at=sr.started_at,
        completed_at=sr.completed_at,
    )
