"""Add missing indexes on frequently filtered columns

Revision ID: 0048
Revises: 0047
Create Date: 2026-03-26

Three columns that appear in hot-path WHERE clauses had no index:

  credit_transactions.user_id  — every credit history query filters by user
  worker_strikes.is_active     — reputation queries: WHERE is_active = true
  worker_certifications.passed — reputation/cert queries: WHERE passed = true

Note: worker_strikes.worker_id and worker_certifications.worker_id are
already indexed (defined with index=True in the ORM model and created at
table-creation time in an earlier migration).
"""
from alembic import op


revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_credit_transactions_user_id",
        "credit_transactions",
        ["user_id"],
    )
    op.create_index(
        "ix_worker_strikes_is_active",
        "worker_strikes",
        ["is_active"],
    )
    op.create_index(
        "ix_worker_certifications_passed",
        "worker_certifications",
        ["passed"],
    )


def downgrade() -> None:
    op.drop_index("ix_worker_certifications_passed", table_name="worker_certifications")
    op.drop_index("ix_worker_strikes_is_active", table_name="worker_strikes")
    op.drop_index("ix_credit_transactions_user_id", table_name="credit_transactions")
