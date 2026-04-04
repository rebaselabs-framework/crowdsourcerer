"""Add league system: seasons, groups, group members, and user league_tier.

Revision ID: 0061
Revises: 0060
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enum for season status
    op.execute("CREATE TYPE league_season_status_enum AS ENUM ('active', 'processing', 'completed')")

    # League seasons table
    op.create_table(
        "league_seasons",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("week_start", sa.Date(), nullable=False, unique=True),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "processing", "completed", name="league_season_status_enum", create_type=False),
            server_default="active",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_league_seasons_week_start", "league_seasons", ["week_start"])

    # League groups table
    op.create_table(
        "league_groups",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("season_id", UUID(as_uuid=True), sa.ForeignKey("league_seasons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("group_number", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("season_id", "tier", "group_number", name="uq_league_group"),
    )
    op.create_index("ix_league_groups_season_id", "league_groups", ["season_id"])

    # League group members table
    op.create_table(
        "league_group_members",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", UUID(as_uuid=True), sa.ForeignKey("league_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("xp_earned", sa.Integer(), server_default="0", nullable=False),
        sa.Column("final_rank", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("group_id", "user_id", name="uq_league_group_member"),
    )
    op.create_index("ix_league_group_members_group_id", "league_group_members", ["group_id"])
    op.create_index("ix_league_group_members_user_id", "league_group_members", ["user_id"])

    # Add league_tier column to users
    op.add_column("users", sa.Column("league_tier", sa.String(16), server_default="bronze", nullable=False))


def downgrade() -> None:
    op.drop_column("users", "league_tier")
    op.drop_table("league_group_members")
    op.drop_table("league_groups")
    op.drop_table("league_seasons")
    op.execute("DROP TYPE IF EXISTS league_season_status_enum")
