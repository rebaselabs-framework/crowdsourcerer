"""Add token_version to users, CHECK constraints on credits

Revision ID: 0056
Revises: 0055
Create Date: 2026-03-31

Adds a token_version integer column to users (default 0, NOT NULL) to support
JWT token invalidation on password change.  Also adds CHECK constraints
ensuring credits cannot go negative on users and organizations.
"""
from alembic import op
import sqlalchemy as sa

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_users_credits_non_negative",
        "users",
        "credits >= 0",
    )
    op.create_check_constraint(
        "ck_organizations_credits_non_negative",
        "organizations",
        "credits >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_organizations_credits_non_negative", "organizations", type_="check")
    op.drop_constraint("ck_users_credits_non_negative", "users", type_="check")
    op.drop_column("users", "token_version")
