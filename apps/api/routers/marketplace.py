"""Task Template Marketplace — browse, create, rate, and use community templates."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_

from core.database import get_db
from core.sql import esc_like, LIKE_ESC
from core.scopes import (
    require_scope,
    SCOPE_MARKETPLACE_READ,
    SCOPE_MARKETPLACE_WRITE,
    SCOPE_TASKS_WRITE,
)
from models.db import TaskTemplateDB, TaskTemplateRatingDB, UserDB
from models.schemas import (
    TemplateCreateRequest, TemplateOut, PaginatedTemplates,
    TemplateRateRequest, TemplateRateResponse,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/marketplace", tags=["marketplace"])

# ── Built-in system templates seeded on startup ───────────────────────────────
SYSTEM_TEMPLATES = [
    {
        "name": "Image Classification",
        "description": "Label images into predefined categories with AI speed.",
        "task_type": "label_image",
        "execution_mode": "ai",
        "category": "data_labeling",
        "tags": ["image", "classification", "cv"],
        "task_config": {"labels": ["cat", "dog", "other"], "prompt": "Classify the image."},
        "example_input": {"image_url": "https://example.com/cat.jpg"},
        "is_featured": True,
    },
    {
        "name": "Fact Verification",
        "description": "Verify whether a claim is true, false, or uncertain.",
        "task_type": "verify_fact",
        "execution_mode": "ai",
        "category": "qa",
        "tags": ["fact-check", "nlp", "trust"],
        "task_config": {"prompt": "Is this claim true?"},
        "example_input": {"claim": "The Eiffel Tower is in Paris."},
        "is_featured": True,
    },
    {
        "name": "Content Moderation (Human)",
        "description": "Have trained human moderators review content for policy violations.",
        "task_type": "moderate_content",
        "execution_mode": "human",
        "category": "moderation",
        "tags": ["safety", "human", "content"],
        "task_config": {"guidelines": "Flag if content violates community standards.", "reward_credits": 5, "workers": 2},
        "example_input": {"text": "This is the content to review."},
        "is_featured": True,
    },
    {
        "name": "Text Summarization",
        "description": "Generate concise summaries of long texts.",
        "task_type": "summarize",
        "execution_mode": "ai",
        "category": "nlp",
        "tags": ["summarization", "nlp", "text"],
        "task_config": {"max_words": 100},
        "example_input": {"text": "The quick brown fox..."},
        "is_featured": False,
    },
    {
        "name": "Sentiment Analysis",
        "description": "Classify text as positive, negative, or neutral.",
        "task_type": "analyze_sentiment",
        "execution_mode": "ai",
        "category": "nlp",
        "tags": ["sentiment", "nlp", "text"],
        "task_config": {"labels": ["positive", "negative", "neutral"]},
        "example_input": {"text": "I love this product!"},
        "is_featured": False,
    },
    {
        "name": "Quality Rating (Human)",
        "description": "Have workers rate the quality of content on a 1–5 scale.",
        "task_type": "rate_quality",
        "execution_mode": "human",
        "category": "data_labeling",
        "tags": ["rating", "human", "quality"],
        "task_config": {"scale": 5, "reward_credits": 3, "workers": 3},
        "example_input": {"content": "Rate this response..."},
        "is_featured": False,
    },
    {
        "name": "Q&A — Expert Answers",
        "description": "Collect expert human answers to domain-specific questions.",
        "task_type": "answer_question",
        "execution_mode": "human",
        "category": "knowledge",
        "tags": ["qa", "human", "expert"],
        "task_config": {"reward_credits": 10, "workers": 1, "instructions": "Provide a detailed, accurate answer."},
        "example_input": {"question": "Explain quantum entanglement in simple terms."},
        "is_featured": False,
    },
    {
        "name": "AI Code Review",
        "description": "Use AI to review code snippets for bugs and improvements.",
        "task_type": "review_code",
        "execution_mode": "ai",
        "category": "development",
        "tags": ["code", "review", "ai"],
        "task_config": {"focus": ["bugs", "performance", "readability"]},
        "example_input": {"code": "def foo(): pass", "language": "python"},
        "is_featured": False,
    },
]


async def _ensure_system_templates(db: AsyncSession) -> None:
    """Seed system templates if they don't exist yet."""
    count = await db.scalar(
        select(func.count()).where(TaskTemplateDB.creator_id.is_(None))
    )
    if count and count > 0:
        return  # Already seeded

    for t in SYSTEM_TEMPLATES:
        template = TaskTemplateDB(
            id=uuid4(),
            creator_id=None,  # System template
            name=t["name"],
            description=t["description"],
            task_type=t["task_type"],
            execution_mode=t["execution_mode"],
            category=t["category"],
            tags=t["tags"],
            task_config=t["task_config"],
            example_input=t.get("example_input"),
            is_public=True,
            is_featured=t.get("is_featured", False),
        )
        db.add(template)
    await db.commit()
    logger.info("system_templates_seeded", count=len(SYSTEM_TEMPLATES))


