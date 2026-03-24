"""add email verification to users table

Revision ID: 0043
Revises: 0042
Create Date: 2026-03-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # email_verified — whether the address has been confirmed
    op.add_column(
        "users",
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default="false"),
    )
    # email_verification_token_hash — SHA-256 of the raw token (raw sent via email)
    op.add_column(
        "users",
        sa.Column("email_verification_token_hash", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_users_email_verification_token_hash",
        "users",
        ["email_verification_token_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_users_email_verification_token_hash", table_name="users")
    op.drop_column("users", "email_verification_token_hash")
    op.drop_column("users", "email_verified")
