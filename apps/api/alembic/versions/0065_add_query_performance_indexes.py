"""Add targeted performance indexes for slow-profiled endpoints.

Indexes added:
- credit_transactions (user_id, type): credits balance endpoint SUM aggregation
- tasks (status, execution_mode, created_at DESC): public task feed default sort
- task_assignments (claimed_at): platform stats active-workers-30d query
- tasks (status, completed_at): platform stats combined completion counts

Identified via API profiling: these endpoints were 150-250ms and can be
brought to <50ms with proper index coverage.

Revision ID: 0065
Revises: 0064
"""
from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Credits balance endpoint: SUM(amount) WHERE user_id=? AND type='charge'
    # Existing ix_credit_transactions_user_created covers (user_id, created_at)
    # but not (user_id, type) — the credits endpoint filters by type, not date.
    op.create_index(
        "ix_credit_transactions_user_type",
        "credit_transactions",
        ["user_id", "type"],
    )

    # Public task feed: WHERE status='open' AND execution_mode='human'
    # ORDER BY created_at DESC — covers the default sort path entirely.
    op.create_index(
        "ix_tasks_status_execmode_created",
        "tasks",
        ["status", "execution_mode", "created_at"],
    )

    # Platform stats: COUNT(DISTINCT worker_id) WHERE claimed_at >= 30d ago
    op.create_index(
        "ix_task_assignments_claimed_at",
        "task_assignments",
        ["claimed_at"],
    )

    # Platform stats: combined completion counts with FILTER on completed_at
    # The existing ix_tasks_completed_at is single-column; this composite
    # allows the planner to satisfy WHERE status='completed' AND completed_at >= X
    # without a second index lookup.
    op.create_index(
        "ix_tasks_status_completed_at",
        "tasks",
        ["status", "completed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_status_completed_at", "tasks")
    op.drop_index("ix_task_assignments_claimed_at", "task_assignments")
    op.drop_index("ix_tasks_status_execmode_created", "tasks")
    op.drop_index("ix_credit_transactions_user_type", "credit_transactions")
