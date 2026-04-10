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

import asyncio
import email.mime.multipart
import email.mime.text
import smtplib
import ssl
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from core.config import get_settings

logger = structlog.get_logger()
_settings = get_settings()


# ─── Shared email base template ─────────────────────────────────────────────

# Pulled from Settings so white-label / staging deployments embed the
# right links in transactional emails. Re-read once at module load —
# changing public_site_url at runtime requires a restart.
SITE_URL = _settings.public_site_url

def _cs_base(
    *,
    icon: str,
    title: str,
    accent: str = "#7c3aed",
    body_html: str,
    cta_text: str | None = None,
    cta_url: str | None = None,
    footer_note: str | None = None,
) -> str:
    """Shared branded wrapper for all CrowdSorcerer transactional emails.

    Produces a light-background email compatible with all major email clients
    while maintaining the CrowdSorcerer violet brand identity.
    """
    cta_block = ""
    if cta_text and cta_url:
        cta_block = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="margin:28px 0">'
            f'<tr><td style="border-radius:8px;background:{accent}">'
            f'<a href="{cta_url}" style="display:inline-block;padding:12px 28px;'
            f'color:#ffffff;font-weight:600;font-size:15px;text-decoration:none;'
            f'border-radius:8px;font-family:sans-serif">{cta_text}</a>'
            f'</td></tr></table>'
        )

    footer_extra = ""
    if footer_note:
        footer_extra = (
            f'<p style="color:#9ca3af;font-size:12px;margin:12px 0 0">'
            f'{footer_note}</p>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f3f4f6;padding:32px 16px">
    <tr><td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" width="580" style="max-width:580px;width:100%">

        <!-- Header -->
        <tr><td style="background:{accent};border-radius:12px 12px 0 0;padding:28px 32px;text-align:center">
          <div style="font-size:36px;line-height:1;margin-bottom:8px">{icon}</div>
          <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;line-height:1.3">{title}</h1>
        </td></tr>

        <!-- Body -->
        <tr><td style="background:#ffffff;padding:32px;border-radius:0 0 12px 12px">
          <div style="color:#374151;font-size:15px;line-height:1.6">
            {body_html}
          </div>
          {cta_block}
          <hr style="border:none;border-top:1px solid #e5e7eb;margin:28px 0"/>
          <p style="color:#9ca3af;font-size:12px;margin:0">
            CrowdSorcerer &middot;
            <a href="{SITE_URL}" style="color:#7c3aed">{SITE_URL.replace("https://","")}</a>
          </p>
          {footer_extra}
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ─── Email templates ───────────────────────────────────────────────────────

def _task_completed_html(task_id: str, task_type: str, output_summary: str) -> str:
    body = f"""
<p>Your <strong style="color:#111827">{task_type}</strong> task finished successfully.</p>
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;margin:20px 0">
  <tr>
    <td style="padding:10px 16px;border-bottom:1px solid #e5e7eb">
      <span style="color:#6b7280;font-size:13px">Task ID</span><br/>
      <span style="font-family:monospace;font-size:13px;color:#374151">{task_id}</span>
    </td>
  </tr>
  <tr>
    <td style="padding:10px 16px">
      <span style="color:#6b7280;font-size:13px">Result preview</span><br/>
      <span style="font-size:14px;color:#374151">{output_summary}</span>
    </td>
  </tr>
</table>
"""
    return _cs_base(
        icon="✅",
        title="Task Completed",
        accent="#059669",
        body_html=body,
        cta_text="View Full Result →",
        cta_url=f"{SITE_URL}/dashboard/tasks/{task_id}",
    )


def _task_failed_html(task_id: str, task_type: str, error: str) -> str:
    body = f"""
<p>Unfortunately, your <strong style="color:#111827">{task_type}</strong> task encountered an error.
Your credits have been <strong>automatically refunded</strong>.</p>
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;margin:20px 0">
  <tr>
    <td style="padding:10px 16px;border-bottom:1px solid #fecaca">
      <span style="color:#6b7280;font-size:13px">Task ID</span><br/>
      <span style="font-family:monospace;font-size:13px;color:#374151">{task_id}</span>
    </td>
  </tr>
  <tr>
    <td style="padding:10px 16px">
      <span style="color:#6b7280;font-size:13px">Error</span><br/>
      <span style="font-size:14px;color:#dc2626">{error}</span>
    </td>
  </tr>
</table>
<p style="font-size:14px;color:#6b7280">You can retry the task directly from your dashboard.</p>
"""
    return _cs_base(
        icon="❌",
        title="Task Failed",
        accent="#dc2626",
        body_html=body,
        cta_text="View Task & Retry →",
        cta_url=f"{SITE_URL}/dashboard/tasks/{task_id}",
    )


def _submission_received_html(task_id: str, task_type: str, worker_name: str) -> str:
    body = f"""
<p>A worker has submitted a response to your <strong style="color:#111827">{task_type}</strong> task.</p>
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;margin:20px 0">
  <tr>
    <td style="padding:10px 16px;border-bottom:1px solid #e5e7eb">
      <span style="color:#6b7280;font-size:13px">Worker</span><br/>
      <span style="font-size:14px;color:#374151;font-weight:600">{worker_name}</span>
    </td>
  </tr>
  <tr>
    <td style="padding:10px 16px">
      <span style="color:#6b7280;font-size:13px">Task ID</span><br/>
      <span style="font-family:monospace;font-size:13px;color:#374151">{task_id}</span>
    </td>
  </tr>
</table>
<p style="font-size:14px;color:#6b7280">Please review and approve or reject the submission.</p>
"""
    return _cs_base(
        icon="📬",
        title="New Submission Received",
        accent="#0891b2",
        body_html=body,
        cta_text="Review Submission →",
        cta_url=f"{SITE_URL}/dashboard/tasks/{task_id}",
    )


def _daily_challenge_html(challenge_title: str, task_type: str, bonus_xp: int, bonus_credits: int) -> str:
    body = f"""
<p>A new daily challenge is ready — complete it to earn bonus rewards!</p>
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;margin:20px 0">
  <tr>
    <td style="padding:10px 16px;border-bottom:1px solid #fde68a">
      <span style="color:#6b7280;font-size:13px">Challenge</span><br/>
      <span style="font-size:15px;font-weight:600;color:#111827">{challenge_title}</span>
    </td>
  </tr>
  <tr>
    <td style="padding:10px 16px;border-bottom:1px solid #fde68a">
      <span style="color:#6b7280;font-size:13px">Task type</span><br/>
      <span style="font-size:14px;color:#374151">{task_type}</span>
    </td>
  </tr>
  <tr>
    <td style="padding:10px 16px">
      <span style="color:#6b7280;font-size:13px">Bonus reward</span><br/>
      <span style="font-size:15px;font-weight:700;color:#d97706">+{bonus_xp} XP &amp; +{bonus_credits} credits</span>
    </td>
  </tr>
</table>
"""
    return _cs_base(
        icon="⚡",
        title="Daily Challenge Available!",
        accent="#d97706",
        body_html=body,
        cta_text="Accept Challenge →",
        cta_url=f"{SITE_URL}/worker/challenges",
    )


def _worker_approved_html(task_type: str, earnings: int, xp: int) -> str:
    usd = earnings / 100
    body = f"""
<p>Great work! Your submission for a <strong style="color:#111827">{task_type}</strong> task
has been approved. Here's what you earned:</p>
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="margin:20px 0">
  <tr>
    <td width="48%" style="background:#f0fdf4;border:1px solid #86efac;border-radius:8px;
                            padding:16px;text-align:center">
      <div style="font-size:28px;font-weight:700;color:#16a34a">{earnings}</div>
      <div style="font-size:12px;color:#6b7280;margin-top:4px">credits earned (${usd:.2f})</div>
    </td>
    <td width="4%"></td>
    <td width="48%" style="background:#faf5ff;border:1px solid #d8b4fe;border-radius:8px;
                            padding:16px;text-align:center">
      <div style="font-size:28px;font-weight:700;color:#7c3aed">+{xp} XP</div>
      <div style="font-size:12px;color:#6b7280;margin-top:4px">experience points</div>
    </td>
  </tr>
</table>
"""
    return _cs_base(
        icon="🎉",
        title="Submission Approved!",
        accent="#16a34a",
        body_html=body,
        cta_text="View My Earnings →",
        cta_url=f"{SITE_URL}/worker/earnings",
    )


def _task_timeout_html(task_id: str, task_type: str, worker_name: str) -> str:
    body = f"""
<p>A worker's assignment on your <strong style="color:#111827">{task_type}</strong> task
timed out before they could submit.</p>
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;margin:20px 0">
  <tr>
    <td style="padding:10px 16px;border-bottom:1px solid #fde68a">
      <span style="color:#6b7280;font-size:13px">Worker</span><br/>
      <span style="font-size:14px;color:#374151">{worker_name}</span>
    </td>
  </tr>
  <tr>
    <td style="padding:10px 16px">
      <span style="color:#6b7280;font-size:13px">Task ID</span><br/>
      <span style="font-family:monospace;font-size:13px;color:#374151">{task_id}</span>
    </td>
  </tr>
</table>
<p style="font-size:14px;color:#6b7280">
  Good news: your task has been <strong>automatically reopened</strong> and is back in
  the marketplace for another worker to claim.
</p>
"""
    return _cs_base(
        icon="⏱️",
        title="Worker Assignment Timed Out",
        accent="#d97706",
        body_html=body,
        cta_text="View Task →",
        cta_url=f"{SITE_URL}/dashboard/tasks/{task_id}",
    )


def _low_credits_html(balance: int, threshold: int, name: str | None = None) -> str:
    greeting = f"Hi {name}," if name else "Hi,"
    usd = balance / 100
    body = f"""
<p>{greeting}</p>
<p>Your CrowdSorcerer credit balance has dropped below your alert threshold.</p>
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;margin:20px 0">
  <tr>
    <td style="padding:12px 16px;border-bottom:1px solid #fecaca">
      <span style="color:#6b7280;font-size:13px">Current balance</span><br/>
      <span style="font-size:18px;font-weight:700;color:#dc2626">{balance} credits (${usd:.2f})</span>
    </td>
  </tr>
  <tr>
    <td style="padding:12px 16px">
      <span style="color:#6b7280;font-size:13px">Your alert threshold</span><br/>
      <span style="font-size:15px;color:#374151">{threshold} credits</span>
    </td>
  </tr>
</table>
<p style="font-size:14px;color:#6b7280">Top up now to keep your tasks running without interruption.</p>
"""
    return _cs_base(
        icon="⚠️",
        title="Low Credit Balance",
        accent="#d97706",
        body_html=body,
        cta_text="Buy Credits →",
        cta_url=f"{SITE_URL}/dashboard/billing",
        footer_note=(
            'You\'re receiving this because you set up a low-balance alert. '
            f'<a href="{SITE_URL}/dashboard/notification-preferences" style="color:#7c3aed">Manage alerts</a>'
        ),
    )


def _password_reset_html(reset_url: str, name: str | None = None) -> str:
    greeting = f"Hi {name}," if name else "Hi there,"
    body = f"""
<p>{greeting}</p>
<p>We received a request to reset your CrowdSorcerer password.
Click the button below to set a new one.</p>
<p style="font-size:14px;color:#6b7280">
  This link expires in <strong style="color:#374151">30 minutes</strong>.
</p>
"""
    return _cs_base(
        icon="🔐",
        title="Reset Your Password",
        accent="#7c3aed",
        body_html=body,
        cta_text="Reset Password →",
        cta_url=reset_url,
        footer_note="If you didn't request a password reset, you can safely ignore this email. Your password will remain unchanged.",
    )


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
        # Log at INFO so operators see it — email silently disabled is easy to miss
        logger.info("email.disabled_skipped", to=to_email, subject=subject)
        return False
    if not settings.smtp_host:
        logger.warning("email.no_smtp_host", to=to_email, subject=subject)
        return False

    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, _send_email_sync, to_email, subject, html_body)
    if ok:
        logger.info("email.sent", to=to_email, subject=subject)
    return ok


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