def _template_out(t: TaskTemplateDB) -> TemplateOut:
    avg = round(t.rating_sum / t.rating_count, 1) if t.rating_count > 0 else None
    out = TemplateOut(
        id=t.id,
        creator_id=t.creator_id,
        name=t.name,
        description=t.description,
        task_type=t.task_type,
        execution_mode=t.execution_mode,
        category=t.category,
        tags=t.tags or [],
        task_config=t.task_config or {},
        example_input=t.example_input,
        is_public=t.is_public,
        is_featured=t.is_featured,
        use_count=t.use_count,
        rating_sum=t.rating_sum,
        rating_count=t.rating_count,
        avg_rating=avg,
        created_at=t.created_at,
    )
    return out


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/templates", response_model=PaginatedTemplates)
async def list_templates(
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    task_type: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    execution_mode: Optional[str] = Query(None),
    search: Optional[str] = Query(None, max_length=200),
    sort: str = Query("featured", enum=["featured", "popular", "newest", "top_rated"]),
    my_own: bool = False,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_MARKETPLACE_READ)),
):
    """Browse the template marketplace. Returns public templates + your own private ones."""
    await _ensure_system_templates(db)

    uid = UUID(user_id)
    q = select(TaskTemplateDB).where(
        or_(
            TaskTemplateDB.is_public == True,
            TaskTemplateDB.creator_id == uid,
        )
    )

    if my_own:
        q = q.where(TaskTemplateDB.creator_id == uid)
    if task_type:
        q = q.where(TaskTemplateDB.task_type == task_type)
    if category:
        q = q.where(TaskTemplateDB.category == category)
    if execution_mode:
        q = q.where(TaskTemplateDB.execution_mode == execution_mode)
    if search:
        s = f"%{esc_like(search)}%"
        q = q.where(or_(
            TaskTemplateDB.name.ilike(s, escape=LIKE_ESC),
            TaskTemplateDB.description.ilike(s, escape=LIKE_ESC),
        ))

    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0

    if sort == "featured":
        q = q.order_by(TaskTemplateDB.is_featured.desc(), TaskTemplateDB.use_count.desc())
    elif sort == "popular":
        q = q.order_by(TaskTemplateDB.use_count.desc())
    elif sort == "newest":
        q = q.order_by(TaskTemplateDB.created_at.desc())
    elif sort == "top_rated":
        # Sort by average rating (rating_sum / rating_count), then use_count
        q = q.order_by(
            (TaskTemplateDB.rating_sum / (TaskTemplateDB.rating_count + 1)).desc(),
            TaskTemplateDB.use_count.desc(),
        )

    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    templates = result.scalars().all()

    return PaginatedTemplates(
        items=[_template_out(t) for t in templates],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/templates/{template_id}", response_model=TemplateOut)
async def get_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_MARKETPLACE_READ)),
):
    """Get a single template's details."""
    t = await _get_template(template_id, user_id, db)
    return _template_out(t)


