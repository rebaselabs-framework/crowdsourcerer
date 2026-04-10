"""System health monitoring and admin alerting.

Tracks:
  - HTTP 5xx error rate (rolling window) — fires "error_rate_spike" alert
  - Sweeper stall detection — fires "sweeper_stall" alert
  - Consecutive sweeper errors — fires "sweeper_errors" alert

Alerts are:
  - Persisted to the `system_alerts` DB table
  - Emailed to ADMIN_EMAIL (gated on EMAIL_ENABLED)
  - Rate-limited by a per-type cooldown to prevent spam

Usage:
  # In request middleware:
  record_http_error(path="/v1/...", status_code=500)

  # In sweeper loop (once per sweep):
  await check_and_fire_alerts(session_factory)
  record_sweep_success()      # call after a successful sweep
  record_sweep_error()        # call after a sweep that raised
"""

import asyncio
import uuid
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.background import safe_create_task
from core.config import get_settings

logger = structlog.get_logger()
_settings = get_settings()

# ─── Module-level state ────────────────────────────────────────────────────

# Rolling window of (timestamp, path) for 5xx errors
_error_window: deque[tuple[datetime, str]] = deque()

# Consecutive sweeper errors counter
_sweep_consecutive_errors: int = 0

# Last alert sent per type: alert_type → datetime
_alert_cooldowns: dict[str, datetime] = {}


# ─── Ingestion helpers ─────────────────────────────────────────────────────

def record_http_error(path: str, status_code: int) -> None:
    """Called from request middleware for every 5xx response."""
    if status_code < 500:
        return
    _error_window.append((datetime.now(timezone.utc), path))


def record_sweep_success() -> None:
    """Reset consecutive error counter after a clean sweep."""
    global _sweep_consecutive_errors  # noqa: PLW0603
    _sweep_consecutive_errors = 0


def record_sweep_error() -> None:
    """Increment consecutive error counter when the sweeper raises."""
    global _sweep_consecutive_errors  # noqa: PLW0603
    _sweep_consecutive_errors += 1


# ─── Core alert check (called from sweeper each cycle) ────────────────────

async def check_and_fire_alerts(session_factory: async_sessionmaker) -> None:
    """Evaluate all health signals and fire alerts as needed."""
    now = datetime.now(timezone.utc)
    window_minutes = _settings.alert_error_rate_window_minutes
    cutoff = now - timedelta(minutes=window_minutes)

    # Prune stale entries from the error window
    while _error_window and _error_window[0][0] < cutoff:
        _error_window.popleft()

    error_count = len(_error_window)

    # --- Check 1: error rate spike ---
    if error_count >= _settings.alert_error_rate_threshold:
        if _is_cooled_down("error_rate_spike", now):
            sample_paths = list({p for _, p in _error_window})[:5]
            await _fire_alert(
                session_factory=session_factory,
                alert_type="error_rate_spike",
                severity="critical",
                title=f"🔴 Error rate spike: {error_count} errors in {window_minutes}m",
                detail={
                    "error_count": error_count,
                    "window_minutes": window_minutes,
                    "sample_paths": sample_paths,
                    "threshold": _settings.alert_error_rate_threshold,
                },
            )

    # --- Check 2: sweeper stall ---
    from core.sweeper import _LAST_SWEEP_AT  # local import to avoid circular  # noqa: PLC0415
    if _LAST_SWEEP_AT is not None:
        minutes_since_sweep = (now - _LAST_SWEEP_AT).total_seconds() / 60
        if minutes_since_sweep >= _settings.alert_sweeper_stall_minutes:
            if _is_cooled_down("sweeper_stall", now):
                await _fire_alert(
                    session_factory=session_factory,
                    alert_type="sweeper_stall",
                    severity="critical",
                    title=f"⚠️ Sweeper stall: last run {minutes_since_sweep:.0f}m ago",
                    detail={
                        "minutes_since_last_sweep": round(minutes_since_sweep, 1),
                        "last_sweep_at": _LAST_SWEEP_AT.isoformat(),
                        "threshold_minutes": _settings.alert_sweeper_stall_minutes,
                    },
                )

    # --- Check 3: consecutive sweeper errors ---
    if _sweep_consecutive_errors >= 3:
        if _is_cooled_down("sweeper_consecutive_errors", now):
            await _fire_alert(
                session_factory=session_factory,
                alert_type="sweeper_consecutive_errors",
                severity="warning",
                title=f"⚠️ Sweeper has failed {_sweep_consecutive_errors} times in a row",
                detail={
                    "consecutive_errors": _sweep_consecutive_errors,
                },
            )


# ─── Internal helpers ──────────────────────────────────────────────────────

