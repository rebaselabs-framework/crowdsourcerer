"""Add missing indexes identified by performance audit.

Indexes added:
- task_applications (worker_id, status): composite for worker marketplace feed queries
- refresh_tokens (replaced_by): FK index — prevents full scan on token deletion/SET NULL
- webhook_delivery_queue (task_id): FK index — prevents full scan on task deletion
- users (active_org_id): FK index — prevents full scan on org deletion

Revision ID: 0067
Revises: 0066
"""
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Composite index for worker marketplace/feed queries:
    # WHERE worker_id = ? AND status = 'pending'
    # The existing single-column ix on worker_id helps, but adding status
    # lets PostgreSQL satisfy the filter without visiting the heap.
    op.create_index(
        "ix_task_applications_worker_status",
        "task_applications",
        ["worker_id", "status"],
    )

    # FK index on replaced_by — self-referential FK with ON DELETE SET NULL.
    # Without this, deleting a refresh token scans the entire table.
    # Partial index: only non-NULL values matter (most rows are NULL).
    op.create_index(
        "ix_refresh_tokens_replaced_by",
        "refresh_tokens",
        ["replaced_by"],
        postgresql_where="replaced_by IS NOT NULL",
    )

    # FK index on webhook_delivery_queue.task_id — ON DELETE SET NULL.
    # This table grows with every webhook failure/retry.
    op.create_index(
        "ix_webhook_delivery_queue_task_id",
        "webhook_delivery_queue",
        ["task_id"],
    )

    # FK index on users.active_org_id — ON DELETE SET NULL.
    # Without this, deleting an org scans the entire users table.
    op.create_index(
        "ix_users_active_org_id",
        "users",
        ["active_org_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_users_active_org_id", "users")
    op.drop_index("ix_webhook_delivery_queue_task_id", "webhook_delivery_queue")
    op.drop_index("ix_refresh_tokens_replaced_by", "refresh_tokens")
    op.drop_index("ix_task_applications_worker_status", "task_applications")