@router.post("/templates", response_model=TemplateOut, status_code=201)
async def create_template(
    req: TemplateCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_MARKETPLACE_WRITE)),
):
    """Save a task configuration as a reusable template."""
    template = TaskTemplateDB(
        id=uuid4(),
        creator_id=UUID(user_id),
        name=req.name,
        description=req.description,
        task_type=req.task_type,
        execution_mode=req.execution_mode,
        category=req.category,
        tags=req.tags,
        task_config=req.task_config,
        example_input=req.example_input,
        is_public=req.is_public,
        is_featured=False,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    logger.info("template_created", template_id=str(template.id), user_id=user_id)
    return _template_out(template)


@router.patch("/templates/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: UUID,
    req: TemplateCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_MARKETPLACE_WRITE)),
):
    """Update your own template."""
    result = await db.execute(
        select(TaskTemplateDB).where(
            TaskTemplateDB.id == template_id,
            TaskTemplateDB.creator_id == UUID(user_id),
        )
    )
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")

    t.name = req.name
    t.description = req.description
    t.task_type = req.task_type
    t.execution_mode = req.execution_mode
    t.category = req.category
    t.tags = req.tags
    t.task_config = req.task_config
    t.example_input = req.example_input
    t.is_public = req.is_public
    t.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(t)
    return _template_out(t)


@router.delete("/templates/{template_id}", status_code=204, response_model=None)
async def delete_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_MARKETPLACE_WRITE)),
):
    """Delete your own template."""
    result = await db.execute(
        select(TaskTemplateDB).where(
            TaskTemplateDB.id == template_id,
            TaskTemplateDB.creator_id == UUID(user_id),
        )
    )
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(t)
    await db.commit()


@router.post("/templates/{template_id}/rate", response_model=TemplateRateResponse)
async def rate_template(
    template_id: UUID,
    req: TemplateRateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_MARKETPLACE_WRITE)),
):
    """Rate a template 1–5 stars. Updates your existing rating if already rated."""
    if not (1 <= req.rating <= 5):
        raise HTTPException(status_code=400, detail="Rating must be 1–5")

    # Lock the template row before reading rating_sum/rating_count to prevent
    # concurrent ratings from causing a lost-update on the aggregate counters.
    uid = UUID(user_id)
    t_result = await db.execute(
        select(TaskTemplateDB).where(
            TaskTemplateDB.id == template_id,
            or_(
                TaskTemplateDB.is_public == True,
                TaskTemplateDB.creator_id == uid,
            ),
        ).with_for_update()
    )
    t = t_result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")

    # Check for existing rating
    existing_result = await db.execute(
        select(TaskTemplateRatingDB).where(
            TaskTemplateRatingDB.template_id == template_id,
            TaskTemplateRatingDB.user_id == UUID(user_id),
        )
    )
    existing = existing_result.scalar_one_or_none()

    if existing:
        # Update rating delta
        delta = req.rating - existing.rating
        t.rating_sum += delta
        existing.rating = req.rating
    else:
        # New rating
        rating_obj = TaskTemplateRatingDB(
            id=uuid4(),
            template_id=template_id,
            user_id=UUID(user_id),
            rating=req.rating,
        )
        db.add(rating_obj)
        t.rating_sum += req.rating
        t.rating_count += 1

    await db.commit()
    await db.refresh(t)

    new_avg = round(t.rating_sum / t.rating_count, 1) if t.rating_count > 0 else None
    return TemplateRateResponse(
        template_id=template_id,
        your_rating=req.rating,
        new_avg=new_avg,
        total_ratings=t.rating_count,
    )


@router.post("/templates/{template_id}/use", status_code=200)
async def use_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_MARKETPLACE_READ)),
):
    """Mark a template as used (increment use_count) and return its task_config for pre-filling."""
    t = await _get_template(template_id, user_id, db)
    t.use_count += 1
    t.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "template_id": str(template_id),
        "task_type": t.task_type,
        "execution_mode": t.execution_mode,
        "task_config": t.task_config,
        "example_input": t.example_input,
    }


