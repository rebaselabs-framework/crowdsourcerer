"""Add streak freeze columns to users table.

Revision ID: 0060
Revises: 0059
"""
from alembic import op
import sqlalchemy as sa

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("streak_freezes", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("streak_freeze_last_earned", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("streak_freezes_used", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("users", "streak_freezes_used")
    op.drop_column("users", "streak_freeze_last_earned")
    op.drop_column("users", "streak_freezes")