async def notify_low_credits(to_email: str, balance: int, threshold: int, name: str | None = None) -> bool:
    """Send a low-credit alert email. Only fires once per threshold crossing (gate in credit_alerts.py)."""
    return await send_email(
        to_email=to_email,
        subject=f"⚠️ Low credits: {balance} remaining — CrowdSorcerer",
        html_body=_low_credits_html(balance, threshold, name),
    )


async def send_password_reset(to_email: str, reset_url: str, name: str | None = None) -> bool:
    """Send a password reset email. Always sends regardless of notification prefs — security emails bypass opt-outs."""
    return await send_email(
        to_email=to_email,
        subject="Reset your CrowdSorcerer password",
        html_body=_password_reset_html(reset_url, name),
    )


def _email_verification_html(verify_url: str, name: str | None) -> str:
    greeting = f"Hi {name}," if name else "Hi there,"
    body = f"""
<p>{greeting}</p>
<p>Thanks for signing up for CrowdSorcerer! Please verify your email address to unlock all platform features.</p>
<p style="font-size:14px;color:#6b7280">
  This link expires in <strong style="color:#374151">24 hours</strong>.
</p>
"""
    return _cs_base(
        icon="✉️",
        title="Verify Your Email Address",
        accent="#7c3aed",
        body_html=body,
        cta_text="Verify My Email →",
        cta_url=verify_url,
        footer_note="If you didn't create an account, you can safely ignore this email.",
    )


