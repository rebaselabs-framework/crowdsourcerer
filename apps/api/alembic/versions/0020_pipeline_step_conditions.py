"""Add condition/next_on_pass/next_on_fail to pipeline steps.

Revision ID: 0020
Revises: 0019
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_pipeline_steps",
        sa.Column("condition", sa.Text(), nullable=True),
    )
    op.add_column(
        "task_pipeline_steps",
        sa.Column("next_on_pass", sa.Integer(), nullable=True),
    )
    op.add_column(
        "task_pipeline_steps",
        sa.Column("next_on_fail", sa.Integer(), nullable=True),
    )
    # Add "skipped" to the step run status enum
    op.execute("ALTER TYPE step_run_status_enum ADD VALUE IF NOT EXISTS 'skipped'")


def downgrade() -> None:
    op.drop_column("task_pipeline_steps", "next_on_fail")
    op.drop_column("task_pipeline_steps", "next_on_pass")
    op.drop_column("task_pipeline_steps", "condition")
    # Note: PostgreSQL does not support removing values from an enum type
    # The 'skipped' value will remain in the type after downgrade
