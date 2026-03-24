"""Add task ratings, worker portfolio, and notification digest enhancements.

Revision ID: 0028
Revises: 0027
Create Date: 2026-03-23

Changes:
  - users: avg_feedback_score, total_ratings_received, last_digest_sent_at
  - notification_preferences: digest_frequency enum + column
  - task_ratings table
  - worker_portfolio table
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── users: new worker feedback + digest columns ─────────────────────────
    op.add_column("users", sa.Column("avg_feedback_score", sa.Float(), nullable=True))
    op.add_column("users", sa.Column("total_ratings_received", sa.Integer(), nullable=False,
                                     server_default="0"))
    op.add_column("users", sa.Column("last_digest_sent_at", sa.DateTime(timezone=True),
                                     nullable=True))

    # ── notification_preferences: digest_frequency ──────────────────────────
    op.execute("CREATE TYPE digest_frequency_enum AS ENUM ('none', 'daily', 'weekly')")
    op.add_column(
        "notification_preferences",
        sa.Column(
            "digest_frequency",
            sa.Enum("none", "daily", "weekly", name="digest_frequency_enum", create_type=False),
            nullable=False,
            server_default="weekly",
        ),
    )

    # ── task_ratings table ──────────────────────────────────────────────────
    op.create_table(
        "task_ratings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requester_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("worker_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("submission_id", UUID(as_uuid=True),
                  sa.ForeignKey("task_submissions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("task_id", "requester_id", name="uq_task_rating"),
    )
    op.create_index("ix_task_ratings_task_id", "task_ratings", ["task_id"])
    op.create_index("ix_task_ratings_worker_id", "task_ratings", ["worker_id"])

    # ── worker_portfolio table ───────────────────────────────────────────────
    op.create_table(
        "worker_portfolio",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("worker_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pinned_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("worker_id", "task_id", name="uq_portfolio_task"),
    )
    op.create_index("ix_worker_portfolio_worker_id", "worker_portfolio", ["worker_id"])


def downgrade() -> None:
    op.drop_table("worker_portfolio")
    op.drop_table("task_ratings")
    op.drop_column("notification_preferences", "digest_frequency")
    op.execute("DROP TYPE IF EXISTS digest_frequency_enum")
    op.drop_column("users", "last_digest_sent_at")
    op.drop_column("users", "total_ratings_received")
    op.drop_column("users", "avg_feedback_score")
