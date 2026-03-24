"""Worker applications, team task routing.

Revision ID: 0034
Revises: 0033
Create Date: 2026-03-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── tasks: application_mode + assigned_team_id ────────────────────────────
    op.add_column("tasks", sa.Column("application_mode", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column(
        "tasks",
        sa.Column(
            "assigned_team_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("worker_teams.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_tasks_assigned_team_id", "tasks", ["assigned_team_id"])

    # ── task_applications ─────────────────────────────────────────────────────
    op.create_table(
        "task_applications",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", PGUUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("worker_id", PGUUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("proposal", sa.Text(), nullable=False),
        sa.Column("proposed_reward", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="'pending'"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "worker_id", name="uq_task_application"),
    )
    # ix_task_applications_task_id and ix_task_applications_worker_id are
    # auto-created by index=True on the columns above


def downgrade() -> None:
    op.drop_table("task_applications")
    op.drop_index("ix_tasks_assigned_team_id", table_name="tasks")
    op.drop_column("tasks", "assigned_team_id")
    op.drop_column("tasks", "application_mode")
