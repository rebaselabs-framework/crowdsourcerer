"""AI task-type health monitoring.

Historical name (``rebasekit_health``) is kept for backward compat
with call sites that already import it. All task handlers now run
in-process or talk directly to Anthropic, so "health" reduces to:

- **Local handlers** (``document_parse``, ``pii_detect``, ``code_execute``)
  are unconditionally ``healthy`` — they have no external dependency,
  so the only way they fail is a code bug.
- **LLM handlers** (``llm_generate``, ``data_transform``,
  ``web_research``) are ``healthy`` iff at least one LLM provider
  key is configured — Anthropic, Gemini, or OpenAI. The actual
  provider is selected by :func:`core.llm_client.get_llm_client`;
  this module only cares whether *some* key is present. We don't
  ping the provider on every probe because the cost is non-trivial
  and rate-limited.

The public surface
(:func:`get_service_health`, :func:`get_ai_health_summary`,
:func:`is_ai_available`, :func:`get_available_task_types`,
:func:`warmup`) is unchanged so the endpoints + tests + frontend
don't need to change.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

import structlog

from core.config import get_settings

logger = structlog.get_logger()

AIHealthStatus = Literal["healthy", "degraded", "unavailable"]


# ── Capability model ─────────────────────────────────────────────────

# Re-exported from :mod:`core.task_types` so existing callers and tests
# that import these names from here keep working. The canonical lists
# live in core.task_types and are derived from the TASK_METADATA table.
from core.task_types import (
    LOCAL_TASK_TYPES,
    LLM_TASK_TYPES,
    AI_TASK_TYPES as ALL_TASK_TYPES,
)

# Legacy names kept so older imports don't break. The previous
# implementation had a per-service mapping; the new layout uses
# a simpler "local" / "llm" split, so TASK_TO_SERVICE now just
# maps every task type to one of those two buckets.
TASK_TO_SERVICE: dict[str, str] = {
    **{t: "local" for t in sorted(LOCAL_TASK_TYPES)},
    **{t: "llm" for t in sorted(LLM_TASK_TYPES)},
}
ALL_SERVICES: list[str] = sorted(set(TASK_TO_SERVICE.values()))


@dataclass(frozen=True, slots=True)
class AIHealthSummary:
    status: AIHealthStatus
    services_up: int
    services_total: int
    services: dict[str, bool]

    @property
    def is_available(self) -> bool:
        return self.status != "unavailable"


# ── Cache ─────────────────────────────────────────────────────────────

CACHE_TTL = 60


class RebaseKitHealthCache:
    """Async-safe TTL cache. Kept under its historical name so existing
    tests that import ``RebaseKitHealthCache`` still work."""

    def __init__(self) -> None:
        self._status: dict[str, bool] = {svc: False for svc in ALL_SERVICES}
        self._last_check: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def is_stale(self) -> bool:
        return time.monotonic() - self._last_check > CACHE_TTL

    async def get_status(self) -> dict[str, bool]:
        if self.is_stale:
            async with self._lock:
                if self.is_stale:
                    await self._refresh()
        return dict(self._status)

    async def force_refresh(self) -> dict[str, bool]:
        async with self._lock:
            await self._refresh()
        return dict(self._status)

    async def _refresh(self) -> None:
        """Recompute service status from config. No network calls —
        local handlers are always up, LLM is up iff any provider key
        (Anthropic / Gemini / OpenAI) is configured."""
        settings = get_settings()
        llm_up = any(
            (
                bool(settings.anthropic_api_key),
                bool(settings.gemini_api_key),
                bool(settings.openai_api_key),
            )
        )
        self._status = {
            "local": True,
            "llm": llm_up,
        }
        self._last_check = time.monotonic()
        logger.info(
            "ai_health_refresh",
            local=True,
            llm=llm_up,
            provider=settings.llm_provider or "auto",
        )
        if not llm_up:
            logger.warning(
                "llm_task_types_disabled",
                message=(
                    "No LLM provider key configured — set one of "
                    "ANTHROPIC_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY. "
                    "llm_generate / data_transform / web_research are "
                    "currently unavailable."
                ),
            )


# ── Module-level singleton ────────────────────────────────────────────

_cache = RebaseKitHealthCache()


async def get_service_health() -> dict[str, bool]:
    return await _cache.get_status()


async def get_ai_health_summary() -> AIHealthSummary:
    """Overall AI capability snapshot.

    ``healthy``     = both local and LLM available.
    ``degraded``    = only local available (LLM key missing).
    ``unavailable`` = neither — but local handlers have no key
                      requirement, so this state is effectively
                      unreachable in production and only triggers if
                      the cache/refresh is actively failing.
    """
    services = await _cache.get_status()
    up = sum(1 for v in services.values() if v)
    total = len(services)

    if up == 0:
        status: AIHealthStatus = "unavailable"
    elif up < total:
        status = "degraded"
    else:
        status = "healthy"

    return AIHealthSummary(
        status=status,
        services_up=up,
        services_total=total,
        services=dict(services),
    )


async def get_ai_health_status() -> AIHealthStatus:
    return (await get_ai_health_summary()).status


async def is_ai_available() -> bool:
    return (await get_ai_health_summary()).is_available


async def get_available_task_types() -> dict[str, bool]:
    """Return a map ``{task_type: available}`` for every AI task type."""
    services = await _cache.get_status()
    return {
        task_type: services.get(bucket, False)
        for task_type, bucket in TASK_TO_SERVICE.items()
    }


async def warmup() -> None:
    """Pre-populate the cache on startup. Never raises."""
    try:
        await _cache.force_refresh()
    except Exception as exc:  # noqa: BLE001 — startup must not crash
        logger.warning("ai_health_warmup_failed", error=str(exc))


def get_cache() -> RebaseKitHealthCache:
    return _cache


__all__ = [
    "AIHealthStatus",
    "AIHealthSummary",
    "ALL_SERVICES",
    "ALL_TASK_TYPES",
    "LLM_TASK_TYPES",
    "LOCAL_TASK_TYPES",
    "RebaseKitHealthCache",
    "TASK_TO_SERVICE",
    "get_ai_health_status",
    "get_ai_health_summary",
    "get_available_task_types",
    "get_cache",
    "get_service_health",
    "is_ai_available",
    "warmup",
]
