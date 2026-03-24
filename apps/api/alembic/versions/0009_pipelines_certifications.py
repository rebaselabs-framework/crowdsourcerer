"""Add task pipelines and worker certifications.

Revision ID: 0009
Revises: 0008
Create Date: 2026-03-23

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Enums ──────────────────────────────────────────────────────────────
    for stmt in [
        "CREATE TYPE pipeline_run_status_enum AS ENUM ('pending', 'running', 'completed', 'failed', 'cancelled')",
        "CREATE TYPE step_run_status_enum AS ENUM ('pending', 'running', 'completed', 'failed')",
    ]:
        op.execute(sa.text(f"DO $$ BEGIN {stmt}; EXCEPTION WHEN duplicate_object THEN NULL; END $$"))

    # ── Task Pipelines ────────────────────────────────────────────────────
    op.create_table(
        "task_pipelines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_pipelines_user_id", "task_pipelines", ["user_id"])
    op.create_index("ix_task_pipelines_org_id", "task_pipelines", ["org_id"])

    op.create_table(
        "task_pipeline_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False, server_default="ai"),
        sa.Column("task_config", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("input_mapping", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pipeline_id"], ["task_pipelines.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_pipeline_steps_pipeline_id", "task_pipeline_steps", ["pipeline_id"])

    op.create_table(
        "task_pipeline_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "completed", "failed", "cancelled",
                    name="pipeline_run_status_enum", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("input", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("output", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["pipeline_id"], ["task_pipelines.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_pipeline_runs_pipeline_id", "task_pipeline_runs", ["pipeline_id"])
    op.create_index("ix_task_pipeline_runs_user_id", "task_pipeline_runs", ["user_id"])
    op.create_index("ix_task_pipeline_runs_status", "task_pipeline_runs", ["status"])

    op.create_table(
        "task_pipeline_step_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "completed", "failed",
                    name="step_run_status_enum", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("input", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("output", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["task_pipeline_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["task_pipeline_steps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_pipeline_step_runs_run_id", "task_pipeline_step_runs", ["run_id"])

    # ── Worker Certifications ─────────────────────────────────────────────
    op.create_table(
        "certifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("passing_score", sa.Integer(), nullable=False, server_default="70"),
        sa.Column("badge_icon", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_type"),
    )
    op.create_index("ix_certifications_task_type", "certifications", ["task_type"])

    op.create_table(
        "certification_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cert_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("question_type", sa.String(32), nullable=False, server_default="single_choice"),
        sa.Column("options", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("correct_answer", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("points", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["cert_id"], ["certifications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_certification_questions_cert_id", "certification_questions", ["cert_id"])

    op.create_table(
        "worker_certifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cert_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("best_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("certified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["worker_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cert_id"], ["certifications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("worker_id", "cert_id", name="uq_worker_cert"),
    )
    op.create_index("ix_worker_certifications_worker_id", "worker_certifications", ["worker_id"])
    op.create_index("ix_worker_certifications_cert_id", "worker_certifications", ["cert_id"])


def downgrade() -> None:
    op.drop_table("worker_certifications")
    op.drop_table("certification_questions")
    op.drop_table("certifications")
    op.drop_table("task_pipeline_step_runs")
    op.drop_table("task_pipeline_runs")
    op.drop_table("task_pipeline_steps")
    op.drop_table("task_pipelines")
    op.execute("DROP TYPE IF EXISTS pipeline_run_status_enum")
    op.execute("DROP TYPE IF EXISTS step_run_status_enum")
