"""Add human task types, worker fields, and task assignments table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-23

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Add role to users ───────────────────────────────────────────────
    op.execute("CREATE TYPE IF NOT EXISTS user_role_enum AS ENUM ('requester', 'worker', 'both')")
    op.add_column("users", sa.Column(
        "role",
        sa.Enum("requester", "worker", "both", name="user_role_enum"),
        nullable=False,
        server_default="requester",
    ))

    # ── 2. Add worker gamification fields to users ─────────────────────────
    op.add_column("users", sa.Column("worker_xp", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("worker_level", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("users", sa.Column("worker_accuracy", sa.Float(), nullable=True))
    op.add_column("users", sa.Column("worker_reliability", sa.Float(), nullable=True))
    op.add_column("users", sa.Column("worker_tasks_completed", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("worker_streak_days", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("worker_last_active_date", sa.DateTime(timezone=True), nullable=True))

    # ── 3. Extend task_type_enum with human types ──────────────────────────
    # PostgreSQL ALTER TYPE ADD VALUE
    for val in [
        "label_image", "label_text", "rate_quality",
        "verify_fact", "moderate_content", "compare_rank",
        "answer_question", "transcription_review",
    ]:
        op.execute(f"ALTER TYPE task_type_enum ADD VALUE IF NOT EXISTS '{val}'")

    # ── 4. Extend task_status_enum with 'open' and 'assigned' ─────────────
    op.execute("ALTER TYPE task_status_enum ADD VALUE IF NOT EXISTS 'open'")
    op.execute("ALTER TYPE task_status_enum ADD VALUE IF NOT EXISTS 'assigned'")

    # ── 5. Add execution_mode enum and column ──────────────────────────────
    op.execute("CREATE TYPE IF NOT EXISTS execution_mode_enum AS ENUM ('ai', 'human')")
    op.add_column("tasks", sa.Column(
        "execution_mode",
        sa.Enum("ai", "human", name="execution_mode_enum"),
        nullable=False,
        server_default="ai",
    ))

    # ── 6. Add human task fields to tasks ─────────────────────────────────
    op.add_column("tasks", sa.Column("worker_reward_credits", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("assignments_required", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("tasks", sa.Column("assignments_completed", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tasks", sa.Column("claim_timeout_minutes", sa.Integer(), nullable=False, server_default="30"))
    op.add_column("tasks", sa.Column("task_instructions", sa.Text(), nullable=True))

    # ── 7. Add 'earning' to transaction_type_enum ──────────────────────────
    op.execute("ALTER TYPE transaction_type_enum ADD VALUE IF NOT EXISTS 'earning'")

    # ── 8. Create task_assignments table ──────────────────────────────────
    op.execute("""
        CREATE TYPE IF NOT EXISTS assignment_status_enum AS ENUM (
            'active', 'submitted', 'approved', 'rejected', 'released', 'timed_out'
        )
    """)
    op.create_table(
        "task_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status",
                  sa.Enum("active", "submitted", "approved", "rejected", "released", "timed_out",
                          name="assignment_status_enum"),
                  nullable=False,
                  server_default="active"),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("worker_note", sa.Text(), nullable=True),
        sa.Column("earnings_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("xp_earned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_task_assignments_task_id", "task_assignments", ["task_id"])
    op.create_index("ix_task_assignments_worker_id", "task_assignments", ["worker_id"])


def downgrade() -> None:
    op.drop_table("task_assignments")
    op.execute("DROP TYPE IF EXISTS assignment_status_enum")

    op.drop_column("tasks", "task_instructions")
    op.drop_column("tasks", "claim_timeout_minutes")
    op.drop_column("tasks", "assignments_completed")
    op.drop_column("tasks", "assignments_required")
    op.drop_column("tasks", "worker_reward_credits")
    op.drop_column("tasks", "execution_mode")
    op.execute("DROP TYPE IF EXISTS execution_mode_enum")

    op.drop_column("users", "worker_last_active_date")
    op.drop_column("users", "worker_streak_days")
    op.drop_column("users", "worker_tasks_completed")
    op.drop_column("users", "worker_reliability")
    op.drop_column("users", "worker_accuracy")
    op.drop_column("users", "worker_level")
    op.drop_column("users", "worker_xp")
    op.drop_column("users", "role")
    op.execute("DROP TYPE IF EXISTS user_role_enum")
