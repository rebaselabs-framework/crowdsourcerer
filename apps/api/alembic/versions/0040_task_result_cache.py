"""Add task_result_cache table for AI task result deduplication.

When a requester submits identical AI task inputs, we return the cached
output immediately and charge only a nominal cache-hit fee — saving credits
and external API quota.

Revision ID: 0040
Revises: 0039
Create Date: 2026-03-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_result_cache",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),   # hex SHA-256
        sa.Column("output", JSONB, nullable=False),
        sa.Column("full_credits_cost", sa.Integer, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("hit_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("task_type", "input_hash", name="uq_cache_type_hash"),
    )
    op.create_index("ix_cache_task_type",   "task_result_cache", ["task_type"])
    op.create_index("ix_cache_input_hash",  "task_result_cache", ["input_hash"])
    op.create_index("ix_cache_expires_at",  "task_result_cache", ["expires_at"])
    op.create_index("ix_cache_created_at",  "task_result_cache", ["created_at"])
    # Composite for fast cache lookup
    op.create_index(
        "ix_cache_type_hash",
        "task_result_cache",
        ["task_type", "input_hash"],
    )


def downgrade() -> None:
    op.drop_table("task_result_cache")