async def send_email_verification(to_email: str, verify_url: str, name: str | None = None) -> bool:
    """Send an email address verification link. Security email — always sent, bypasses notification prefs."""
    return await send_email(
        to_email=to_email,
        subject="Verify your CrowdSorcerer email address",
        html_body=_email_verification_html(verify_url, name),
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
    except SQLAlchemyError:
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
    body = f"""
<p>Your <strong style="color:#111827">{task_type}</strong> task has exceeded its
{sla_hours:.0f}-hour SLA target. We apologize for the delay — your task is still being processed.</p>
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;margin:20px 0">
  <tr>
    <td style="padding:10px 16px;border-bottom:1px solid #fecaca">
      <span style="color:#6b7280;font-size:13px">Task ID</span><br/>
      <span style="font-family:monospace;font-size:13px;color:#374151">{task_id}</span>
    </td>
  </tr>
  <tr>
    <td style="padding:10px 16px">
      <span style="color:#6b7280;font-size:13px">SLA target</span><br/>
      <span style="font-size:14px;color:#dc2626;font-weight:600">{sla_hours:.0f} hours (exceeded)</span>
    </td>
  </tr>
</table>
"""
    html = _cs_base(
        icon="⚠️",
        title="SLA Breach Detected",
        accent="#dc2626",
        body_html=body,
        cta_text="View Task →",
        cta_url=f"{SITE_URL}/dashboard/tasks/{task_id}",
    )
    await send_email(to_email, f"⚠️ SLA Breach: Your {task_type} task is overdue", html)


async def notify_payout_update_gated(
    to_email: str, user_id: str, status: str, amount_usd: float,
) -> None:
    """Send payout status update email if user has opted in."""
    prefs = await _get_prefs(user_id)
    if prefs and not prefs.email_payout_update:
        return
    emoji = {"paid": "💸", "rejected": "❌", "processing": "⏳"}.get(status, "📋")
    status_color = {"paid": "#16a34a", "rejected": "#dc2626", "processing": "#d97706"}.get(status, "#374151")
    body = f"""
<p>Your payout request status has been updated.</p>
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;margin:20px 0">
  <tr>
    <td style="padding:12px 16px;border-bottom:1px solid #e5e7eb">
      <span style="color:#6b7280;font-size:13px">Amount</span><br/>
      <span style="font-size:18px;font-weight:700;color:#111827">${amount_usd:.2f} USD</span>
    </td>
  </tr>
  <tr>
    <td style="padding:12px 16px">
      <span style="color:#6b7280;font-size:13px">Status</span><br/>
      <span style="font-size:15px;font-weight:700;color:{status_color}">{status.capitalize()}</span>
    </td>
  </tr>
</table>
"""
    html = _cs_base(
        icon=emoji,
        title=f"Payout {status.capitalize()}",
        accent=status_color,
        body_html=body,
        cta_text="View Earnings →",
        cta_url=f"{SITE_URL}/worker/earnings",
    )
    await send_email(to_email, f"{emoji} Payout {status}: ${amount_usd:.2f}", html)


async def notify_task_timeout_gated(
    to_email: str, user_id: str, task_id: str, task_type: str,
    worker_name: str = "a worker",
) -> None:
    """Send task-timeout email to requester if they have task_failed emails enabled."""
    prefs = await _get_prefs(user_id)
    # Gate on email_task_failed preference (timeout = task interrupted, semantically similar)
    if prefs and not prefs.email_task_failed:
        return
    await send_email(
        to_email=to_email,
        subject=f"⏱️ Worker timed out on your {task_type} task — reopened",
        html_body=_task_timeout_html(task_id, task_type, worker_name),
    )


def _task_available_html(
    task_id: str,
    task_type: str,
    reward_credits: int,
    task_title: str | None,
) -> str:
    display_type = task_type.replace("_", " ").title()
    title_row = ""
    if task_title:
        title_row = f"""
  <tr>
    <td style="padding:10px 16px;border-bottom:1px solid #e5e7eb">
      <span style="color:#6b7280;font-size:13px">Task title</span><br/>
      <span style="font-size:14px;font-weight:600;color:#111827">{task_title}</span>
    </td>
  </tr>"""
    usd = reward_credits / 100
    body = f"""
<p>A new task matching your skills has just been posted. Claim it before someone else does!</p>
<table role="presentation" cellpadding="0" cellspacing="0" width="100%"
       style="background:#f5f3ff;border:1px solid #ddd6fe;border-radius:8px;margin:20px 0">
  <tr>
    <td style="padding:10px 16px;border-bottom:1px solid #ddd6fe">
      <span style="color:#6b7280;font-size:13px">Task type</span><br/>
      <span style="font-size:14px;font-weight:600;color:#374151">{display_type}</span>
    </td>
  </tr>
  {title_row}
  <tr>
    <td style="padding:12px 16px">
      <span style="color:#6b7280;font-size:13px">Reward</span><br/>
      <span style="font-size:20px;font-weight:700;color:#7c3aed">{reward_credits} credits</span>
      <span style="color:#9ca3af;font-size:13px;margin-left:6px">(${usd:.2f})</span>
    </td>
  </tr>
</table>
"""
    return _cs_base(
        icon="🔔",
        title=f"New {display_type} Task Available",
        accent="#7c3aed",
        body_html=body,
        cta_text="View Task in Marketplace →",
        cta_url=f"{SITE_URL}/worker/marketplace",
        footer_note=(
            'You received this because you enabled task availability alerts. '
            f'<a href="{SITE_URL}/dashboard/notification-preferences" style="color:#7c3aed">Manage preferences</a>'
        ),
    )


async def notify_task_available_gated(
    to_email: str,
    user_id: str,
    task_id: str,
    task_type: str,
    reward_credits: int,
    task_title: str | None = None,
) -> bool:
    """Send new-task-available email if worker has opted in (email_task_available)."""
    prefs = await _get_prefs(user_id)
    # task_available is opt-in (default False) — only send if explicitly enabled
    if not prefs or not prefs.email_task_available:
        return False
    return await send_email(
        to_email=to_email,
        subject=f"🔔 New {task_type.replace('_', ' ').title()} task available — {reward_credits} credits",
        html_body=_task_available_html(task_id, task_type, reward_credits, task_title),
    )


async def notify_matched_workers_of_task(
    task_id: str,
    task_type: str,
    reward_credits: int,
    task_title: str | None,
    db: "AsyncSession",
    limit: int = 50,
) -> int:
    """
    Find workers with skills matching `task_type` who have opted in to task-available
    emails and send each of them a notification. Returns the count of emails sent.

    Limited to `limit` workers (default 50) to avoid spam on large worker pools.
    Workers are ranked by proficiency_level DESC so the most skilled get notified first.
    """
    from sqlalchemy import select
    from models.db import WorkerSkillDB, UserDB, NotificationPreferencesDB

    # Workers with a skill row for this task type AND opted-in email pref
    result = await db.execute(
        select(UserDB, NotificationPreferencesDB)
        .join(WorkerSkillDB, WorkerSkillDB.worker_id == UserDB.id)
        .join(
            NotificationPreferencesDB,
            NotificationPreferencesDB.user_id == UserDB.id,
        )
        .where(
            WorkerSkillDB.task_type == task_type,
            NotificationPreferencesDB.email_task_available == True,  # noqa: E712
            UserDB.email.isnot(None),
        )
        .order_by(WorkerSkillDB.proficiency_level.desc())
        .limit(limit)
    )
    rows = result.all()

    sent = 0
    for user, _prefs in rows:
        try:
            ok = await send_email(
                to_email=user.email,
                subject=f"🔔 New {task_type.replace('_', ' ').title()} task available — {reward_credits} credits",
                html_body=_task_available_html(task_id, task_type, reward_credits, task_title),
            )
            if ok:
                sent += 1
        except Exception:
            logger.exception("task_available_email_failed", user_id=str(user.id))

    if sent:
        logger.info("task_available_emails_sent", task_id=task_id, task_type=task_type, count=sent)
    return sent


# ─── Weekly Analytics Digest ─────────────────────────────────────────────────

def _weekly_digest_html(
    user_name: str,
    week_label: str,
    tasks_created: int,
    tasks_completed: int,
    credits_spent: int,
    credits_balance: int,
    top_workers: list[dict],  # [{"name": str, "tasks": int, "earnings": int}]
    # Worker-specific fields (only shown if user is a worker)
    worker_tasks_done: int = 0,
    worker_earnings: int = 0,
    worker_xp: int = 0,
    is_worker: bool = False,
) -> str:
    top_workers_rows = "".join(
        f'<tr><td style="padding:6px 8px">{i+1}. {w["name"]}</td>'
        f'<td style="padding:6px 8px;text-align:right">{w["tasks"]} tasks</td>'
        f'<td style="padding:6px 8px;text-align:right">{w["earnings"]} cr</td></tr>'
        for i, w in enumerate(top_workers[:5])
    )
    worker_section = ""
    if is_worker:
        worker_section = f"""
<h3 style="color:#10b981;margin:24px 0 8px">Your Worker Stats</h3>
<table style="width:100%;border-collapse:collapse;margin:8px 0">
  <tr>
    <td style="padding:12px;background:#ecfdf5;border-radius:8px;text-align:center;width:33%">
      <div style="font-size:24px;font-weight:bold;color:#10b981">{worker_tasks_done}</div>
      <div style="font-size:12px;color:#6b7280">Tasks Completed</div>
    </td>
    <td style="width:12px"></td>
    <td style="padding:12px;background:#f0fdf4;border-radius:8px;text-align:center;width:33%">
      <div style="font-size:24px;font-weight:bold;color:#10b981">{worker_earnings}</div>
      <div style="font-size:12px;color:#6b7280">Credits Earned</div>
    </td>
    <td style="width:12px"></td>
    <td style="padding:12px;background:#f7fee7;border-radius:8px;text-align:center;width:33%">
      <div style="font-size:24px;font-weight:bold;color:#84cc16">+{worker_xp} XP</div>
      <div style="font-size:12px;color:#6b7280">XP This Week</div>
    </td>
  </tr>
</table>"""

    top_table = f"""
<h3 style="color:#6366f1;margin:24px 0 8px">🏆 Top Workers This Week</h3>
<table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px">
  <thead><tr style="background:#f8f9fa">
    <th style="padding:8px;text-align:left">Worker</th>
    <th style="padding:8px;text-align:right">Tasks</th>
    <th style="padding:8px;text-align:right">Earned</th>
  </tr></thead>
  <tbody>{top_workers_rows if top_workers_rows else '<tr><td colspan="3" style="padding:12px;text-align:center;color:#9ca3af">No completed tasks this week</td></tr>'}</tbody>
</table>""" if top_workers else ""

    return f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#111827">
<div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:24px;border-radius:12px;margin-bottom:24px;text-align:center">
  <h1 style="color:white;margin:0;font-size:24px">📊 Weekly Digest</h1>
  <p style="color:#c7d2fe;margin:8px 0 0">{week_label}</p>
</div>
<p>Hi {user_name}, here's your CrowdSorcerer summary for the week.</p>

<h3 style="color:#6366f1;margin:24px 0 8px">Platform Activity</h3>
<table style="width:100%;border-collapse:collapse;margin:8px 0">
  <tr>
    <td style="padding:12px;background:#f0f4ff;border-radius:8px;text-align:center;width:33%">
      <div style="font-size:28px;font-weight:bold;color:#6366f1">{tasks_created}</div>
      <div style="font-size:12px;color:#6b7280">Tasks Created</div>
    </td>
    <td style="width:12px"></td>
    <td style="padding:12px;background:#f0fdf4;border-radius:8px;text-align:center;width:33%">
      <div style="font-size:28px;font-weight:bold;color:#10b981">{tasks_completed}</div>
      <div style="font-size:12px;color:#6b7280">Completed</div>
    </td>
    <td style="width:12px"></td>
    <td style="padding:12px;background:#fff7ed;border-radius:8px;text-align:center;width:33%">
      <div style="font-size:28px;font-weight:bold;color:#f59e0b">{credits_spent}</div>
      <div style="font-size:12px;color:#6b7280">Credits Spent</div>
    </td>
  </tr>
</table>

<p style="color:#6b7280;font-size:13px">Your current balance: <strong style="color:#111">{credits_balance} credits</strong></p>
{worker_section}
{top_table}

<div style="margin-top:24px;display:flex;gap:12px">
  <a href="{SITE_URL}/dashboard"
     style="background:#6366f1;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block">
     Go to Dashboard →</a>
  <a href="{SITE_URL}/dashboard/notification-preferences"
     style="background:#f3f4f6;color:#374151;padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block;margin-left:8px">
     Manage preferences</a>
</div>
<hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
<p style="color:#9ca3af;font-size:12px">
  CrowdSorcerer Weekly Digest ·
  <a href="{SITE_URL}/dashboard/notification-preferences">Unsubscribe</a>
</p>
</body></html>
"""


async def send_weekly_digest(
    to_email: str,
    user_name: str,
    week_label: str,
    tasks_created: int,
    tasks_completed: int,
    credits_spent: int,
    credits_balance: int,
    top_workers: list,
    worker_tasks_done: int = 0,
    worker_earnings: int = 0,
    worker_xp: int = 0,
    is_worker: bool = False,
) -> None:
    await send_email(
        to_email=to_email,
        subject=f"📊 Your CrowdSorcerer Weekly Digest — {week_label}",
        html_body=_weekly_digest_html(
            user_name=user_name,
            week_label=week_label,
            tasks_created=tasks_created,
            tasks_completed=tasks_completed,
            credits_spent=credits_spent,
            credits_balance=credits_balance,
            top_workers=top_workers,
            worker_tasks_done=worker_tasks_done,
            worker_earnings=worker_earnings,
            worker_xp=worker_xp,
            is_worker=is_worker,
        ),
    )


# ─── Daily Digest ─────────────────────────────────────────────────────────────

def _daily_digest_html(
    user_name: str,
    date_label: str,
    unread_count: int,
    highlights: list[dict],  # [{"title": str, "body": str, "link": str}]
    credits_balance: int,
) -> str:
    """Compact daily digest: show today's unread notifications."""
    rows = ""
    for h in highlights[:8]:
        rows += (
            f'<tr><td style="padding:10px;border-bottom:1px solid #f3f4f6">'
            f'<a href="{SITE_URL}{h.get("link","")}" '
            f'style="color:#6366f1;text-decoration:none;font-weight:500">{h["title"]}</a>'
            f'<div style="font-size:12px;color:#6b7280;margin-top:2px">{h["body"][:120]}</div>'
            f'</td></tr>'
        )

    return f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#111827">
<div style="background:linear-gradient(135deg,#0ea5e9,#6366f1);padding:20px;border-radius:12px;margin-bottom:24px">
  <h1 style="color:white;margin:0;font-size:20px">☀️ Daily Digest</h1>
  <p style="color:#bae6fd;margin:6px 0 0;font-size:14px">{date_label}</p>
</div>
<p>Hi {user_name}, you have <strong>{unread_count} unread notification{"s" if unread_count != 1 else ""}</strong> today.</p>

<h3 style="color:#374151;font-size:15px;margin:20px 0 8px">Recent Activity</h3>
<table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
  {rows if rows else '<tr><td style="padding:16px;text-align:center;color:#9ca3af">No new notifications</td></tr>'}
</table>

<p style="color:#6b7280;font-size:13px;margin-top:16px">
  Current balance: <strong style="color:#111">{credits_balance} credits</strong>
</p>

<div style="margin-top:20px">
  <a href="{SITE_URL}/dashboard/notifications"
     style="background:#6366f1;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block">
     View All Notifications →</a>
  <a href="{SITE_URL}/dashboard/notification-preferences"
     style="color:#6b7280;font-size:13px;text-decoration:none;margin-left:16px">
     Manage digest</a>
</div>
<hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
<p style="color:#9ca3af;font-size:12px">
  CrowdSorcerer Daily Digest ·
  <a href="{SITE_URL}/dashboard/notification-preferences">Unsubscribe</a>
</p>
</body></html>
"""


async def send_daily_digest(
    to_email: str,
    user_name: str,
    date_label: str,
    unread_count: int,
    highlights: list,
    credits_balance: int,
) -> None:
    await send_email(
        to_email=to_email,
        subject=f"☀️ CrowdSorcerer Daily Digest — {date_label}",
        html_body=_daily_digest_html(
            user_name=user_name,
            date_label=date_label,
            unread_count=unread_count,
            highlights=highlights,
            credits_balance=credits_balance,
        ),
    )
