"""task_comments_stripe

Add task_comments table and stripe_event_log table.

Revision ID: 0015
Revises: 0014
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Task Comments ─────────────────────────────────────────────────────────
    op.create_table(
        "task_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_internal", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["task_comments.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_task_comments_task_id", "task_comments", ["task_id"])
    op.create_index("ix_task_comments_user_id", "task_comments", ["user_id"])
    op.create_index("ix_task_comments_parent_id", "task_comments", ["parent_id"])

    # ── Stripe Event Log ──────────────────────────────────────────────────────
    op.create_table(
        "stripe_event_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stripe_event_id", sa.String(128), nullable=False, unique=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_stripe_event_log_stripe_event_id", "stripe_event_log", ["stripe_event_id"])
    op.create_index("ix_stripe_event_log_event_type", "stripe_event_log", ["event_type"])


def downgrade() -> None:
    op.drop_table("task_comments")
    op.drop_table("stripe_event_log")
