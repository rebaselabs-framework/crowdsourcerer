"""Add quest system: active_quests and quest_progress tables.

Revision ID: 0062
Revises: 0061
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Active quests — generated weekly (3-5 per week)
    op.create_table(
        "active_quests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("quest_key", sa.String(64), nullable=False),       # e.g. "volume_10", "streak_5"
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.String(8), nullable=False),             # emoji
        sa.Column("quest_type", sa.String(32), nullable=False),      # volume, streak, variety, accuracy, challenge
        sa.Column("target_value", sa.Integer(), nullable=False),     # goal to reach
        sa.Column("xp_reward", sa.Integer(), nullable=False),
        sa.Column("credits_reward", sa.Integer(), nullable=False),
        sa.Column("difficulty", sa.String(16), nullable=False),      # easy, medium, hard
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_active_quests_week_start", "active_quests", ["week_start"])

    # Quest progress — per-user tracking for each active quest
    op.create_table(
        "quest_progress",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("quest_id", UUID(as_uuid=True), sa.ForeignKey("active_quests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("current_value", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_complete", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_claimed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extra_data", sa.JSON(), nullable=True),            # e.g. set of task types for variety quest
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("quest_id", "user_id", name="uq_quest_progress"),
    )
    op.create_index("ix_quest_progress_quest_id", "quest_progress", ["quest_id"])
    op.create_index("ix_quest_progress_user_id", "quest_progress", ["user_id"])


def downgrade() -> None:
    op.drop_table("quest_progress")
    op.drop_table("active_quests")
