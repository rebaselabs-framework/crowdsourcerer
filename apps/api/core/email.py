"""Email notification service for CrowdSorcerer.

Sends transactional emails for key platform events:
  - Task completed (to requester)
  - Task failed (to requester)
  - Worker submission received (to requester, if auto-notify enabled)
  - Daily challenge available (to opted-in workers)

Configuration (via env vars / Settings):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM
  EMAIL_ENABLED=true/false (default: false until configured)

If EMAIL_ENABLED is false, emails are logged but not sent (safe default).
"""
from __future__ import annotations

import asyncio
import email.mime.multipart
import email.mime.text
import smtplib
import ssl
from typing import Optional

import structlog
from sqlalchemy import select

from core.config import get_settings

logger = structlog.get_logger()
_settings = get_settings()


# ─── Email templates ───────────────────────────────────────────────────────

def _task_completed_html(task_id: str, task_type: str, output_summary: str) -> str:
    return f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
<h2 style="color:#6366f1">✅ Task Completed</h2>
<p>Your <strong>{task_type}</strong> task has finished successfully.</p>
<table style="width:100%;border-collapse:collapse;margin:16px 0">
  <tr><td style="padding:8px;background:#f8f9fa;font-weight:bold;width:120px">Task ID</td>
      <td style="padding:8px">{task_id}</td></tr>
  <tr><td style="padding:8px;font-weight:bold">Type</td>
      <td style="padding:8px">{task_type}</td></tr>
  <tr><td style="padding:8px;background:#f8f9fa;font-weight:bold">Result</td>
      <td style="padding:8px">{output_summary}</td></tr>
</table>
<p><a href="https://crowdsourcerer.rebaselabs.online/dashboard/tasks/{task_id}"
   style="background:#6366f1;color:white;padding:10px 20px;border-radius:6px;text-decoration:none">
   View Task →</a></p>
<hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
<p style="color:#9ca3af;font-size:12px">CrowdSorcerer · <a href="https://crowdsourcerer.rebaselabs.online">crowdsourcerer.rebaselabs.online</a></p>
</body></html>
"""


def _task_failed_html(task_id: str, task_type: str, error: str) -> str:
    return f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
<h2 style="color:#ef4444">❌ Task Failed</h2>
<p>Unfortunately, your <strong>{task_type}</strong> task encountered an error.</p>
<table style="width:100%;border-collapse:collapse;margin:16px 0">
  <tr><td style="padding:8px;background:#f8f9fa;font-weight:bold;width:120px">Task ID</td>
      <td style="padding:8px">{task_id}</td></tr>
  <tr><td style="padding:8px;font-weight:bold">Error</td>
      <td style="padding:8px;color:#ef4444">{error}</td></tr>
</table>
<p>Your credits have been refunded. You can retry the task from your dashboard.</p>
<p><a href="https://crowdsourcerer.rebaselabs.online/dashboard/tasks/{task_id}"
   style="background:#6366f1;color:white;padding:10px 20px;border-radius:6px;text-decoration:none">
   View Task →</a></p>
<hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
<p style="color:#9ca3af;font-size:12px">CrowdSorcerer · <a href="https://crowdsourcerer.rebaselabs.online">crowdsourcerer.rebaselabs.online</a></p>
</body></html>
"""


def _submission_received_html(task_id: str, task_type: str, worker_name: str) -> str:
    return f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
<h2 style="color:#10b981">📬 New Submission Received</h2>
<p>A worker has submitted a response to your <strong>{task_type}</strong> task.</p>
<table style="width:100%;border-collapse:collapse;margin:16px 0">
  <tr><td style="padding:8px;background:#f8f9fa;font-weight:bold;width:120px">Task ID</td>
      <td style="padding:8px">{task_id}</td></tr>
  <tr><td style="padding:8px;font-weight:bold">Worker</td>
      <td style="padding:8px">{worker_name}</td></tr>
</table>
<p>Please review and approve or reject the submission.</p>
<p><a href="https://crowdsourcerer.rebaselabs.online/dashboard/tasks/{task_id}"
   style="background:#6366f1;color:white;padding:10px 20px;border-radius:6px;text-decoration:none">
   Review Submission →</a></p>
<hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
<p style="color:#9ca3af;font-size:12px">CrowdSorcerer · <a href="https://crowdsourcerer.rebaselabs.online">crowdsourcerer.rebaselabs.online</a></p>
</body></html>
"""


def _daily_challenge_html(challenge_title: str, task_type: str, bonus_xp: int, bonus_credits: int) -> str:
    return f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
<h2 style="color:#f59e0b">⚡ Daily Challenge Available!</h2>
<p>A new daily challenge is ready for you on CrowdSorcerer.</p>
<table style="width:100%;border-collapse:collapse;margin:16px 0">
  <tr><td style="padding:8px;background:#f8f9fa;font-weight:bold;width:120px">Challenge</td>
      <td style="padding:8px">{challenge_title}</td></tr>
  <tr><td style="padding:8px;font-weight:bold">Task Type</td>
      <td style="padding:8px">{task_type}</td></tr>
  <tr><td style="padding:8px;background:#f8f9fa;font-weight:bold">Bonus Reward</td>
      <td style="padding:8px">+{bonus_xp} XP &amp; +{bonus_credits} credits</td></tr>
</table>
<p><a href="https://crowdsourcerer.rebaselabs.online/worker/challenges"
   style="background:#f59e0b;color:white;padding:10px 20px;border-radius:6px;text-decoration:none">
   Accept Challenge →</a></p>
<hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
<p style="color:#9ca3af;font-size:12px">CrowdSorcerer · <a href="https://crowdsourcerer.rebaselabs.online">crowdsourcerer.rebaselabs.online</a></p>
</body></html>
"""


