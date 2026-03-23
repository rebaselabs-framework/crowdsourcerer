"""Add payload to webhook_logs for event replay; add org activity indexes.

Revision ID: 0032
Revises: 0031
Create Date: 2026-03-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── webhook_logs: store original payload for replay ──────────────────────
    op.add_column("webhook_logs", sa.Column("payload", JSON, nullable=True))
    op.add_column("webhook_logs", sa.Column("is_replay", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("webhook_logs", sa.Column("replay_of", sa.UUID(as_uuid=True), nullable=True))

    # ── tasks: store partial / streaming output for llm_generate tasks ───────
    op.add_column("tasks", sa.Column("streaming_output", sa.Text(), nullable=True))

    # ── org_activity_log: lightweight event table for org analytics ───────────
    op.create_table(
        "org_activity_log",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),  # task_created, task_completed, credit_spend
        sa.Column("task_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_org_activity_log_org_created", "org_activity_log", ["org_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_org_activity_log_org_created", "org_activity_log")
    op.drop_table("org_activity_log")
    op.drop_column("tasks", "streaming_output")
    op.drop_column("webhook_logs", "replay_of")
    op.drop_column("webhook_logs", "is_replay")
    op.drop_column("webhook_logs", "payload")
