"""Unified full-text search across tasks, pipelines, and templates.

Uses PostgreSQL ILIKE for simplicity (no tsvector needed at this scale).
JSON/JSONB columns are cast to text before matching so input/output blobs
are fully searchable.
Returns a ranked result set with entity type labels.
"""
from __future__ import annotations
import json as _json
from typing import Optional
from uuid import UUID
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, or_, func, cast, String
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.scopes import require_scope, SCOPE_TASKS_READ, SCOPE_ANALYTICS_READ
from models.db import TaskDB, TaskPipelineDB, TaskTemplateDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/search", tags=["search"])

_LIKE_ESC = "\\"


def _esc_like(s: str) -> str:
    """Escape ILIKE/LIKE special characters so user input is treated literally."""
    return s.replace(_LIKE_ESC, _LIKE_ESC * 2).replace("%", f"{_LIKE_ESC}%").replace("_", f"{_LIKE_ESC}_")


# ─── Schemas ──────────────────────────────────────────────────────────────────

class SearchResultItem(BaseModel):
    entity_type: str          # "task" | "pipeline" | "template"
    id: UUID
    title: str
    subtitle: Optional[str]   # e.g. task type, status, category
    status: Optional[str]
    created_at: datetime
    url: str                  # Relative URL to navigate to
    match_context: Optional[dict] = None  # {"field": "output", "snippet": "..."}


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
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
    db: AsyncSession = Depends(get_db),
):
    """Search across tasks, pipelines, and task templates.

    Results are scoped to the authenticated user's owned resources plus public templates.
    Searches task type, instructions, and full input/output JSON content.
    """
    search_term = f"%{_esc_like(q)}%"
    types_filter = set(entity_types.split(",")) if entity_types else {"task", "pipeline", "template"}

    task_results: list[SearchResultItem] = []
    pipeline_results: list[SearchResultItem] = []
    template_results: list[SearchResultItem] = []

    # ── Tasks (search type, instructions, input JSON, output JSON) ────────────
    if "task" in types_filter:
        task_q = select(TaskDB).where(
            TaskDB.user_id == user_id,
            or_(
                TaskDB.type.ilike(search_term, escape=_LIKE_ESC),
                TaskDB.task_instructions.ilike(search_term, escape=_LIKE_ESC),
                cast(TaskDB.input, String).ilike(search_term, escape=_LIKE_ESC),
                cast(TaskDB.output, String).ilike(search_term, escape=_LIKE_ESC),
            ),
        ).order_by(TaskDB.created_at.desc()).limit(limit)

        result = await db.execute(task_q)
        tasks = result.scalars().all()
        term_lower = q.lower()
        task_results = [
            SearchResultItem(
                entity_type="task",
                id=t.id,
                title=_task_title(t),
                subtitle=f"{t.type.replace('_', ' ').title()} • {t.status}",
                status=t.status,
                created_at=t.created_at,
                url=f"/dashboard/tasks/{t.id}",
                match_context=_extract_match_context(t, term_lower),
            )
            for t in tasks
        ]

    # ── Pipelines ─────────────────────────────────────────────────────────────
    if "pipeline" in types_filter:
        pipeline_q = select(TaskPipelineDB).where(
            TaskPipelineDB.user_id == user_id,
            or_(
                TaskPipelineDB.name.ilike(search_term, escape=_LIKE_ESC),
                TaskPipelineDB.description.ilike(search_term, escape=_LIKE_ESC),
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
                TaskTemplateDB.name.ilike(search_term, escape=_LIKE_ESC),
                TaskTemplateDB.description.ilike(search_term, escape=_LIKE_ESC),
                TaskTemplateDB.category.ilike(search_term, escape=_LIKE_ESC),
                TaskTemplateDB.task_type.ilike(search_term, escape=_LIKE_ESC),
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
    tags: Optional[str] = Query(None, description="Comma-separated list of tags to filter by (AND logic)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user_id: str = Depends(require_scope(SCOPE_TASKS_READ)),
    db: AsyncSession = Depends(get_db),
):
    """Advanced task search with full-text filtering across type, instructions,
    and input/output JSON content. Returns paginated results."""
    query = select(TaskDB).where(TaskDB.user_id == user_id)

    if q:
        search_term = f"%{q}%"
        query = query.where(
            or_(
                TaskDB.type.ilike(search_term, escape=_LIKE_ESC),
                TaskDB.task_instructions.ilike(search_term, escape=_LIKE_ESC),
                cast(TaskDB.input, String).ilike(search_term, escape=_LIKE_ESC),
                cast(TaskDB.output, String).ilike(search_term, escape=_LIKE_ESC),
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
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        for tag in tag_list:
            # PostgreSQL: JSON array contains this string value
            query = query.where(
                cast(TaskDB.tags, String).ilike(f'%"{_esc_like(tag)}"%', escape=_LIKE_ESC)
            )

    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    query = query.order_by(TaskDB.created_at.desc()).offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    tasks = result.scalars().all()

    search_term_lower = q.lower() if q else None

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
                "tags": t.tags or [],
                # Highlight the matched field so UI can show context
                "match_context": _extract_match_context(t, search_term_lower),
                "title": _task_title(t),
                "url": f"/dashboard/tasks/{t.id}",
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


def _extract_match_context(task: TaskDB, term: Optional[str]) -> Optional[dict]:
    """Find which field matched and return a short text snippet for display."""
    if not term:
        return None

    def _snip(text: str, window: int = 120) -> str:
        lo = text.lower()
        idx = lo.find(term)
        if idx == -1:
            return text[:window] + ("…" if len(text) > window else "")
        start = max(0, idx - 40)
        end = min(len(text), idx + len(term) + 80)
        snippet = ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")
        return snippet

    # Check instructions first
    if task.task_instructions and term in task.task_instructions.lower():
        return {"field": "instructions", "snippet": _snip(task.task_instructions)}

    # Check input JSON blob
    input_text = _json.dumps(task.input or {})
    if term in input_text.lower():
        return {"field": "input", "snippet": _snip(input_text)}

    # Check output JSON blob
    if task.output:
        output_text = _json.dumps(task.output)
        if term in output_text.lower():
            return {"field": "output", "snippet": _snip(output_text)}

    # Fallback: type matched
    return {"field": "type", "snippet": task.type.replace("_", " ").title()}