def _worker_approved_html(task_type: str, earnings: int, xp: int) -> str:
    return f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
<h2 style="color:#10b981">🎉 Submission Approved!</h2>
<p>Your submission for a <strong>{task_type}</strong> task has been approved.</p>
<table style="width:100%;border-collapse:collapse;margin:16px 0">
  <tr><td style="padding:8px;background:#f8f9fa;font-weight:bold;width:120px">Earnings</td>
      <td style="padding:8px">+{earnings} credits</td></tr>
  <tr><td style="padding:8px;font-weight:bold">XP Earned</td>
      <td style="padding:8px">+{xp} XP</td></tr>
</table>
<p><a href="https://crowdsourcerer.rebaselabs.online/worker"
   style="background:#10b981;color:white;padding:10px 20px;border-radius:6px;text-decoration:none">
   View Dashboard →</a></p>
<hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
<p style="color:#9ca3af;font-size:12px">CrowdSorcerer · <a href="https://crowdsourcerer.rebaselabs.online">crowdsourcerer.rebaselabs.online</a></p>
</body></html>
"""


# ─── Core send function ────────────────────────────────────────────────────

def _send_email_sync(to_email: str, subject: str, html_body: str) -> bool:
    """Synchronous SMTP send. Returns True on success."""
    settings = get_settings()

    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to_email

    part = email.mime.text.MIMEText(html_body, "html")
    msg.attach(part)

    try:
        if settings.smtp_use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as server:
                server.login(settings.smtp_user, settings.smtp_pass)
                server.sendmail(settings.smtp_from, to_email, msg.as_string())
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_pass)
                server.sendmail(settings.smtp_from, to_email, msg.as_string())
        return True
    except Exception as exc:
        logger.error("email.send_error", to=to_email, subject=subject, error=str(exc))
        return False


async def send_email(to_email: str, subject: str, html_body: str) -> bool:
    """Async wrapper around SMTP send. Non-blocking."""
    settings = get_settings()
    if not settings.email_enabled:
        logger.debug("email.disabled", to=to_email, subject=subject)
        return False
    if not settings.smtp_host:
        logger.warning("email.no_smtp_host", to=to_email, subject=subject)
        return False

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _send_email_sync, to_email, subject, html_body)


# ─── Typed send helpers ────────────────────────────────────────────────────

async def notify_task_completed(
    to_email: str,
    task_id: str,
    task_type: str,
    output_summary: str = "View full output in dashboard",
) -> None:
    await send_email(
        to_email=to_email,
        subject=f"✅ Your {task_type} task is done",
        html_body=_task_completed_html(task_id, task_type, output_summary),
    )


async def notify_task_failed(
    to_email: str,
    task_id: str,
    task_type: str,
    error: str = "An unexpected error occurred",
) -> None:
    await send_email(
        to_email=to_email,
        subject=f"❌ Your {task_type} task failed",
        html_body=_task_failed_html(task_id, task_type, error),
    )


async def notify_submission_received(
    to_email: str,
    task_id: str,
    task_type: str,
    worker_name: str = "a worker",
) -> None:
    await send_email(
        to_email=to_email,
        subject=f"📬 New submission on your {task_type} task",
        html_body=_submission_received_html(task_id, task_type, worker_name),
    )


async def notify_daily_challenge(
    to_email: str,
    challenge_title: str,
    task_type: str,
    bonus_xp: int,
    bonus_credits: int,
) -> None:
    await send_email(
        to_email=to_email,
        subject=f"⚡ Daily Challenge: {challenge_title}",
        html_body=_daily_challenge_html(challenge_title, task_type, bonus_xp, bonus_credits),
    )


async def notify_worker_approved(
    to_email: str,
    task_type: str,
    earnings: int,
    xp: int,
) -> None:
    await send_email(
        to_email=to_email,
        subject=f"🎉 Submission approved — you earned {earnings} credits!",
        html_body=_worker_approved_html(task_type, earnings, xp),
    )


# ─── Preference-gated send helpers ─────────────────────────────────────────
# These load the user's NotificationPreferencesDB row before sending; if the
# pref is disabled (or the row doesn't exist yet, defaults to enabled), the
# email is skipped.

async def _get_prefs(user_id: str):
    """Return the user's NotificationPreferencesDB row (or None if not set)."""
    try:
        from core.database import AsyncSessionLocal
        from models.db import NotificationPreferencesDB
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(NotificationPreferencesDB).where(
                    NotificationPreferencesDB.user_id == user_id
                )
            )
            return result.scalar_one_or_none()
    except Exception:
        return None


