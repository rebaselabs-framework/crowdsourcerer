"""Task tags, scheduled tasks, and requester onboarding.

Revision ID: 0022
Revises: 0021
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Task tags ─────────────────────────────────────────────────────────────
    # JSON array of strings on tasks (max 20 tags, max 50 chars each)
    op.add_column(
        "tasks",
        sa.Column("tags", JSONB(), nullable=True),
    )

    # ── Scheduled tasks ───────────────────────────────────────────────────────
    # When set and in the future, task stays pending until the sweeper fires it
    op.add_column(
        "tasks",
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Index for efficient scheduled-task sweeper query
    op.create_index("ix_tasks_scheduled_at", "tasks", ["scheduled_at"])

    # ── Requester onboarding ──────────────────────────────────────────────────
    op.create_table(
        "requester_onboarding",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        # Step completion flags
        sa.Column("step_welcome", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("step_create_task", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("step_view_results", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("step_set_webhook", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("step_invite_team", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bonus_claimed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("requester_onboarding")
    op.drop_index("ix_tasks_scheduled_at", table_name="tasks")
    op.drop_column("tasks", "scheduled_at")
    op.drop_column("tasks", "tags")
