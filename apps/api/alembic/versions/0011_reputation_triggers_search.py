"""Add worker reputation system, pipeline triggers.

Revision ID: 0011
Revises: 0010
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Worker reputation columns on users ────────────────────────────────────
    op.add_column("users", sa.Column("reputation_score", sa.Float(), nullable=False,
                                     server_default="50.0"))
    op.add_column("users", sa.Column("strike_count", sa.Integer(), nullable=False,
                                     server_default="0"))
    op.add_column("users", sa.Column("is_banned", sa.Boolean(), nullable=False,
                                     server_default="false"))
    op.add_column("users", sa.Column("ban_reason", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("ban_expires_at",
                                     sa.DateTime(timezone=True), nullable=True))

    # ── min_reputation_score on tasks ─────────────────────────────────────────
    op.add_column("tasks", sa.Column("min_reputation_score", sa.Float(), nullable=True))

    # ── Worker strikes table ──────────────────────────────────────────────────
    op.create_table(
        "worker_strikes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issued_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="minor"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_worker_strikes_worker_id", "worker_strikes", ["worker_id"])

    # ── Pipeline triggers table ───────────────────────────────────────────────
    op.create_table(
        "pipeline_triggers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("task_pipelines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger_type", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("cron_expression", sa.String(64), nullable=True),
        sa.Column("webhook_token", sa.String(64), nullable=True, unique=True),
        sa.Column("default_input", postgresql.JSONB(), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_pipeline_triggers_pipeline_id", "pipeline_triggers", ["pipeline_id"])
    op.create_index("ix_pipeline_triggers_user_id", "pipeline_triggers", ["user_id"])
    op.create_index("ix_pipeline_triggers_webhook_token", "pipeline_triggers",
                    ["webhook_token"], unique=True)
    op.create_index("ix_pipeline_triggers_next_fire_at", "pipeline_triggers", ["next_fire_at"])


def downgrade() -> None:
    op.drop_table("pipeline_triggers")
    op.drop_table("worker_strikes")
    op.drop_column("tasks", "min_reputation_score")
    op.drop_column("users", "ban_expires_at")
    op.drop_column("users", "ban_reason")
    op.drop_column("users", "is_banned")
    op.drop_column("users", "strike_count")
    op.drop_column("users", "reputation_score")
