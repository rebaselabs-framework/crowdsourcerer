"""Add A/B experiments, worker onboarding, SLA breaches.

Revision ID: 0012
Revises: 0011
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── A/B Experiments ──────────────────────────────────────────────────────
    op.create_table(
        "ab_experiments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("hypothesis", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("task_type", sa.String(32), nullable=True),
        sa.Column("primary_metric", sa.String(32), nullable=False,
                  server_default="completion_rate"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("winner_variant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_ab_experiments_user_id", "ab_experiments", ["user_id"])

    op.create_table(
        "ab_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ab_experiments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("traffic_pct", sa.Float(), nullable=False, server_default="50.0"),
        sa.Column("task_config", postgresql.JSONB(), nullable=True),
        sa.Column("is_control", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("participant_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_accuracy", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("total_duration_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_credits_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_ab_variants_experiment_id", "ab_variants", ["experiment_id"])

    op.create_table(
        "ab_participants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ab_experiments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ab_variants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, unique=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("accuracy", sa.Float(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("credits_used", sa.Integer(), nullable=True),
    )
    op.create_index("ix_ab_participants_experiment_id", "ab_participants", ["experiment_id"])
    op.create_index("ix_ab_participants_variant_id", "ab_participants", ["variant_id"])

    # ── Worker Onboarding ─────────────────────────────────────────────────────
    op.create_table(
        "onboarding_progress",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("step_profile", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("step_explore", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("step_first_task", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("step_skills", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("step_cert", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bonus_claimed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_onboarding_progress_user_id", "onboarding_progress",
                    ["user_id"], unique=True)

    # ── SLA Breaches ──────────────────────────────────────────────────────────
    op.create_table(
        "sla_breaches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("plan", sa.String(16), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("sla_hours", sa.Float(), nullable=False),
        sa.Column("task_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("breach_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credits_refunded", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_sla_breaches_task_id", "sla_breaches", ["task_id"], unique=True)
    op.create_index("ix_sla_breaches_user_id", "sla_breaches", ["user_id"])


def downgrade() -> None:
    op.drop_table("sla_breaches")
    op.drop_table("onboarding_progress")
    op.drop_table("ab_participants")
    op.drop_table("ab_variants")
    op.drop_table("ab_experiments")
