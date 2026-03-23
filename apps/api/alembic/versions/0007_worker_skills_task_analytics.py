"""Add worker_skills table and task analytics support.

Revision ID: 0007
Revises: 0006
Create Date: 2026-03-23

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── worker_skills ──────────────────────────────────────────────────────
    # One row per (worker, task_type) pair — updated as tasks are completed.
    op.create_table(
        "worker_skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "worker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("tasks_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tasks_approved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tasks_rejected", sa.Integer(), nullable=False, server_default="0"),
        # Accuracy = approved / (approved + rejected), NULL until ≥1 graded
        sa.Column("accuracy", sa.Float(), nullable=True),
        # Average submission time in minutes (claimed_at → submitted_at)
        sa.Column("avg_response_minutes", sa.Float(), nullable=True),
        # Total credits earned from this skill
        sa.Column("credits_earned", sa.Integer(), nullable=False, server_default="0"),
        # Proficiency level: 1=novice, 2=learner, 3=competent, 4=proficient, 5=expert
        sa.Column("proficiency_level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_task_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("worker_id", "task_type", name="uq_worker_skill"),
    )
    op.create_index("ix_worker_skills_worker_id", "worker_skills", ["worker_id"])
    op.create_index("ix_worker_skills_task_type", "worker_skills", ["task_type"])


def downgrade() -> None:
    op.drop_index("ix_worker_skills_task_type", "worker_skills")
    op.drop_index("ix_worker_skills_worker_id", "worker_skills")
    op.drop_table("worker_skills")
