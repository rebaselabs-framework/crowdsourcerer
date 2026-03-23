"""profiles_2fa_saved_searches

Add user bio/avatar, TOTP 2FA fields, and saved_searches table.

Revision ID: 0016
Revises: 0015
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── User profile fields ───────────────────────────────────────────────────
    op.add_column("users", sa.Column("bio", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(512), nullable=True))
    op.add_column("users", sa.Column("profile_public", sa.Boolean(), nullable=False,
                                     server_default="true"))

    # ── TOTP 2FA fields ───────────────────────────────────────────────────────
    op.add_column("users", sa.Column("totp_secret", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("totp_enabled", sa.Boolean(), nullable=False,
                                     server_default="false"))
    op.add_column("users", sa.Column("totp_backup_codes", sa.JSON(), nullable=True))
    # Partial JWT issued after password check but before 2FA — used in 2FA verify step
    op.add_column("users", sa.Column("totp_pending_token", sa.String(512), nullable=True))

    # ── Saved searches ────────────────────────────────────────────────────────
    op.create_table(
        "saved_searches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        # JSON blob of filter params: {task_type, status, priority, q, min_reward, ...}
        sa.Column("filters", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("alert_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("alert_frequency", sa.String(16), nullable=False,
                  server_default="'instant'"),  # instant | daily | weekly
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("match_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_saved_searches_user_id", "saved_searches", ["user_id"])
    op.create_index("ix_saved_searches_alert_enabled", "saved_searches", ["alert_enabled"])


def downgrade() -> None:
    op.drop_table("saved_searches")
    op.drop_column("users", "totp_pending_token")
    op.drop_column("users", "totp_backup_codes")
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret")
    op.drop_column("users", "profile_public")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "bio")
