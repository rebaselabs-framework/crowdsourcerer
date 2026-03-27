"""Add requester_note and reviewed_at to task_assignments

Revision ID: 0053
Revises: 0052
Create Date: 2026-03-27

Allows requesters to leave optional feedback when approving or rejecting a
worker submission.  Workers see this note in their task-detail and earnings
pages so they can understand why their submission was accepted or declined and
improve future work.

  requester_note — free-text from the requester (nullable, max ~4 KB in practice)
  reviewed_at    — UTC timestamp of when the submission was reviewed
"""
from alembic import op
import sqlalchemy as sa


revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_assignments",
        sa.Column("requester_note", sa.Text(), nullable=True),
    )
    op.add_column(
        "task_assignments",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_assignments", "reviewed_at")
    op.drop_column("task_assignments", "requester_note")
