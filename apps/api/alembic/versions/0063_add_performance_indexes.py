"""Add performance indexes for frequently queried columns.

Indexes added:
- task_assignments (worker_id, status): worker dashboard, weekly digest
- api_keys (user_id): FK index for user key listing and cascade deletes
- tasks (completed_at): admin health dashboard, analytics
- credit_transactions (user_id, created_at): weekly digest spend aggregation

Revision ID: 0063
Revises: 0062
"""
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Composite index for worker dashboard (hot path) and weekly digest worker stats
    op.create_index(
        "ix_task_assignments_worker_status",
        "task_assignments",
        ["worker_id", "status"],
    )

    # FK index for user's API keys listing and CASCADE delete performance
    op.create_index(
        "ix_api_keys_user_id",
        "api_keys",
        ["user_id"],
    )

    # Admin dashboard queries filter tasks by completion time
    op.create_index(
        "ix_tasks_completed_at",
        "tasks",
        ["completed_at"],
    )

    # Weekly digest per-user credit spend aggregation
    op.create_index(
        "ix_credit_transactions_user_created",
        "credit_transactions",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_credit_transactions_user_created", "credit_transactions")
    op.drop_index("ix_tasks_completed_at", "tasks")
    op.drop_index("ix_api_keys_user_id", "api_keys")
    op.drop_index("ix_task_assignments_worker_status", "task_assignments")
