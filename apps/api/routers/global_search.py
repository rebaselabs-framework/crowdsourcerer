"""Global search across tasks, workers, and organizations.

Uses PostgreSQL ILIKE for simple text search across fields.
Results are scoped: tasks/workers owned by or accessible to the user;
orgs only if the user is a member.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, or_, cast, String
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user_id
from core.database import get_db
from models.db import TaskDB, UserDB, OrganizationDB, OrgMemberDB, WorkerSkillDB

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/search", tags=["search"])

_LIKE_ESC = "\\"


def _esc_like(s: str) -> str:
    """Escape ILIKE/LIKE special characters so user input is treated literally."""
    return s.replace(_LIKE_ESC, _LIKE_ESC * 2).replace("%", f"{_LIKE_ESC}%").replace("_", f"{_LIKE_ESC}_")


@router.get("/global")
async def global_search(
    q: str = Query(..., min_length=1, max_length=200, description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Search across tasks, workers, and orgs the user belongs to.

    Tasks: match on type, task_instructions.
    Workers: match on name, email (display), and skill types.
    Orgs: match on name — only orgs the user is a member of.
    """
    search_term = f"%{_esc_like(q)}%"

    # ── Tasks ─────────────────────────────────────────────────────────────────
    task_res = await db.execute(
        select(TaskDB).where(
            TaskDB.user_id == user_id,
            or_(
                TaskDB.type.ilike(search_term, escape=_LIKE_ESC),
                TaskDB.task_instructions.ilike(search_term, escape=_LIKE_ESC),
                cast(TaskDB.input, String).ilike(search_term, escape=_LIKE_ESC),
            ),
        ).order_by(TaskDB.created_at.desc()).limit(limit)
    )
    tasks = task_res.scalars().all()
    task_results = [
        {
            "id": str(t.id),
            "title": (
                t.task_instructions[:60] + "…"
                if t.task_instructions and len(t.task_instructions) > 60
                else (t.task_instructions or t.type.replace("_", " ").title())
            ),
            "type": t.type,
            "status": t.status,
            "url": f"/dashboard/tasks/{t.id}",
        }
        for t in tasks
    ]

    # ── Workers ───────────────────────────────────────────────────────────────
    # Search on name and email; also match workers who have a skill matching query
    worker_ids_by_skill_res = await db.execute(
        select(WorkerSkillDB.worker_id).where(
            WorkerSkillDB.task_type.ilike(search_term, escape=_LIKE_ESC)
        ).distinct().limit(limit)
    )
    skill_worker_ids = [r for r, in worker_ids_by_skill_res.fetchall()]

    worker_res = await db.execute(
        select(UserDB).where(
            UserDB.role.in_(["worker", "both"]),
            UserDB.is_active == True,  # noqa: E712
            or_(
                UserDB.name.ilike(search_term, escape=_LIKE_ESC),
                UserDB.email.ilike(search_term, escape=_LIKE_ESC),
                UserDB.id.in_(skill_worker_ids),
            ),
        ).limit(limit)
    )
    workers = worker_res.scalars().all()
    worker_results = [
        {
            "id": str(w.id),
            "display_name": w.name or w.email.split("@")[0],
            "tier": f"Level {w.worker_level}",
            "url": f"/workers/{w.id}",
        }
        for w in workers
    ]

    # ── Orgs ──────────────────────────────────────────────────────────────────
    # Only return orgs the user is a member of
    member_org_ids_res = await db.execute(
        select(OrgMemberDB.org_id).where(OrgMemberDB.user_id == user_id)
    )
    member_org_ids = [r for r, in member_org_ids_res.fetchall()]

    org_results = []
    if member_org_ids:
        org_res = await db.execute(
            select(OrganizationDB).where(
                OrganizationDB.id.in_(member_org_ids),
                OrganizationDB.name.ilike(search_term, escape=_LIKE_ESC),
            ).limit(limit)
        )
        orgs = org_res.scalars().all()
        org_results = [
            {
                "id": str(o.id),
                "name": o.name,
                "slug": o.slug,
                "url": f"/dashboard/team",
            }
            for o in orgs
        ]

    return {
        "query": q,
        "total": len(task_results) + len(worker_results) + len(org_results),
        "tasks": task_results,
        "workers": worker_results,
        "orgs": org_results,
    }
