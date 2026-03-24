"""Add task template marketplace, rate-limit quota tracking.

Revision ID: 0010
Revises: 0009
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Task Template Marketplace ────────────────────────────────────────────
    op.create_table(
        "task_templates_marketplace",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("creator_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("execution_mode",
                  sa.Enum("ai", "human", name="template_exec_mode_enum"),
                  nullable=False, server_default="ai"),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("tags", sa.JSON, nullable=True),
        sa.Column("task_config", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("example_input", sa.JSON, nullable=True),
        sa.Column("is_public", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("is_featured", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("use_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rating_sum", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rating_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_task_templates_marketplace_task_type",
                    "task_templates_marketplace", ["task_type"])
    op.create_index("ix_task_templates_marketplace_category",
                    "task_templates_marketplace", ["category"])

    op.create_table(
        "task_template_ratings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("template_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("task_templates_marketplace.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("template_id", "user_id", name="uq_template_rating"),
    )
    op.create_index("ix_task_template_ratings_template_id",
                    "task_template_ratings", ["template_id"])

    # ── Rate Limit Quota Tracking ────────────────────────────────────────────
    op.create_table(
        "rate_limit_buckets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bucket_key", sa.String(64), nullable=False),  # e.g. "tasks:2026-03-23"
        sa.Column("count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "bucket_key", name="uq_rate_limit_bucket"),
    )
    op.create_index("ix_rate_limit_buckets_user_id",
                    "rate_limit_buckets", ["user_id"])


def downgrade() -> None:
    op.drop_table("rate_limit_buckets")
    op.drop_table("task_template_ratings")
    op.drop_table("task_templates_marketplace")
    op.execute("DROP TYPE IF EXISTS template_exec_mode_enum")
