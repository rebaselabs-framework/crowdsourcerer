"""Add notification_digest_prefs JSON column to notification_preferences.

Revision ID: 0037
Revises: 0036
Create Date: 2026-03-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_DEFAULT = (
    '{"enabled": true, "frequency": "daily", "send_at_hour": 8, '
    '"include_task_updates": true, "include_worker_activity": true, '
    '"include_credit_changes": true}'
)


def upgrade() -> None:
    op.add_column(
        "notification_preferences",
        sa.Column(
            "notification_digest_prefs",
            JSONB,
            nullable=True,
            server_default=sa.text(f"'{_DEFAULT}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("notification_preferences", "notification_digest_prefs")
