"""Add priority_escalated_at column to tasks.

Revision ID: 0026
Revises: 0025
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("priority_escalated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Also add "critical" to task_priority_enum (new escalation tier above "high")
    op.execute(
        "ALTER TYPE task_priority_enum ADD VALUE IF NOT EXISTS 'critical'"
    )


def downgrade() -> None:
    op.drop_column("tasks", "priority_escalated_at")
