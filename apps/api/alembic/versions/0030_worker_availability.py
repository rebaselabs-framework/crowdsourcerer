"""Add worker availability status.

Revision ID: 0030
Revises: 0029
Create Date: 2026-03-23

Changes:
  - users: availability_status (enum: available, busy, away, default: available)
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum type first
    op.execute("CREATE TYPE availability_status_enum AS ENUM ('available', 'busy', 'away')")
    op.add_column("users", sa.Column("availability_status",
                                     postgresql.ENUM("available", "busy", "away", name="availability_status_enum", create_type=False),
                                     server_default="available", nullable=False))


def downgrade() -> None:
    op.drop_column("users", "availability_status")
    op.execute("DROP TYPE IF EXISTS availability_status_enum")
