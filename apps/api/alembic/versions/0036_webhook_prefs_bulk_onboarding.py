"""Webhook event preferences, task bulk ops, worker onboarding banner.

Revision ID: 0036
Revises: 0035
Create Date: 2026-03-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── webhook_event_prefs on notification_preferences ───────────────────────
    # JSON dict mapping event_type → bool (True=on, False=off).
    # Missing keys default to True (event fires). Stored per-user.
    op.add_column(
        "notification_preferences",
        sa.Column("webhook_event_prefs", JSONB, nullable=True, server_default=sa.text("'{}'")),
    )

    # ── worker_onboarding_banner_dismissed ────────────────────────────────────
    # Track if worker has dismissed the onboarding progress banner so it
    # doesn't keep reappearing after they've seen it.
    op.add_column(
        "onboarding_progress",
        sa.Column("banner_dismissed", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("notification_preferences", "webhook_event_prefs")
    op.drop_column("onboarding_progress", "banner_dismissed")
