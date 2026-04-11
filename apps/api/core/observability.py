"""Sentry error-tracking initialisation.

Called once from FastAPI's lifespan hook. When ``settings.sentry_dsn``
is empty the init is a zero-cost no-op so local dev, CI, and tests
never emit events.

Hooks installed:

- FastAPI integration — automatic request / response / exception
  capture for every route.
- asyncio integration — captures unhandled exceptions in background
  tasks (sweeper, webhook retry worker).
- httpx integration — breadcrumbs for outbound calls to RebaseKit,
  Stripe, and the SMTP-over-HTTP providers.
- Logger integration at WARNING level — narrowed ``except
  SQLAlchemyError`` / ``except httpx.HTTPError`` branches that call
  ``logger.warning(...)`` or ``logger.error(...)`` automatically
  turn into Sentry breadcrumbs.

``before_send`` strips known-safe paths (``/health``, ``/v1/health``,
favicon) so uptime-monitor traffic doesn't burn the quota.
"""

from __future__ import annotations

from typing import Any

import structlog

from core.config import Settings

logger = structlog.get_logger()

# Routes we never want to ship events for. Uptime monitors hit these
# multiple times per minute and the signal-to-noise ratio is zero.
_SILENCED_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/health/ready",
        "/v1/health",
        "/favicon.ico",
    }
)


def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    """Drop events originating from silenced health-probe routes."""
    request = event.get("request") or {}
    url = request.get("url", "")
    for path in _SILENCED_PATHS:
        if url.endswith(path):
            return None
    return event


def init_sentry(settings: Settings) -> bool:
    """Initialise Sentry if a DSN is configured.

    Returns True when Sentry was actually initialised, False when it
    was skipped (empty DSN → silent no-op). Callers can log the
    outcome on startup.
    """
    if not settings.sentry_dsn:
        return False

    # Imported lazily so projects that don't set a DSN never pay the
    # ~20ms cost of loading the SDK and its integrations.
    import sentry_sdk
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.httpx import HttpxIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        release=f"crowdsourcerer@{settings.app_version}",
        traces_sample_rate=settings.sentry_traces_sample_rate,
        profiles_sample_rate=settings.sentry_profiles_sample_rate,
        send_default_pii=False,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
            AsyncioIntegration(),
            HttpxIntegration(),
            LoggingIntegration(level=None, event_level=None),
        ],
        before_send=_before_send,
    )
    logger.info(
        "sentry.initialised",
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
    )
    return True


__all__ = ["init_sentry"]
