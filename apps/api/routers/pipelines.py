"""Task Pipeline endpoints — define and run multi-step task chains."""
from __future__ import annotations
import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from core.auth import get_current_user_id
from core.background import safe_create_task
from core.scopes import require_scope, SCOPE_PIPELINES_READ, SCOPE_PIPELINES_WRITE
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


# ── Condition Evaluation ───────────────────────────────────────────────────────

_CONDITION_RE = re.compile(
    r"^(.+?)\s*(==|!=|>=|<=|>|<)\s*(.+)$"
)


def _evaluate_condition(
    condition: Optional[str],
    pipeline_input: dict,
    step_outputs: list[Optional[dict]],
) -> bool:
    """Evaluate a condition expression against the pipeline state.

    Supported forms:
        $.steps.0.output.score > 0.8        — compare extracted value
        $.input.type == "web_research"      — string comparison
        $.steps.1.output.is_flagged         — truthy check (no operator)
        true / false                        — literals

    Returns True if the condition passes (step should run), False to skip.
    A None/empty condition always passes.
    """
    if not condition or not condition.strip():
        return True

    expr = condition.strip()

    # Check for binary operator
    m = _CONDITION_RE.match(expr)
    if m:
        left_path, op, right_raw = m.group(1).strip(), m.group(2), m.group(3).strip()

        left_val = _extract_path(left_path, pipeline_input, step_outputs) if left_path.startswith("$") else left_path

        # Parse right side
        right_val: Any
        if right_raw.startswith('"') or right_raw.startswith("'"):
            right_val = right_raw.strip("\"'")
        elif right_raw.lower() in ("true", "false"):
            right_val = right_raw.lower() == "true"
        elif right_raw.lower() in ("null", "none"):
            right_val = None
        else:
            try:
                right_val = float(right_raw)
            except ValueError:
                right_val = right_raw

        # Coerce left to same type as right if possible
        if isinstance(right_val, float) and left_val is not None:
            try:
                left_val = float(str(left_val))
            except (ValueError, TypeError):
                pass

        try:
            if op == "==":
                return left_val == right_val
            if op == "!=":
                return left_val != right_val
            if op == ">":
                return float(left_val) > float(right_val)  # type: ignore[arg-type]
            if op == ">=":
                return float(left_val) >= float(right_val)  # type: ignore[arg-type]
            if op == "<":
                return float(left_val) < float(right_val)  # type: ignore[arg-type]
            if op == "<=":
                return float(left_val) <= float(right_val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    # Simple truthy path check
    if expr.lower() == "true":
        return True
    if expr.lower() == "false":
        return False
    if expr.startswith("$"):
        val = _extract_path(expr, pipeline_input, step_outputs)
        return bool(val)

    return True  # Unknown expression — pass through


# ── Pipeline CRUD ─────────────────────────────────────────────────────────────

@router.post("", response_model=PipelineDetailOut, status_code=201)
async def create_pipeline(
    req: PipelineCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_PIPELINES_WRITE)),
):
    """Create a new pipeline definition."""
    # Enforce pipeline-total quota
    user_result = await db.execute(select(UserDB).where(UserDB.id == UUID(user_id)))
    user = user_result.scalar_one_or_none()
    if user:
        from core.quotas import enforce_pipeline_total_quota
        await enforce_pipeline_total_quota(db, user_id, user.plan)

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
            condition=step_data.condition,
            next_on_pass=step_data.next_on_pass,
            next_on_fail=step_data.next_on_fail,
            max_retries=step_data.max_retries or 0,
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
    user_id: str = Depends(require_scope(SCOPE_PIPELINES_READ)),
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

    # Bulk-fetch step_count + run_count in 2 GROUP BY queries (avoids N+1)
    pipeline_ids = [p.id for p in pipelines]
    step_counts: dict = {}
    run_counts: dict = {}
    if pipeline_ids:
        sc_rows = (await db.execute(
            select(TaskPipelineStepDB.pipeline_id, func.count().label("cnt"))
            .where(TaskPipelineStepDB.pipeline_id.in_(pipeline_ids))
            .group_by(TaskPipelineStepDB.pipeline_id)
        )).all()
        step_counts = {r.pipeline_id: r.cnt for r in sc_rows}

        rc_rows = (await db.execute(
            select(TaskPipelineRunDB.pipeline_id, func.count().label("cnt"))
            .where(TaskPipelineRunDB.pipeline_id.in_(pipeline_ids))
            .group_by(TaskPipelineRunDB.pipeline_id)
        )).all()
        run_counts = {r.pipeline_id: r.cnt for r in rc_rows}

    items = []
    for p in pipelines:
        out = PipelineOut.model_validate(p)
        out.step_count = step_counts.get(p.id, 0)
        out.run_count = run_counts.get(p.id, 0)
        items.append(out)

    return PaginatedPipelines(items=items, total=total, page=page, page_size=page_size)


