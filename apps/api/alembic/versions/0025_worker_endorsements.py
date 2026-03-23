"""Add worker_endorsements table.

Revision ID: 0025
Revises: 0024
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_endorsements",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "worker_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "requester_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("tasks.id"),
            nullable=False,
        ),
        sa.Column("skill_tag", sa.String(100), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "worker_id", "requester_id", "task_id",
            name="uq_worker_endorsement",
        ),
    )
    op.create_index(
        "ix_worker_endorsements_worker_id",
        "worker_endorsements",
        ["worker_id"],
    )

    # Also add "archived" to the task_status_enum
    op.execute(
        "ALTER TYPE task_status_enum ADD VALUE IF NOT EXISTS 'archived'"
    )


def downgrade() -> None:
    op.drop_index("ix_worker_endorsements_worker_id", table_name="worker_endorsements")
    op.drop_table("worker_endorsements")
    # Note: PostgreSQL does not support removing enum values without recreation
