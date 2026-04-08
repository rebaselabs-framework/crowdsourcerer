"""Add composite index on tasks(user_id, created_at).

Covers the weekly digest task-stats aggregation query which filters by
user_id IN (...) AND created_at >= week_start. Without this index,
PostgreSQL does a sequential scan on the tasks table.

Revision ID: 0064
Revises: 0063
"""
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_tasks_user_created",
        "tasks",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_user_created", "tasks")
