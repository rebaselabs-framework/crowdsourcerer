"""Add platform_announcements table for admin broadcast banners

Revision ID: 0054
Revises: 0053
Create Date: 2026-03-27

Allows admins to post announcements (maintenance windows, new features, beta
notices) that appear as dismissible banners on all platform pages.

Fields:
  title         — short headline (≤200 chars)
  message       — body text (unlimited)
  type          — info | warning | maintenance | feature (controls banner colour)
  target_role   — all | requester | worker (audience filter)
  is_active     — soft-delete / instant hide
  starts_at     — earliest display time (default = now)
  expires_at    — NULL = show indefinitely
  created_by_id — FK to users.id (nullable so old admin accounts can be deleted)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_announcements",
        sa.Column("id",            UUID(as_uuid=True), primary_key=True),
        sa.Column("title",         sa.String(200),     nullable=False),
        sa.Column("message",       sa.Text(),          nullable=False),
        sa.Column("type",          sa.String(20),      nullable=False, server_default="info"),
        sa.Column("target_role",   sa.String(20),      nullable=False, server_default="all"),
        sa.Column("is_active",     sa.Boolean(),       nullable=False, server_default="true"),
        sa.Column("starts_at",     sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at",    sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",    sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_by_id", UUID(as_uuid=True), sa.ForeignKey(
            "users.id", ondelete="SET NULL"), nullable=True),
    )
    # Index for the hot-path public fetch: active + not expired + started
    op.create_index(
        "ix_platform_announcements_active_expires",
        "platform_announcements",
        ["is_active", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_platform_announcements_active_expires",
                  table_name="platform_announcements")
    op.drop_table("platform_announcements")
