"""Task result cache — deduplicates identical AI task runs.

How it works
------------
Before executing an AI task via RebaseKit, we compute a stable SHA-256 hash
of (task_type, canonical-JSON(input)).  If a matching, non-expired entry exists
in ``task_result_cache``, we return the cached output immediately and skip the
external API call entirely.

After a successful run (cache miss), we store the result so future identical
calls can benefit.

Credit economics
----------------
* Cache **miss** (first run)  → charged at the normal full rate.
* Cache **hit** (repeat run)  → charged only ``CACHE_HIT_FEE_CREDITS`` (default 1).
  The requester gets an instant refund of (full_cost - fee) credits.

Task types and TTLs
-------------------
Some task types return time-sensitive data (e.g. ``web_research`` scrapes a
live URL) so their cache entries expire sooner.  TTLs are configurable via
``Settings.cache_ttl_*``.  A TTL of 0 means "never expire".

The ``task_result_cache_enabled`` config flag lets you disable caching globally
without redeploying (useful when debugging RebaseKit issues).
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import structlog
from sqlalchemy import select, func, delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import TaskResultCacheDB

logger = structlog.get_logger()

# Credits charged for a cache hit (small fee to cover DB lookup overhead)
CACHE_HIT_FEE_CREDITS: int = 1

# Per-type TTL in hours (0 = never expire)
_DEFAULT_TTL_HOURS: dict[str, int] = {
    "web_research": 1,      # Live web content stales quickly
    "screenshot": 2,        # Pages change
    "web_intel": 2,         # Live intelligence data
    "audio_transcribe": 0,  # Deterministic — fine to cache indefinitely
    "document_parse": 0,    # Same document → same parse
    "data_transform": 0,    # Pure function
    "llm_generate": 6,      # Prompt → output; model may update
    "entity_lookup": 4,     # Entity data changes slowly
    "pii_detect": 0,        # Deterministic
    "code_execute": 0,      # Deterministic
}


def _input_hash(task_type: str, task_input: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex digest for (task_type, input).

    We sort all dict keys recursively so that ``{"b": 1, "a": 2}`` and
    ``{"a": 2, "b": 1}`` produce the same hash.
    """
    canonical = json.dumps(
        {"task_type": task_type, "input": task_input},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _ttl_hours(task_type: str) -> int:
    """Return the TTL in hours for a given task type."""
    from core.config import get_settings
    settings = get_settings()
    # Settings can override per-type or globally
    override = getattr(settings, f"cache_ttl_{task_type}", None)
    if override is not None:
        return int(override)
    return _DEFAULT_TTL_HOURS.get(task_type, 6)


async def cache_lookup(
    db: AsyncSession,
    task_type: str,
    task_input: dict[str, Any],
) -> TaskResultCacheDB | None:
    """Return a valid (non-expired) cache entry, or None on miss.

    Also increments ``hit_count`` and ``last_hit_at`` on a hit — fire-and-
    forget style (we commit the update after returning to avoid blocking the
    caller).
    """
    from core.config import get_settings
    if not get_settings().task_result_cache_enabled:
        return None

    h = _input_hash(task_type, task_input)
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(TaskResultCacheDB).where(
            TaskResultCacheDB.task_type == task_type,
            TaskResultCacheDB.input_hash == h,
        )
    )
    entry = result.scalar_one_or_none()

    if entry is None:
        return None

    # Check expiry
    if entry.expires_at is not None and entry.expires_at <= now:
        logger.info("cache_expired", task_type=task_type, input_hash=h)
        await db.delete(entry)
        await db.commit()
        return None

    # Update hit stats (best-effort — don't let a stats write block the response)
    try:
        entry.hit_count += 1
        entry.last_hit_at = now
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()

    logger.info("cache_hit", task_type=task_type, input_hash=h, hit_count=entry.hit_count)
    return entry


