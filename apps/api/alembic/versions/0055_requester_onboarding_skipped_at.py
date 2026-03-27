"""Add skipped_at to requester_onboarding for whole-flow skip

Revision ID: 0055
Revises: 0054
Create Date: 2026-03-27

Adds a skipped_at timestamp to requester_onboarding, mirroring the worker
onboarding's skipped_at field (on onboarding_progress).  This enables a
proper backend "skip for good" endpoint (POST /v1/requester-onboarding/skip)
that persists across sessions, instead of the current frontend-only workaround.
"""
from alembic import op
import sqlalchemy as sa

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "requester_onboarding",
        sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("requester_onboarding", "skipped_at")
