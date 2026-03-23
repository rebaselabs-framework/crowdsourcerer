"""Unified full-text search across tasks, pipelines, and templates.

Uses PostgreSQL ILIKE for simplicity (no tsvector needed at this scale).
Returns a ranked result set with entity type labels.
"""
from __future__ import annotations
from typing import Optional
from uuid import UUID
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from models.db import TaskDB, TaskPipelineDB, TaskTemplateDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/search", tags=["search"])


# ─── Schemas ──────────────────────────────────────────────────────────────────

class SearchResultItem(BaseModel):
    entity_type: str          # "task" | "pipeline" | "template"
    id: UUID
    title: str
    subtitle: Optional[str]   # e.g. task type, status, category
    status: Optional[str]
    created_at: datetime
    url: str                  # Relative URL to navigate to


class SearchResults(BaseModel):
    query: str
    total: int
    tasks: list[SearchResultItem]
    pipelines: list[SearchResultItem]
    templates: list[SearchResultItem]


# ─── Search endpoint ──────────────────────────────────────────────────────────

@router.get("", response_model=SearchResults)
async def unified_search(
    q: str = Query(..., min_length=2, max_length=200, description="Search query"),
    entity_types: Optional[str] = Query(
        None,
        description="Comma-separated entity types to search: task,pipeline,template. Default: all.",
    ),
    limit: int = Query(10, ge=1, le=50),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Search across tasks, pipelines, and task templates.

    Results are scoped to the authenticated user's owned resources plus public templates.
    """
    search_term = f"%{q}%"
    types_filter = set(entity_types.split(",")) if entity_types else {"task", "pipeline", "template"}

    task_results: list[SearchResultItem] = []
    pipeline_results: list[SearchResultItem] = []
    template_results: list[SearchResultItem] = []

    # ── Tasks ────────────────────────────────────────────────────────────────
    if "task" in types_filter:
        task_q = select(TaskDB).where(
            TaskDB.user_id == user_id,
            or_(
                TaskDB.type.ilike(search_term),
                TaskDB.task_instructions.ilike(search_term),
                func.cast(TaskDB.input, type_=None).ilike(search_term),
            ),
        ).order_by(TaskDB.created_at.desc()).limit(limit)

        result = await db.execute(task_q)
        tasks = result.scalars().all()
        task_results = [
            SearchResultItem(
                entity_type="task",
                id=t.id,
                title=_task_title(t),
                subtitle=f"{t.type.replace('_', ' ').title()} • {t.status}",
                status=t.status,
                created_at=t.created_at,
                url=f"/dashboard/tasks/{t.id}",
            )
            for t in tasks
        ]

    # ── Pipelines ─────────────────────────────────────────────────────────────
    if "pipeline" in types_filter:
        pipeline_q = select(TaskPipelineDB).where(
            TaskPipelineDB.user_id == user_id,
            or_(
                TaskPipelineDB.name.ilike(search_term),
                TaskPipelineDB.description.ilike(search_term),
            ),
        ).order_by(TaskPipelineDB.created_at.desc()).limit(limit)

        result = await db.execute(pipeline_q)
        pipelines = result.scalars().all()
        pipeline_results = [
            SearchResultItem(
                entity_type="pipeline",
                id=p.id,
                title=p.name,
                subtitle=p.description[:80] if p.description else None,
                status="active" if p.is_active else "inactive",
                created_at=p.created_at,
                url=f"/dashboard/pipelines",
            )
            for p in pipelines
        ]

    # ── Templates (public + own) ───────────────────────────────────────────────
    if "template" in types_filter:
        template_q = select(TaskTemplateDB).where(
            or_(
                TaskTemplateDB.creator_id == user_id,
                TaskTemplateDB.is_public == True,  # noqa: E712
            ),
            or_(
                TaskTemplateDB.name.ilike(search_term),
                TaskTemplateDB.description.ilike(search_term),
                TaskTemplateDB.category.ilike(search_term),
                TaskTemplateDB.task_type.ilike(search_term),
            ),
        ).order_by(TaskTemplateDB.use_count.desc(), TaskTemplateDB.created_at.desc()).limit(limit)

        result = await db.execute(template_q)
        templates = result.scalars().all()
        template_results = [
            SearchResultItem(
                entity_type="template",
                id=t.id,
                title=t.name,
                subtitle=f"{t.task_type.replace('_', ' ').title()} • {t.category or 'General'}",
                status="featured" if t.is_featured else "public" if t.is_public else "private",
                created_at=t.created_at,
                url=f"/dashboard/marketplace",
            )
            for t in templates
        ]

    total = len(task_results) + len(pipeline_results) + len(template_results)

    return SearchResults(
        query=q,
        total=total,
        tasks=task_results,
        pipelines=pipeline_results,
        templates=template_results,
    )


# ─── Task search (more detailed, with filters) ────────────────────────────────

@router.get("/tasks", response_model=dict)
async def search_tasks(
    q: Optional[str] = Query(None, min_length=2, max_length=200),
    status: Optional[str] = Query(None),
    task_type: Optional[str] = Query(None),
    execution_mode: Optional[str] = Query(None),
    from_date: Optional[datetime] = Query(None),
    to_date: Optional[datetime] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Advanced task search with filtering. Returns paginated results."""
    query = select(TaskDB).where(TaskDB.user_id == user_id)

    if q:
        search_term = f"%{q}%"
        query = query.where(
            or_(
                TaskDB.type.ilike(search_term),
                TaskDB.task_instructions.ilike(search_term),
                func.cast(TaskDB.input, type_=None).ilike(search_term),
            )
        )
    if status:
        query = query.where(TaskDB.status == status)
    if task_type:
        query = query.where(TaskDB.type == task_type)
    if execution_mode:
        query = query.where(TaskDB.execution_mode == execution_mode)
    if from_date:
        query = query.where(TaskDB.created_at >= from_date)
    if to_date:
        query = query.where(TaskDB.created_at <= to_date)

    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    query = query.order_by(TaskDB.created_at.desc()).offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    tasks = result.scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": (page * page_size) < total,
        "items": [
            {
                "id": str(t.id),
                "type": t.type,
                "status": t.status,
                "execution_mode": t.execution_mode,
                "priority": t.priority,
                "credits_used": t.credits_used,
                "created_at": t.created_at.isoformat(),
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "has_output": t.output is not None,
            }
            for t in tasks
        ],
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _task_title(task: TaskDB) -> str:
    """Generate a human-readable title for a task."""
    type_label = task.type.replace("_", " ").title()
    if task.task_instructions:
        snippet = task.task_instructions[:60].strip()
        if len(task.task_instructions) > 60:
            snippet += "…"
        return f"{type_label}: {snippet}"
    # Try to pull from input JSON
    inp = task.input or {}
    for key in ("prompt", "query", "url", "text", "title"):
        val = inp.get(key)
        if val and isinstance(val, str):
            snippet = val[:60].strip()
            if len(val) > 60:
                snippet += "…"
            return f"{type_label}: {snippet}"
    return f"{type_label} Task"