@router.get("/{pipeline_id}", response_model=PipelineDetailOut)
async def get_pipeline(
    pipeline_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_PIPELINES_READ)),
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


@router.delete("/{pipeline_id}", status_code=204, response_model=None)
async def delete_pipeline(
    pipeline_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_PIPELINES_WRITE)),
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
    user_id: str = Depends(require_scope(SCOPE_PIPELINES_WRITE)),
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

    # Enforce pipeline-run quota
    from core.quotas import enforce_pipeline_run_quota, record_pipeline_run
    await enforce_pipeline_run_quota(db, user_id, user.plan)

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

    # Record pipeline run quota usage
    await record_pipeline_run(db, user_id)

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
    user_id: str = Depends(require_scope(SCOPE_PIPELINES_READ)),
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

    # Bulk-fetch all step_runs for all runs in one query (avoids N+1)
    run_ids = [r.id for r in runs]
    step_runs_by_run: dict = {}
    if run_ids:
        sr_rows = (await db.execute(
            select(TaskPipelineStepRunDB)
            .where(TaskPipelineStepRunDB.run_id.in_(run_ids))
            .order_by(TaskPipelineStepRunDB.run_id, TaskPipelineStepRunDB.step_order)
        )).scalars().all()
        for sr in sr_rows:
            step_runs_by_run.setdefault(sr.run_id, []).append(sr)

    items = []
    for run in runs:
        out = PipelineRunOut.model_validate(run)
        out.step_runs = [_step_run_out(sr) for sr in step_runs_by_run.get(run.id, [])]
        items.append(out)

    return PaginatedPipelineRuns(items=items, total=total, page=page, page_size=page_size)


@router.get("/runs/{run_id}", response_model=PipelineRunOut)
async def get_pipeline_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_PIPELINES_READ)),
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


@router.post("/runs/{run_id}/retry", response_model=PipelineRunOut, status_code=202)
async def retry_pipeline_run(
    run_id: UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_PIPELINES_WRITE)),
):
    """Retry a failed or cancelled pipeline run from the first failed step.

    Creates a NEW run that reuses the original input but picks up from where
    the failed run left off. The original run is not modified.
    """
    run_result = await db.execute(
        select(TaskPipelineRunDB).where(
            TaskPipelineRunDB.id == run_id,
            TaskPipelineRunDB.user_id == UUID(user_id),
        )
    )
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    if run.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"Only failed or cancelled runs can be retried (current: {run.status})")

    # Find the step index to resume from (first step that didn't complete/skip)
    sr_result = await db.execute(
        select(TaskPipelineStepRunDB)
        .where(TaskPipelineStepRunDB.run_id == run_id)
        .order_by(TaskPipelineStepRunDB.step_order)
    )
    step_runs = sr_result.scalars().all()
    resume_from = 0
    prior_outputs: list[Optional[dict]] = []
    for sr in step_runs:
        if sr.status in ("completed", "skipped"):
            resume_from = sr.step_order + 1
            prior_outputs.append(sr.output)
        else:
            prior_outputs.append(None)
            break

    # Load steps
    steps_result = await db.execute(
        select(TaskPipelineStepDB)
        .where(TaskPipelineStepDB.pipeline_id == run.pipeline_id)
        .order_by(TaskPipelineStepDB.step_order)
    )
    steps = steps_result.scalars().all()

    # Pad prior_outputs to full step count
    while len(prior_outputs) < len(steps):
        prior_outputs.append(None)

    # Check credits
    user_result = await db.execute(select(UserDB).where(UserDB.id == UUID(user_id)))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    remaining_steps = steps[resume_from:]
    estimated_credits = sum(
        TASK_CREDITS.get(s.task_type, 1)
        for s in remaining_steps
        if s.execution_mode == "ai"
    )
    if user.credits < estimated_credits:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient credits to retry. Need ~{estimated_credits}, have {user.credits}.",
        )

    # Create a fresh run record (retry = new run, original preserved)
    new_run = TaskPipelineRunDB(
        pipeline_id=run.pipeline_id,
        user_id=UUID(user_id),
        status="running",
        input=run.input,
        current_step=resume_from,
        created_at=utcnow(),
        started_at=utcnow(),
    )
    db.add(new_run)
    await db.flush()

    for step in steps:
        sr_status = "pending"
        sr_output = None
        if step.step_order < resume_from:
            # Copy results from the original run
            orig = next((s for s in step_runs if s.step_order == step.step_order), None)
            if orig:
                sr_status = orig.status
                sr_output = orig.output
        new_sr = TaskPipelineStepRunDB(
            run_id=new_run.id,
            step_id=step.id,
            step_order=step.step_order,
            status=sr_status,
            output=sr_output,
        )
        db.add(new_sr)

    await db.commit()
    await db.refresh(new_run)

    background_tasks.add_task(
        _execute_pipeline_run,
        new_run.id,
        str(run.pipeline_id),
        user_id,
        resume_from,
        prior_outputs,
    )

    new_step_runs = (await db.execute(
        select(TaskPipelineStepRunDB)
        .where(TaskPipelineStepRunDB.run_id == new_run.id)
        .order_by(TaskPipelineStepRunDB.step_order)
    )).scalars().all()

    out = PipelineRunOut.model_validate(new_run)
    out.step_runs = [_step_run_out(sr) for sr in new_step_runs]
    return out


