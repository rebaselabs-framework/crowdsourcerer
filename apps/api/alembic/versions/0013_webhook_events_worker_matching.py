"""0013 — webhook events expansion + worker matching fields

Revision ID: 0013
Revises: 0012
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Tasks: webhook_events subscription list ───────────────────────────────
    op.add_column("tasks", sa.Column("webhook_events", sa.JSON(), nullable=True))

    # ── Tasks: min_skill_level for worker matching ────────────────────────────
    # Optional: task creator can require workers to have a minimum proficiency
    # level for this task_type. NULL = no requirement.
    op.add_column("tasks", sa.Column("min_skill_level", sa.Integer(), nullable=True))

    # ── WebhookLogs: event_type ───────────────────────────────────────────────
    op.add_column("webhook_logs",
                  sa.Column("event_type", sa.String(64), nullable=True,
                            server_default="task.completed"))

    # ── Worker Skills: tag / specialisation notes ─────────────────────────────
    # match_weight: how much this skill should influence task routing (1.0 default)
    op.add_column("worker_skills",
                  sa.Column("match_weight", sa.Float(), nullable=True, server_default="1.0"))


def downgrade() -> None:
    op.drop_column("worker_skills", "match_weight")
    op.drop_column("webhook_logs", "event_type")
    op.drop_column("tasks", "min_skill_level")
    op.drop_column("tasks", "webhook_events")
