"""Add quality control fields, worker badges, and daily challenges.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-23

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Add quality control fields to tasks ────────────────────────────
    op.add_column("tasks", sa.Column(
        "is_gold_standard", sa.Boolean(), nullable=False, server_default="false"
    ))
    op.add_column("tasks", sa.Column("gold_answer", sa.JSON(), nullable=True))

    # ── 2. Create worker_badges table ─────────────────────────────────────
    op.create_table(
        "worker_badges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("badge_id", sa.String(64), nullable=False),
        sa.Column("earned_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "badge_id", name="uq_worker_badge"),
    )
    op.create_index("ix_worker_badges_user_id", "worker_badges", ["user_id"])

    # ── 3. Create daily_challenges table ──────────────────────────────────
    op.create_table(
        "daily_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("challenge_date", sa.Date(), nullable=False, unique=True),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("bonus_xp", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("bonus_credits", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("target_count", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_daily_challenges_challenge_date", "daily_challenges", ["challenge_date"])

    # ── 4. Create daily_challenge_progress table ──────────────────────────
    op.create_table(
        "daily_challenge_progress",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("challenge_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("daily_challenges.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tasks_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bonus_claimed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("bonus_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "challenge_id", name="uq_challenge_progress"),
    )
    op.create_index("ix_challenge_progress_user_id", "daily_challenge_progress", ["user_id"])
    op.create_index("ix_challenge_progress_challenge_id", "daily_challenge_progress", ["challenge_id"])

    # ── 5. Add 'approved' and 'rejected' to assignment_status_enum ────────
    # (these exist already from 0002 in the enum but may not have been added)
    # They are already there from 0002, no change needed.


def downgrade() -> None:
    op.drop_table("daily_challenge_progress")
    op.drop_table("daily_challenges")
    op.drop_table("worker_badges")
    op.drop_column("tasks", "gold_answer")
    op.drop_column("tasks", "is_gold_standard")
