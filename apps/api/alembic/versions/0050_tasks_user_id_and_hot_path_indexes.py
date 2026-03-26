"""Add missing hot-path indexes: tasks.user_id, credit_transactions.task_id

Revision ID: 0050
Revises: 0049
Create Date: 2026-03-26

Performance analysis found that two high-traffic columns were missing indexes:

  tasks.user_id  — every dashboard task list query (GET /v1/tasks) filters by
                   user_id.  Without an index this is a full sequential scan on
                   the tasks table for every authenticated page load.
                   A composite (user_id, created_at DESC) index also covers the
                   common ORDER BY created_at DESC pagination pattern.

  credit_transactions.task_id  — task detail pages and credit history views join
                                  credit_transactions on task_id to show per-task
                                  cost breakdowns.  The column is a nullable FK
                                  (SET NULL on task delete) but queries filter on
                                  it directly (WHERE task_id = ?).

Previously added (0046-0048):
  - tasks.type, tasks.execution_mode, (status, scheduled_at) composite
  - credit_transactions.user_id, credit_transactions.created_at
  - worker_strikes.is_active, worker_certifications.passed
"""
from alembic import op


revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Composite index: covers WHERE user_id=? ORDER BY created_at DESC
    # Also serves plain WHERE user_id=? queries via left-prefix usage.
    op.create_index(
        "ix_tasks_user_id_created_at",
        "tasks",
        ["user_id", "created_at"],
        postgresql_ops={"created_at": "DESC"},
    )

    # Nullable FK — WHERE task_id = ? on credit history / task detail pages
    op.create_index(
        "ix_credit_transactions_task_id",
        "credit_transactions",
        ["task_id"],
        postgresql_where="task_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index("ix_credit_transactions_task_id", table_name="credit_transactions")
    op.drop_index("ix_tasks_user_id_created_at", table_name="tasks")