@router.post("/runs/{run_id}/cancel", status_code=200)
async def cancel_pipeline_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_PIPELINES_WRITE)),
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

async def _execute_pipeline_run(
    run_id: UUID,
    pipeline_id: str,
    user_id: str,
    resume_from_step: int = 0,
    prior_outputs: Optional[list[Optional[dict]]] = None,
) -> None:
    """Execute each step of a pipeline run sequentially.

    Args:
        run_id: The pipeline run ID.
        pipeline_id: The pipeline definition ID.
        user_id: The user who owns the pipeline.
        resume_from_step: The step_order index to start/resume from.
        prior_outputs: Outputs already collected from steps 0..(resume_from_step-1).
    """
    from core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        try:
            run_result = await db.execute(select(TaskPipelineRunDB).where(TaskPipelineRunDB.id == run_id))
            run = run_result.scalar_one_or_none()
            # Guard: skip if run doesn't exist, was cancelled, or already reached a
            # terminal state (prevents double-execution on concurrent invocations or
            # after a restart picks up a run that already completed).
            if not run or run.status in ("cancelled", "completed", "failed"):
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

            # Build output history — start from prior_outputs, fill in completed steps
            step_outputs: list[Optional[dict]] = prior_outputs if prior_outputs else [None] * len(steps)
            if len(step_outputs) < len(steps):
                step_outputs.extend([None] * (len(steps) - len(step_outputs)))

            # Backfill outputs from completed step_runs we may have skipped
            for sr in step_runs:
                if sr.step_order < resume_from_step and sr.output is not None:
                    step_outputs[sr.step_order] = sr.output

            # Use an index-based cursor for branching support
            current_idx = resume_from_step
            step_map = {s.step_order: s for s in steps}
            sr_map = {sr.step_order: sr for sr in step_runs}
            max_steps = len(steps)
            visited: set[int] = set()  # guard against infinite loops

            while current_idx < max_steps:
                if current_idx in visited:
                    # Loop detected
                    run.status = "failed"
                    run.error = f"Infinite loop detected at step {current_idx}"
                    run.completed_at = utcnow()
                    await db.commit()
                    return
                visited.add(current_idx)

                step = step_map.get(current_idx)
                sr = sr_map.get(current_idx)
                if step is None or sr is None:
                    break  # No more steps

                # Check if cancelled
                await db.refresh(run)
                if run.status == "cancelled":
                    return

                # ── Evaluate condition ────────────────────────────────────────
                condition_passes = _evaluate_condition(
                    step.condition,
                    run.input,
                    step_outputs,
                )
                if not condition_passes:
                    # Skip this step — mark skipped and advance
                    sr.status = "skipped"
                    sr.completed_at = utcnow()
                    await db.commit()
                    logger.info(
                        "pipeline_step_skipped_condition",
                        run_id=str(run_id), step=current_idx, condition=step.condition,
                    )
                    # Honor next_on_pass for the "skip" path (treat skip as pass-through)
                    current_idx = step.next_on_pass if step.next_on_pass is not None else current_idx + 1
                    continue

                # Mark step running
                sr.status = "running"
                sr.started_at = utcnow()
                run.current_step = current_idx
                await db.commit()

                # Resolve input
                task_input = _resolve_input(
                    step.input_mapping,
                    step.task_config,
                    run.input,
                    step_outputs,
                )

                if step.execution_mode == "ai":
                    max_retries = step.max_retries or 0
                    last_error: Optional[Exception] = None
                    succeeded = False

                    for attempt in range(max_retries + 1):  # 0..max_retries inclusive
                        if attempt > 0:
                            # Mark as retrying before each retry attempt
                            sr.status = "retrying"
                            sr.retry_count = attempt
                            await db.commit()
                            logger.info(
                                "pipeline_step_retrying",
                                run_id=str(run_id), step=current_idx,
                                attempt=attempt, max_retries=max_retries,
                            )
                            # Brief backoff: 2^attempt seconds (1s, 2s, 4s ...)
                            await asyncio.sleep(min(2 ** attempt, 30))

                        try:
                            client = get_rebasekit_client()
                            output = await execute_task(step.task_type, task_input, client)

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
                            sr.retry_count = attempt
                            sr.completed_at = utcnow()
                            step_outputs[current_idx] = output
                            await db.commit()
                            succeeded = True
                            break  # Exit retry loop

                        except (WorkerError, Exception) as e:
                            last_error = e
                            logger.warning(
                                "pipeline_step_attempt_failed",
                                run_id=str(run_id), step=current_idx,
                                attempt=attempt, error=str(e),
                            )

                    if succeeded:
                        # Determine next step (branch on success)
                        current_idx = step.next_on_pass if step.next_on_pass is not None else current_idx + 1
                    else:
                        # All attempts failed
                        sr.status = "failed"
                        sr.retry_count = max_retries
                        sr.completed_at = utcnow()

                        # Check if there's an on-failure branch
                        next_fail = step.next_on_fail
                        if next_fail is not None and next_fail >= 0:
                            # Branch to failure recovery step
                            step_outputs[current_idx] = {"error": str(last_error), "step": current_idx}
                            await db.commit()
                            logger.info(
                                "pipeline_step_failed_branching",
                                run_id=str(run_id), step=current_idx, next=next_fail,
                            )
                            current_idx = next_fail
                        else:
                            # Default: fail the pipeline
                            run.status = "failed"
                            run.error = str(last_error)
                            run.completed_at = utcnow()
                            await db.commit()
                            return

                else:
                    # Human step — pause and wait
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

                    logger.info("pipeline_waiting_for_human_step",
                                run_id=str(run_id), step=current_idx, task_id=str(task.id))
                    return  # Pause — resume via resume_pipeline_after_human_step()

            # All steps processed
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
                logger.error(
                    "pipeline_run.error_recovery_failed",
                    run_id=str(run_id),
                    exc_info=True,
                )


