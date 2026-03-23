"""dispute_evidence_events_mediator

Add dispute evidence, dispute events tables, and mediator_id on tasks.

Revision ID: 0018
Revises: 0017
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── mediator_id on tasks ──────────────────────────────────────────────────
    op.add_column(
        "tasks",
        sa.Column("mediator_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_mediator_id",
        "tasks", "users",
        ["mediator_id"], ["id"],
        ondelete="SET NULL",
    )

    # ── dispute_evidence table ────────────────────────────────────────────────
    op.create_table(
        "dispute_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submitter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submitter_role", sa.String(32), nullable=False),
        sa.Column("evidence_type", sa.String(32), nullable=False, server_default="text"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["submitter_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assignment_id"], ["task_assignments.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_dispute_evidence_task_id", "dispute_evidence", ["task_id"])
    op.create_index("ix_dispute_evidence_submitter_id", "dispute_evidence", ["submitter_id"])

    # ── dispute_events table ──────────────────────────────────────────────────
    op.create_table(
        "dispute_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("event_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()"), index=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_dispute_events_task_id", "dispute_events", ["task_id"])
    op.create_index("ix_dispute_events_created_at", "dispute_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("dispute_events")
    op.drop_table("dispute_evidence")
    op.drop_constraint("fk_tasks_mediator_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "mediator_id")
