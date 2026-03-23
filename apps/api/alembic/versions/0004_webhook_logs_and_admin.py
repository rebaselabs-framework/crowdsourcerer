"""Add webhook_logs table and is_admin column to users.

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-23

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Add is_admin flag to users ─────────────────────────────────────
    op.add_column("users", sa.Column(
        "is_admin", sa.Boolean(), nullable=False, server_default="false"
    ))

    # ── 2. Create webhook_logs table ──────────────────────────────────────
    op.create_table(
        "webhook_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_webhook_logs_task_id", "webhook_logs", ["task_id"])
    op.create_index("ix_webhook_logs_user_id", "webhook_logs", ["user_id"])


def downgrade() -> None:
    op.drop_table("webhook_logs")
    op.drop_column("users", "is_admin")