async def cache_store(
    db: AsyncSession,
    task_type: str,
    task_input: dict[str, Any],
    output: dict[str, Any],
    full_credits_cost: int,
    duration_ms: int | None = None,
) -> None:
    """Persist a successful task result to the cache (upsert).

    Silently skips if caching is disabled or if the entry already exists
    (another concurrent task may have stored it first).
    """
    from core.config import get_settings
    if not get_settings().task_result_cache_enabled:
        return

    h = _input_hash(task_type, task_input)
    ttl_h = _ttl_hours(task_type)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(hours=ttl_h)) if ttl_h > 0 else None

    try:
        # Check for existing entry first (handle UniqueConstraint gracefully)
        existing_result = await db.execute(
            select(TaskResultCacheDB).where(
                TaskResultCacheDB.task_type == task_type,
                TaskResultCacheDB.input_hash == h,
            )
        )
        existing = existing_result.scalar_one_or_none()

        if existing:
            # Update the cached output + refresh TTL
            existing.output = output
            existing.full_credits_cost = full_credits_cost
            existing.duration_ms = duration_ms
            existing.expires_at = expires_at
        else:
            entry = TaskResultCacheDB(
                id=uuid.uuid4(),
                task_type=task_type,
                input_hash=h,
                output=output,
                full_credits_cost=full_credits_cost,
                duration_ms=duration_ms,
                expires_at=expires_at,
            )
            db.add(entry)

        await db.commit()
        logger.info("cache_stored", task_type=task_type, input_hash=h, ttl_hours=ttl_h)
    except SQLAlchemyError:
        await db.rollback()
        logger.warning("cache_store_failed", task_type=task_type, input_hash=h, exc_info=True)


async def cache_stats(db: AsyncSession) -> dict[str, Any]:
    """Return aggregate statistics for the admin dashboard."""
    now = datetime.now(timezone.utc)

    total_result = await db.execute(select(func.count()).select_from(TaskResultCacheDB))
    total = total_result.scalar() or 0

    expired_result = await db.execute(
        select(func.count()).select_from(TaskResultCacheDB).where(
            TaskResultCacheDB.expires_at.isnot(None),
            TaskResultCacheDB.expires_at <= now,
        )
    )
    expired = expired_result.scalar() or 0

    hits_result = await db.execute(select(func.sum(TaskResultCacheDB.hit_count)))
    total_hits = int(hits_result.scalar() or 0)

    # Credits saved = sum over entries of (hit_count * (full_cost - CACHE_HIT_FEE))
    # Compute as: sum(hit_count * full_credits_cost) - total_hits * CACHE_HIT_FEE
    savings_result = await db.execute(
        select(func.sum(TaskResultCacheDB.hit_count * TaskResultCacheDB.full_credits_cost))
    )
    gross = int(savings_result.scalar() or 0)
    credits_saved = max(0, gross - (total_hits * CACHE_HIT_FEE_CREDITS))

    # Per-type breakdown
    type_result = await db.execute(
        select(
            TaskResultCacheDB.task_type,
            func.count().label("entries"),
            func.sum(TaskResultCacheDB.hit_count).label("hits"),
        ).group_by(TaskResultCacheDB.task_type).order_by(
            func.sum(TaskResultCacheDB.hit_count).desc()
        )
    )
    by_type = [
        {"task_type": row.task_type, "entries": row.entries, "hits": int(row.hits or 0)}
        for row in type_result.all()
    ]

    return {
        "total_entries": total,
        "expired_entries": expired,
        "total_hits": total_hits,
        "credits_saved": credits_saved,
        "cache_hit_fee": CACHE_HIT_FEE_CREDITS,
        "by_type": by_type,
    }


async def cache_flush(
    db: AsyncSession,
    task_type: str | None = None,
    expired_only: bool = False,
) -> int:
    """Delete cache entries.  Returns count of deleted rows.

    * ``task_type`` — only flush entries of this type (None = all types)
    * ``expired_only`` — only remove entries whose TTL has elapsed
    """
    now = datetime.now(timezone.utc)
    stmt = delete(TaskResultCacheDB)

    conditions = []
    if task_type:
        conditions.append(TaskResultCacheDB.task_type == task_type)
    if expired_only:
        conditions.append(TaskResultCacheDB.expires_at.isnot(None))
        conditions.append(TaskResultCacheDB.expires_at <= now)

    if conditions:
        from sqlalchemy import and_
        stmt = stmt.where(and_(*conditions))

    result = await db.execute(stmt)
    await db.commit()
    deleted = result.rowcount or 0
    logger.info("cache_flushed", task_type=task_type, expired_only=expired_only, deleted=deleted)
    return deleted
