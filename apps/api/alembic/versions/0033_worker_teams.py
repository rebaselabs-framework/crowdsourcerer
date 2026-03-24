"""Worker teams — worker-side collaboration groups with invite system.

Revision ID: 0033
Revises: 0032
Create Date: 2026-03-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── worker_teams ─────────────────────────────────────────────────────────
    op.create_table(
        "worker_teams",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("avatar_emoji", sa.String(8), nullable=True, server_default="'👥'"),
        sa.Column("created_by", PGUUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_worker_teams_created_by", "worker_teams", ["created_by"])

    # ── worker_team_members ───────────────────────────────────────────────────
    op.create_table(
        "worker_team_members",
        sa.Column("team_id", PGUUID(as_uuid=True), sa.ForeignKey("worker_teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", PGUUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="'member'"),  # owner | member
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("team_id", "user_id"),
    )
    op.create_index("ix_worker_team_members_user_id", "worker_team_members", ["user_id"])

    # ── worker_team_invites ───────────────────────────────────────────────────
    op.create_table(
        "worker_team_invites",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("team_id", PGUUID(as_uuid=True), sa.ForeignKey("worker_teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invitee_id", PGUUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invited_by", PGUUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "accepted", "declined", name="team_invite_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("team_id", "invitee_id", name="uq_team_invite_pending",
                            deferrable=True),
    )
    op.create_index("ix_worker_team_invites_invitee_id", "worker_team_invites", ["invitee_id"])
    op.create_index("ix_worker_team_invites_team_id",    "worker_team_invites", ["team_id"])


def downgrade() -> None:
    op.drop_table("worker_team_invites")
    op.execute("DROP TYPE IF EXISTS team_invite_status")
    op.drop_table("worker_team_members")
    op.drop_table("worker_teams")
