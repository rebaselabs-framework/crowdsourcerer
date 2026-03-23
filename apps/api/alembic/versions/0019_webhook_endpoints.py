"""Add webhook_endpoints table for persistent webhook subscriptions.

Revision ID: 0019
Revises: 0018
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("events", sa.JSON(), nullable=True),
        sa.Column("secret", sa.String(128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("delivery_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_webhook_endpoints_user_id", "webhook_endpoints", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_webhook_endpoints_user_id", table_name="webhook_endpoints")
    op.drop_table("webhook_endpoints")
