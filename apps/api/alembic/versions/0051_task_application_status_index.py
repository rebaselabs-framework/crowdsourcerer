"""Add indexes to task_applications for marketplace applied-state queries

Revision ID: 0051
Revises: 0050
Create Date: 2026-03-26

The worker marketplace endpoints run this query on every page load:

    SELECT task_id FROM task_applications
    WHERE worker_id = ?
      AND task_id IN (...)
      AND status IN ('pending', 'accepted')

Two indexes help here:

1. Composite (worker_id, status) — narrows the scan to only this worker's
   active applications before the task_id IN filter is applied.
   The existing standalone ix on worker_id alone works, but the composite
   avoids a heap fetch when status is also checked.

2. Standalone status index — also helps admin/requester queries that filter
   by status only (e.g. "all pending applications for this task").

The existing unique constraint (task_id, worker_id) already serves as a
composite index for (task_id, worker_id) lookups; we don't duplicate it.
"""
from alembic import op


revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Composite index: WHERE worker_id = ? AND status IN (...)
    # Also covers plain WHERE worker_id = ? queries via left-prefix.
    op.create_index(
        "ix_task_applications_worker_id_status",
        "task_applications",
        ["worker_id", "status"],
    )

    # Single-column status index for admin/requester queries
    op.create_index(
        "ix_task_applications_status",
        "task_applications",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_applications_status", table_name="task_applications")
    op.drop_index("ix_task_applications_worker_id_status", table_name="task_applications")
