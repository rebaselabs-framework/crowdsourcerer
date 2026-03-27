"""Add missing indexes on frequently filtered columns

Revision ID: 0048
Revises: 0047
Create Date: 2026-03-26

Two columns that appear in hot-path WHERE clauses had no index:

  worker_strikes.is_active     — reputation queries: WHERE is_active = true
  worker_certifications.passed — reputation/cert queries: WHERE passed = true

Note: credit_transactions.user_id already has an index from migration 0001
(ix_credit_transactions_user_id). worker_strikes.worker_id and
worker_certifications.worker_id are also already indexed.
"""
from alembic import op


revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