async def notify_task_completed_gated(
    to_email: str, user_id: str, task_id: str, task_type: str,
    output_summary: str = "View full output in dashboard",
) -> None:
    prefs = await _get_prefs(user_id)
    if prefs and not prefs.email_task_completed:
        return
    await notify_task_completed(to_email, task_id, task_type, output_summary)


async def notify_task_failed_gated(
    to_email: str, user_id: str, task_id: str, task_type: str,
    error: str = "An unexpected error occurred",
) -> None:
    prefs = await _get_prefs(user_id)
    if prefs and not prefs.email_task_failed:
        return
    await notify_task_failed(to_email, task_id, task_type, error)


async def notify_submission_received_gated(
    to_email: str, user_id: str, task_id: str, task_type: str,
    worker_name: str = "a worker",
) -> None:
    prefs = await _get_prefs(user_id)
    if prefs and not prefs.email_submission_received:
        return
    await notify_submission_received(to_email, task_id, task_type, worker_name)


async def notify_worker_approved_gated(
    to_email: str, user_id: str, task_type: str, earnings: int, xp: int,
) -> None:
    prefs = await _get_prefs(user_id)
    if prefs and not prefs.email_worker_approved:
        return
    await notify_worker_approved(to_email, task_type, earnings, xp)


async def notify_daily_challenge_gated(
    to_email: str, user_id: str, challenge_title: str, task_type: str,
    bonus_xp: int, bonus_credits: int,
) -> None:
    prefs = await _get_prefs(user_id)
    # daily_challenge emails are opt-in (default False)
    if not prefs or not prefs.email_daily_challenge:
        return
    await notify_daily_challenge(to_email, challenge_title, task_type, bonus_xp, bonus_credits)


async def notify_sla_breach_gated(
    to_email: str, user_id: str, task_id: str, task_type: str, sla_hours: float,
) -> None:
    """Send SLA breach email if user has opted in."""
    prefs = await _get_prefs(user_id)
    if prefs and not prefs.email_sla_breach:
        return
    html = f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
<h2 style="color:#ef4444">⚠️ SLA Breach Detected</h2>
<p>Your <strong>{task_type}</strong> task has exceeded its {sla_hours:.0f}-hour SLA target.</p>
<table style="width:100%;border-collapse:collapse;margin:16px 0">
  <tr><td style="padding:8px;background:#f8f9fa;font-weight:bold;width:120px">Task ID</td>
      <td style="padding:8px">{task_id}</td></tr>
  <tr><td style="padding:8px;font-weight:bold">SLA Target</td>
      <td style="padding:8px">{sla_hours:.0f} hours</td></tr>
</table>
<p>We apologize for the delay. Your task is still being processed.</p>
<p><a href="https://crowdsourcerer.rebaselabs.online/dashboard/tasks/{task_id}"
   style="background:#6366f1;color:white;padding:10px 20px;border-radius:6px;text-decoration:none">
   View Task →</a></p>
<hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
<p style="color:#9ca3af;font-size:12px">CrowdSorcerer · <a href="https://crowdsourcerer.rebaselabs.online">crowdsourcerer.rebaselabs.online</a></p>
</body></html>
"""
    await send_email(to_email, f"⚠️ SLA Breach: Your {task_type} task is overdue", html)


async def notify_payout_update_gated(
    to_email: str, user_id: str, status: str, amount_usd: float,
) -> None:
    """Send payout status update email if user has opted in."""
    prefs = await _get_prefs(user_id)
    if prefs and not prefs.email_payout_update:
        return
    emoji = {"paid": "💸", "rejected": "❌", "processing": "⏳"}.get(status, "📋")
    html = f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
<h2 style="color:#6366f1">{emoji} Payout {status.capitalize()}</h2>
<p>Your payout request of <strong>${amount_usd:.2f}</strong> has been updated.</p>
<table style="width:100%;border-collapse:collapse;margin:16px 0">
  <tr><td style="padding:8px;background:#f8f9fa;font-weight:bold;width:120px">Amount</td>
      <td style="padding:8px">${amount_usd:.2f} USD</td></tr>
  <tr><td style="padding:8px;font-weight:bold">Status</td>
      <td style="padding:8px">{status.capitalize()}</td></tr>
</table>
<p><a href="https://crowdsourcerer.rebaselabs.online/worker/earnings"
   style="background:#6366f1;color:white;padding:10px 20px;border-radius:6px;text-decoration:none">
   View Earnings →</a></p>
<hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
<p style="color:#9ca3af;font-size:12px">CrowdSorcerer · <a href="https://crowdsourcerer.rebaselabs.online">crowdsourcerer.rebaselabs.online</a></p>
</body></html>
"""
    await send_email(to_email, f"{emoji} Payout {status}: ${amount_usd:.2f}", html)