async def resume_pipeline_after_human_step(
    task_id: UUID,
    task_output: Optional[dict],
    db: AsyncSession,
) -> None:
    """Called when a human-assigned task is completed.

    If the task belongs to a pipeline step run, marks that step as complete
    and resumes the pipeline from the next step in a background coroutine.

    This is a no-op if the task is not linked to any pipeline step.
    """
    # Look up the step run linked to this task
    sr_result = await db.execute(
        select(TaskPipelineStepRunDB).where(TaskPipelineStepRunDB.task_id == task_id)
    )
    sr = sr_result.scalar_one_or_none()
    if not sr:
        return  # Task is not part of any pipeline

    # Mark the step as completed
    sr.status = "completed"
    sr.output = task_output
    sr.completed_at = utcnow()

    # Load the run
    run_result = await db.execute(
        select(TaskPipelineRunDB).where(TaskPipelineRunDB.id == sr.run_id)
    )
    run = run_result.scalar_one_or_none()
    if not run or run.status in ("cancelled", "failed", "completed"):
        await db.commit()
        return

    # Load the pipeline to get the pipeline_id + user_id
    pipeline_result = await db.execute(
        select(TaskPipelineDB).where(TaskPipelineDB.id == run.pipeline_id)
    )
    pipeline = pipeline_result.scalar_one_or_none()
    if not pipeline:
        await db.commit()
        return

    await db.commit()

    # Resume execution starting from the NEXT step
    next_step = sr.step_order + 1
    logger.info(
        "pipeline_resuming_after_human_step",
        run_id=str(run.id),
        completed_step=sr.step_order,
        next_step=next_step,
    )

    # Fire the continuation as a background task (non-blocking)
    safe_create_task(
        _execute_pipeline_run(
            run_id=run.id,
            pipeline_id=str(pipeline.id),
            user_id=str(run.user_id),
            resume_from_step=next_step,
        )
    )


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
        condition=step.condition,
        next_on_pass=step.next_on_pass,
        next_on_fail=step.next_on_fail,
        max_retries=step.max_retries or 0,
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
        retry_count=sr.retry_count or 0,
        started_at=sr.started_at,
        completed_at=sr.completed_at,
    )
