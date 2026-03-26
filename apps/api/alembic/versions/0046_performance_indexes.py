"""Add missing performance indexes on hot-path columns

Revision ID: 0046
Revises: 0045
Create Date: 2026-03-26

These indexes cover columns that are filtered/sorted frequently but were
missing from earlier migrations:

  task_assignments.status      — sweeper finds expired assignments every 5m
  task_assignments.timeout_at  — sweeper WHERE timeout_at <= now
  task_assignments.submitted_at — worker history, weekly digest aggregates
  tasks.execution_mode          — filtering AI vs human tasks in many endpoints
  tasks.type                    — analytics, leaderboards, marketplace filter
  credit_transactions.created_at — credit history pagination / digest aggregates

Without these, PostgreSQL performs sequential scans on growing tables,
causing degraded query performance at scale.
"""
from alembic import op


revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # task_assignments hot-path columns
    op.create_index(
        "ix_task_assignments_status",
        "task_assignments",
        ["status"],
    )
    op.create_index(
        "ix_task_assignments_timeout_at",
        "task_assignments",
        ["timeout_at"],
    )
    op.create_index(
        "ix_task_assignments_submitted_at",
        "task_assignments",
        ["submitted_at"],
    )

    # Composite index for the sweeper's primary query:
    #   WHERE status = 'active' AND timeout_at IS NOT NULL AND timeout_at <= now
    op.create_index(
        "ix_task_assignments_active_timeout",
        "task_assignments",
        ["status", "timeout_at"],
    )

    # tasks hot-path columns
    op.create_index(
        "ix_tasks_execution_mode",
        "tasks",
        ["execution_mode"],
    )
    op.create_index(
        "ix_tasks_type",
        "tasks",
        ["type"],
    )

    # Composite index for scheduled task sweeper:
    #   WHERE status = 'pending' AND scheduled_at IS NOT NULL AND scheduled_at <= now
    op.create_index(
        "ix_tasks_pending_scheduled",
        "tasks",
        ["status", "scheduled_at"],
    )

    # credit_transactions for history queries and digest aggregates
    op.create_index(
        "ix_credit_transactions_created_at",
        "credit_transactions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_credit_transactions_created_at", table_name="credit_transactions")
    op.drop_index("ix_tasks_pending_scheduled", table_name="tasks")
    op.drop_index("ix_tasks_type", table_name="tasks")
    op.drop_index("ix_tasks_execution_mode", table_name="tasks")
    op.drop_index("ix_task_assignments_active_timeout", table_name="task_assignments")
    op.drop_index("ix_task_assignments_submitted_at", table_name="task_assignments")
    op.drop_index("ix_task_assignments_timeout_at", table_name="task_assignments")
    op.drop_index("ix_task_assignments_status", table_name="task_assignments")
