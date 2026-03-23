"""Per-API-key rate limits, pipeline step retry, and credit burn alerts.

Revision ID: 0021
Revises: 0020
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Per-API-key rate limits ───────────────────────────────────────────────
    # Add configurable per-key limits on top of the global plan-based limits.
    op.add_column(
        "api_keys",
        sa.Column("rate_limit_rpm", sa.Integer(), nullable=True),  # requests/minute; None = use plan default
    )
    op.add_column(
        "api_keys",
        sa.Column("rate_limit_daily", sa.Integer(), nullable=True),  # requests/day; None = use plan default
    )

    # Sliding-window bucket table for per-key rate tracking
    op.create_table(
        "api_key_rate_buckets",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "api_key_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("window_key", sa.String(64), nullable=False),   # e.g. "rpm:2026-03-23T14:05" or "daily:2026-03-23"
        sa.Column("count", sa.Integer(), default=0, nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("api_key_id", "window_key", name="uq_api_key_rate_bucket"),
    )

    # ── Pipeline step retry ───────────────────────────────────────────────────
    op.add_column(
        "task_pipeline_steps",
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "task_pipeline_step_runs",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    # Add "retrying" status to step run enum
    op.execute("ALTER TYPE step_run_status_enum ADD VALUE IF NOT EXISTS 'retrying'")

    # ── Credit burn-rate alerts ───────────────────────────────────────────────
    op.add_column(
        "users",
        sa.Column("credit_alert_threshold", sa.Integer(), nullable=True),  # fire alert when balance drops below this
    )
    op.add_column(
        "users",
        sa.Column("credit_alert_fired", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("users", "credit_alert_fired")
    op.drop_column("users", "credit_alert_threshold")
    op.drop_column("task_pipeline_step_runs", "retry_count")
    op.drop_column("task_pipeline_steps", "max_retries")
    op.drop_table("api_key_rate_buckets")
    op.drop_column("api_keys", "rate_limit_daily")
    op.drop_column("api_keys", "rate_limit_rpm")
