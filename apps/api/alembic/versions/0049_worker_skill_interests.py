"""Add worker_skill_interests column to users table

Revision ID: 0049
Revises: 0048
Create Date: 2026-03-26

Workers can now declare which task types they are interested in before they
have accumulated any earned-proficiency history.  This seeds the skill-based
task feed for new workers who would otherwise receive an empty feed.

The column stores a JSON array of task-type strings, e.g.:
  ["label_image", "verify_fact", "moderate_content"]

A NULL value (pre-migration rows) is treated identically to an empty array by
the application layer.
"""
from alembic import op
import sqlalchemy as sa


revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "worker_skill_interests",
            sa.JSON,
            nullable=True,
            comment="Task types the worker is interested in (declared preference, not earned proficiency)",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "worker_skill_interests")
