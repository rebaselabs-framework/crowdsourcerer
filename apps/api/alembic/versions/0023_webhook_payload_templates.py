"""Add webhook_payload_templates table.

Revision ID: 0023
Revises: 0022
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_payload_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "event_type", name="uq_webhook_payload_templates_user_event"),
    )
    op.create_index(
        "ix_webhook_payload_templates_user_id",
        "webhook_payload_templates",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_payload_templates_user_id",
        table_name="webhook_payload_templates",
    )
    op.drop_table("webhook_payload_templates")
