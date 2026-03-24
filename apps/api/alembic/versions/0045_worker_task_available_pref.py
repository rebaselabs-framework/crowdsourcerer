"""add email_task_available to notification_preferences

Revision ID: 0045
Revises: 0044
Create Date: 2026-03-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_preferences",
        sa.Column("email_task_available", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("notification_preferences", "email_task_available")
