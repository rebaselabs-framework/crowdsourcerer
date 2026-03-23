"""Add dispute resolution, task export support, and team/org features.

Revision ID: 0008
Revises: 0007
Create Date: 2026-03-23

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Dispute / consensus fields on tasks ──────────────────────────────
    op.add_column(
        "tasks",
        sa.Column(
            "consensus_strategy",
            sa.String(32),
            nullable=False,
            server_default="any_first",
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("dispute_status", sa.String(32), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "winning_assignment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task_assignments.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # ── Organizations ──────────────────────────────────────────────────
    op.create_table(
        "organizations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("credits", sa.Integer, nullable=False, server_default="0"),
        sa.Column("plan", sa.String(32), nullable=False, server_default="free"),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("avatar_url", sa.String(2048), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_organizations_owner_id", "organizations", ["owner_id"])

    # ── Org members ───────────────────────────────────────────────────
    op.create_table(
        "org_members",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.String(32),
            nullable=False,
            server_default="member",
        ),  # owner | admin | member | viewer
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("org_id", "user_id", name="uq_org_member"),
    )
    op.create_index("ix_org_members_org_id", "org_members", ["org_id"])
    op.create_index("ix_org_members_user_id", "org_members", ["user_id"])

    # ── Org invites ────────────────────────────────────────────────────
    op.create_table(
        "org_invites",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="member"),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "invited_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_org_invites_org_id", "org_invites", ["org_id"])
    op.create_index("ix_org_invites_token", "org_invites", ["token"])

    # ── org_id on tasks (optional — tasks can belong to an org) ───────
    op.add_column(
        "tasks",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_tasks_org_id", "tasks", ["org_id"])

    # ── org_id on users (active org context) ──────────────────────────
    op.add_column(
        "users",
        sa.Column(
            "active_org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "active_org_id")
    op.drop_index("ix_tasks_org_id", table_name="tasks")
    op.drop_column("tasks", "org_id")
    op.drop_index("ix_org_invites_token", table_name="org_invites")
    op.drop_index("ix_org_invites_org_id", table_name="org_invites")
    op.drop_table("org_invites")
    op.drop_index("ix_org_members_user_id", table_name="org_members")
    op.drop_index("ix_org_members_org_id", table_name="org_members")
    op.drop_table("org_members")
    op.drop_index("ix_organizations_owner_id", table_name="organizations")
    op.drop_table("organizations")
    op.drop_column("tasks", "winning_assignment_id")
    op.drop_column("tasks", "dispute_status")
    op.drop_column("tasks", "consensus_strategy")