def _is_cooled_down(alert_type: str, now: datetime) -> bool:
    """Return True if enough time has passed since this alert type last fired."""
    last = _alert_cooldowns.get(alert_type)
    if last is None:
        return True
    cooldown = timedelta(hours=_settings.alert_cooldown_hours)
    return (now - last) >= cooldown


async def _fire_alert(
    session_factory: async_sessionmaker,
    alert_type: str,
    severity: str,
    title: str,
    detail: dict,
) -> None:
    """Persist alert to DB, send email, and update cooldown."""
    now = datetime.now(timezone.utc)
    _alert_cooldowns[alert_type] = now

    logger.warning(
        "system_alert.fired",
        alert_type=alert_type,
        severity=severity,
        title=title,
    )

    # Persist to DB
    alert_id = uuid.uuid4()
    notified_at: Optional[datetime] = None
    try:
        from models.db import SystemAlertDB  # local import to avoid circular  # noqa: PLC0415
        async with session_factory() as db:
            alert = SystemAlertDB(
                id=alert_id,
                alert_type=alert_type,
                severity=severity,
                title=title,
                detail=detail,
            )
            db.add(alert)
            await db.commit()
    except Exception:
        logger.exception("system_alert.db_error", alert_type=alert_type)

    # Send email (fire-and-forget)
    safe_create_task(
        _send_alert_email(alert_type, severity, title, detail),
        name="email.system_alert",
    )


async def _send_alert_email(
    alert_type: str,
    severity: str,
    title: str,
    detail: dict,
) -> None:
    """Send an alert email to the configured admin address."""
    from core.email import _send_email  # local import  # noqa: PLC0415

    settings = get_settings()
    if not settings.email_enabled:
        logger.info("system_alert.email_skipped", reason="email_not_enabled", alert_type=alert_type)
        return

    recipient = settings.admin_email or settings.smtp_from
    if not recipient:
        logger.info("system_alert.email_skipped", reason="no_admin_email", alert_type=alert_type)
        return

    subject = f"[CrowdSorcerer] {title}"
    html = _system_alert_html(alert_type, severity, title, detail)
    await _send_email(to=recipient, subject=subject, html=html)
    logger.info("system_alert.email_sent", alert_type=alert_type, recipient=recipient)


# ─── Email template ────────────────────────────────────────────────────────

def _system_alert_html(alert_type: str, severity: str, title: str, detail: dict) -> str:
    severity_color = "#ef4444" if severity == "critical" else "#f59e0b"
    severity_label = "CRITICAL" if severity == "critical" else "WARNING"
    site_url = get_settings().public_site_url
    site_host = site_url.removeprefix("https://").removeprefix("http://")

    detail_rows = ""
    for key, value in detail.items():
        label = key.replace("_", " ").title()
        if isinstance(value, list):
            value_str = ", ".join(str(v) for v in value) or "—"
        else:
            value_str = str(value)
        bg = "#f8f9fa" if len(detail_rows) % 2 == 0 else "white"
        detail_rows += f"""
  <tr>
    <td style="padding:8px 12px;background:{bg};font-weight:600;white-space:nowrap;width:40%">{label}</td>
    <td style="padding:8px 12px;background:{bg};font-family:monospace">{value_str}</td>
  </tr>"""

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#111827">

  <div style="border-left:4px solid {severity_color};padding:12px 16px;background:{severity_color}11;margin-bottom:24px;border-radius:0 6px 6px 0">
    <span style="background:{severity_color};color:white;font-size:11px;font-weight:700;letter-spacing:.05em;padding:2px 8px;border-radius:4px;margin-right:8px">{severity_label}</span>
    <strong style="font-size:16px">{title}</strong>
  </div>

  <table style="width:100%;border-collapse:collapse;border-radius:8px;overflow:hidden;border:1px solid #e5e7eb;margin-bottom:24px">
    <thead>
      <tr style="background:#f3f4f6">
        <th colspan="2" style="padding:10px 12px;text-align:left;font-size:12px;color:#6b7280;letter-spacing:.05em;text-transform:uppercase">Alert Details</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td style="padding:8px 12px;font-weight:600;width:40%">Alert Type</td>
        <td style="padding:8px 12px;font-family:monospace;color:#6366f1">{alert_type}</td>
      </tr>
      <tr>
        <td style="padding:8px 12px;background:#f8f9fa;font-weight:600">Time</td>
        <td style="padding:8px 12px;background:#f8f9fa">{now_str}</td>
      </tr>
      {detail_rows}
    </tbody>
  </table>

  <p>
    <a href="{site_url}/admin/alerts"
       style="background:#6366f1;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:600">
      View Alert Dashboard →
    </a>
  </p>

  <hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
  <p style="color:#9ca3af;font-size:12px;margin:0">
    CrowdSorcerer System Monitor &middot;
    <a href="{site_url}" style="color:#9ca3af">{site_host}</a>
  </p>
</body>
</html>
"""
