"""RebaseKit service health monitoring with TTL cache.

Provides live health status for all RebaseKit backend services.
Results are cached with a configurable TTL (default 60s) to avoid
hammering health endpoints on every request.

Usage:
    from core.rebasekit_health import get_service_health, get_ai_health_status

    status = await get_service_health()
    # {"pii": True, "llm": False, "webtask": False, ...}

    match await get_ai_health_status():
        case "healthy":
            ...   # all workers up
        case "degraded":
            ...   # some workers up, warn the user
        case "unavailable":
            ...   # no workers reachable, fall back / block
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

import httpx
import structlog

from core.config import get_settings

logger = structlog.get_logger()

# Public status enum. Three tiers avoid the historical lie where one service
# up out of ten reported ai_available=True to the frontend.
AIHealthStatus = Literal["healthy", "degraded", "unavailable"]


@dataclass(frozen=True, slots=True)
class AIHealthSummary:
    """Snapshot of overall AI worker health.

    Used by /v1/config and admin tooling so the UI can render a
    "some task types unavailable" banner without re-deriving counts.
    """

    status: AIHealthStatus
    services_up: int
    services_total: int
    services: dict[str, bool]

    @property
    def is_available(self) -> bool:
        """True whenever at least one backing service is reachable."""
        return self.status != "unavailable"

# ── Service mapping ──────────────────────────────────────────────────────

# Map each AI task type to the RebaseKit service it depends on.
TASK_TO_SERVICE: dict[str, str] = {
    "web_research": "webtask",
    "entity_lookup": "enrich",
    "document_parse": "docparse",
    "data_transform": "transform",
    "llm_generate": "llm",
    "screenshot": "screenshot",
    "audio_transcribe": "audio",
    "pii_detect": "pii",
    "code_execute": "code",
    "web_intel": "webtask",  # shares webtask service
}

# Unique set of services to health-check.
ALL_SERVICES: list[str] = sorted(set(TASK_TO_SERVICE.values()))

# ── Configuration ────────────────────────────────────────────────────────

CACHE_TTL = 60          # seconds between health probes
CHECK_TIMEOUT = 5.0     # per-service HTTP timeout


# ── Health cache ─────────────────────────────────────────────────────────

class RebaseKitHealthCache:
    """Async-safe TTL cache for RebaseKit service health."""

    def __init__(self):
        self._status: dict[str, bool] = {}
        self._last_check: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def is_stale(self) -> bool:
        return time.monotonic() - self._last_check > CACHE_TTL

    async def get_status(self) -> dict[str, bool]:
        """Return cached service health, refreshing if stale."""
        if self.is_stale:
            async with self._lock:
                # Double-check after acquiring lock (another task may have refreshed)
                if self.is_stale:
                    await self._refresh()
        return dict(self._status)

    async def force_refresh(self) -> dict[str, bool]:
        """Force an immediate health check, ignoring cache."""
        async with self._lock:
            await self._refresh()
        return dict(self._status)

    async def _refresh(self):
        """Ping all RebaseKit service health endpoints concurrently."""
        settings = get_settings()

        if not settings.rebasekit_api_key:
            self._status = {svc: False for svc in ALL_SERVICES}
            self._last_check = time.monotonic()
            return

        base_url = settings.rebasekit_base_url.rstrip("/")

        async def _check_one(service: str) -> tuple[str, bool]:
            url = f"{base_url}/{service}/api/health"
            try:
                async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
                    r = await client.get(url)
                    # 200 = healthy; 401 = service is up but needs auth (still "up")
                    return service, r.status_code in (200, 401)
            except (httpx.HTTPError, OSError):
                return service, False

        results = await asyncio.gather(
            *[_check_one(svc) for svc in ALL_SERVICES],
            return_exceptions=True,
        )

        new_status: dict[str, bool] = {}
        for result in results:
            if isinstance(result, BaseException):
                continue
            svc, up = result
            new_status[svc] = up

        # Fill in any missing services as down
        for svc in ALL_SERVICES:
            if svc not in new_status:
                new_status[svc] = False

        self._status = new_status
        self._last_check = time.monotonic()

        up_count = sum(1 for v in new_status.values() if v)
        logger.info(
            "rebasekit_health_check",
            up=up_count,
            total=len(ALL_SERVICES),
            services=new_status,
        )


# ── Module-level singleton ───────────────────────────────────────────────

_cache = RebaseKitHealthCache()


async def get_service_health() -> dict[str, bool]:
    """Get cached health status of all RebaseKit services.

    Returns dict mapping service name → bool (True = up).
    """
    return await _cache.get_status()


async def get_ai_health_summary() -> AIHealthSummary:
    """Compute the canonical AI-side health snapshot.

    The three-tier status replaces the old "any service up" bool:

    - ``healthy``     — API key present and **all** services reachable.
    - ``degraded``    — API key present, at least one service up, but
                        not all of them. Task types backed by downed
                        services will still fail.
    - ``unavailable`` — no API key configured, or every service is down.

    This is the single source of truth; other helpers
    (:func:`is_ai_available`, :func:`get_ai_health_status`) are thin
    wrappers so callers can pick the shape that fits.
    """
    settings = get_settings()
    if not settings.rebasekit_api_key:
        return AIHealthSummary(
            status="unavailable",
            services_up=0,
            services_total=len(ALL_SERVICES),
            services={svc: False for svc in ALL_SERVICES},
        )

    services = await _cache.get_status()
    total = len(ALL_SERVICES)
    up = sum(1 for svc in ALL_SERVICES if services.get(svc, False))

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
        services={svc: services.get(svc, False) for svc in ALL_SERVICES},
    )


async def get_ai_health_status() -> AIHealthStatus:
    """Return just the three-tier status string (see :class:`AIHealthSummary`)."""
    summary = await get_ai_health_summary()
    return summary.status


async def is_ai_available() -> bool:
    """True whenever the integration is configured and ≥1 worker is reachable.

    Retained for backwards compatibility — new code should prefer
    :func:`get_ai_health_status` or :func:`get_ai_health_summary` which
    distinguish a fully healthy fleet from a degraded one.
    """
    summary = await get_ai_health_summary()
    return summary.is_available


async def get_available_task_types() -> dict[str, bool]:
    """Return availability flag for each AI task type.

    Maps task type name → bool based on whether its backing service is up.
    """
    status = await _cache.get_status()
    return {
        task_type: status.get(service, False)
        for task_type, service in TASK_TO_SERVICE.items()
    }


async def warmup():
    """Pre-populate the cache on application startup."""
    try:
        await _cache.force_refresh()
    except Exception as e:
        logger.warning("rebasekit_health_warmup_failed", error=str(e))


def get_cache() -> RebaseKitHealthCache:
    """Return the global cache instance (for testing)."""
    return _cache
