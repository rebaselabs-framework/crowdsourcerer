"""0027 — worker_invites + task_watchlist tables

Revision ID: 0027
Revises: 0026
Create Date: 2026-03-23
"""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op
import sqlalchemy.dialects.postgresql as pg

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # invite_status_enum
    op.execute(
        "CREATE TYPE invite_status_enum AS ENUM "
        "('pending', 'accepted', 'declined', 'expired')"
    )

    # worker_invites
    op.create_table(
        "worker_invites",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("worker_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requester_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("status",
                  sa.Enum("pending", "accepted", "declined", "expired",
                          name="invite_status_enum", create_type=False),
                  nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("task_id", "worker_id", name="uq_worker_invite"),
    )
    op.create_index("ix_worker_invites_task_id", "worker_invites", ["task_id"])
    op.create_index("ix_worker_invites_worker_id", "worker_invites", ["worker_id"])
    op.create_index("ix_worker_invites_status", "worker_invites", ["status"])

    # task_watchlist
    op.create_table(
        "task_watchlist",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("worker_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("worker_id", "task_id", name="uq_task_watchlist"),
    )
    op.create_index("ix_task_watchlist_worker_id", "task_watchlist", ["worker_id"])
    op.create_index("ix_task_watchlist_task_id", "task_watchlist", ["task_id"])


def downgrade() -> None:
    op.drop_table("task_watchlist")
    op.drop_table("worker_invites")
    op.execute("DROP TYPE IF EXISTS invite_status_enum")
