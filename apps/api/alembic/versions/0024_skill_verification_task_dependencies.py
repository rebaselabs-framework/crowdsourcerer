"""Add skill verification fields and task_dependencies table.

Revision ID: 0024
Revises: 0023
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Skill verification columns on worker_skills ──────────────────────────
    op.add_column(
        "worker_skills",
        sa.Column("verified", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "worker_skills",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── task_dependencies table ───────────────────────────────────────────────
    op.create_table(
        "task_dependencies",
        sa.Column(
            "id",
            PGUUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "task_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "depends_on_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("task_id", "depends_on_id", name="uq_task_dependency"),
    )
    op.create_index(
        "ix_task_dependencies_task_id",
        "task_dependencies",
        ["task_id"],
    )
    op.create_index(
        "ix_task_dependencies_depends_on_id",
        "task_dependencies",
        ["depends_on_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_dependencies_depends_on_id", table_name="task_dependencies")
    op.drop_index("ix_task_dependencies_task_id", table_name="task_dependencies")
    op.drop_table("task_dependencies")
    op.drop_column("worker_skills", "verified_at")
    op.drop_column("worker_skills", "verified")
