"""Add requester saved templates and extend worker profile fields.

Revision ID: 0029
Revises: 0028
Create Date: 2026-03-23

Changes:
  - users: location (String 128), website_url (String 512)
  - requester_saved_templates table (personal task templates per requester)
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── users: new public profile fields ────────────────────────────────────
    op.add_column("users", sa.Column("location", sa.String(128), nullable=True))
    op.add_column("users", sa.Column("website_url", sa.String(512), nullable=True))

    # ── requester_saved_templates table ──────────────────────────────────────
    op.create_table(
        "requester_saved_templates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("task_input", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("task_config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("icon", sa.String(8), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    # ix_requester_saved_templates_user_id is auto-created by index=True on the column above
    op.create_index(
        "ix_requester_saved_templates_task_type",
        "requester_saved_templates", ["task_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_requester_saved_templates_task_type",
                  table_name="requester_saved_templates")
    op.drop_table("requester_saved_templates")
    op.drop_column("users", "website_url")
    op.drop_column("users", "location")
