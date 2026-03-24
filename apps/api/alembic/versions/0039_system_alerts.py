"""Add system_alerts table for platform health monitoring.

Revision ID: 0039
Revises: 0038
Create Date: 2026-03-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("alert_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="warning"),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_system_alerts_alert_type", "system_alerts", ["alert_type"])
    op.create_index("ix_system_alerts_severity",   "system_alerts", ["severity"])
    op.create_index("ix_system_alerts_created_at", "system_alerts", ["created_at"])
    op.create_index(
        "ix_system_alerts_unresolved",
        "system_alerts",
        ["alert_type", "resolved_at"],
    )


def downgrade() -> None:
    op.drop_table("system_alerts")