@router.post("/templates/{template_id}/clone-task", status_code=201)
async def clone_template_as_task(
    template_id: UUID,
    title: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_TASKS_WRITE)),
) -> dict:
    """
    One-click: create a task directly from a template's config.
    Returns the new task ID so the client can redirect to it.
    """
    from models.db import TaskDB, CreditTransactionDB
    from core.quotas import enforce_task_creation_quota, record_task_creation

    uid = UUID(user_id)
    t = await _get_template(template_id, user_id, db)

    # Pull config values
    cfg = t.task_config or {}
    task_type = t.task_type
    execution_mode = t.execution_mode or "ai"
    credits_cost = cfg.get("credits_cost", 1)
    reward_credits = cfg.get("reward_credits", 2)
    workers = cfg.get("workers", 1)
    timeout = cfg.get("timeout_minutes", 30)
    instructions = cfg.get("instructions", "")

    # Load user with row-level lock to prevent concurrent clone requests racing
    # on the credits balance check (read-then-deduct pattern).
    user_res = await db.execute(select(UserDB).where(UserDB.id == uid).with_for_update())
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    if execution_mode == "ai" and user.credits < credits_cost:
        raise HTTPException(
            402,
            f"Insufficient credits. Need {credits_cost}, have {user.credits}.",
        )

    # Check quota
    await enforce_task_creation_quota(uid, db)

    # Create task
    task_input = t.example_input or {"_template": str(template_id)}
    task = TaskDB(
        user_id=uid,
        type=task_type,
        status="pending" if execution_mode == "ai" else "open",
        execution_mode=execution_mode,
        input=task_input,
        task_instructions=instructions or f"Task from template: {t.name}",
        worker_reward_credits=reward_credits if execution_mode == "human" else None,
        assignments_required=workers if execution_mode == "human" else 1,
        claim_timeout_minutes=timeout,
        credits_used=credits_cost if execution_mode == "ai" else 0,
        metadata={"template_id": str(template_id), "template_name": t.name},
    )
    db.add(task)

    # Charge credits for AI tasks
    if execution_mode == "ai" and credits_cost > 0:
        user.credits -= credits_cost
        txn = CreditTransactionDB(
            user_id=uid,
            amount=-credits_cost,
            type="charge",
            description=f"Task from template: {t.name}",
        )
        db.add(txn)

    # Increment template use_count
    t.use_count += 1
    t.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(task)
    await record_task_creation(uid, db)

    logger.info("template_cloned_as_task",
                template_id=str(template_id), task_id=str(task.id), user_id=user_id)

    return {
        "task_id": str(task.id),
        "task_type": task_type,
        "execution_mode": execution_mode,
        "status": task.status,
        "template_name": t.name,
        "message": f"Task created from template '{t.name}'",
    }


@router.get("/categories", response_model=list[dict])
async def list_categories(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_scope(SCOPE_MARKETPLACE_READ)),
):
    """Return all distinct template categories with counts."""
    await _ensure_system_templates(db)

    result = await db.execute(
        select(TaskTemplateDB.category, func.count().label("count"))
        .where(TaskTemplateDB.is_public == True, TaskTemplateDB.category.isnot(None))
        .group_by(TaskTemplateDB.category)
        .order_by(func.count().desc())
    )
    rows = result.all()
    return [{"category": r[0], "count": r[1]} for r in rows]


# ── Helper ────────────────────────────────────────────────────────────────────

async def _get_template(
    template_id: UUID,
    user_id: str,
    db: AsyncSession,
) -> TaskTemplateDB:
    uid = UUID(user_id)
    result = await db.execute(
        select(TaskTemplateDB).where(
            TaskTemplateDB.id == template_id,
            or_(
                TaskTemplateDB.is_public == True,
                TaskTemplateDB.creator_id == uid,
            ),
        )
    )
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return t
