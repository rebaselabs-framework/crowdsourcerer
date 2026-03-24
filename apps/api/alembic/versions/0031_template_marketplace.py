"""Add template marketplace fields to requester_saved_templates.

Revision ID: 0031
Revises: 0030
Create Date: 2026-03-23

Changes:
  - requester_saved_templates: is_public (bool), marketplace_title (varchar),
    marketplace_description (text), marketplace_tags (jsonb),
    import_count (int), published_at (timestamptz)
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "requester_saved_templates",
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "requester_saved_templates",
        sa.Column("marketplace_title", sa.String(255), nullable=True),
    )
    op.add_column(
        "requester_saved_templates",
        sa.Column("marketplace_description", sa.Text(), nullable=True),
    )
    op.add_column(
        "requester_saved_templates",
        sa.Column("marketplace_tags", JSONB(), nullable=True, server_default="[]"),
    )
    op.add_column(
        "requester_saved_templates",
        sa.Column("import_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "requester_saved_templates",
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # Index for marketplace browsing (only show public templates efficiently)
    op.create_index(
        "ix_templates_marketplace",
        "requester_saved_templates",
        ["is_public", "task_type", "import_count"],
    )


def downgrade() -> None:
    op.drop_index("ix_templates_marketplace", table_name="requester_saved_templates")
    op.drop_column("requester_saved_templates", "published_at")
    op.drop_column("requester_saved_templates", "import_count")
    op.drop_column("requester_saved_templates", "marketplace_tags")
    op.drop_column("requester_saved_templates", "marketplace_description")
    op.drop_column("requester_saved_templates", "marketplace_title")
    op.drop_column("requester_saved_templates", "is_public")
