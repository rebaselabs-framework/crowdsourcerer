"""api_key_usage_skill_quiz

Add API key usage log table, skill quiz tables, and api_key counters.

Revision ID: 0017
Revises: 0016
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── API key counter columns ───────────────────────────────────────────────
    op.add_column("api_keys", sa.Column("request_count", sa.Integer(), nullable=False,
                                         server_default="0"))
    op.add_column("api_keys", sa.Column("total_credits_used", sa.Integer(), nullable=False,
                                         server_default="0"))

    # ── API key usage logs ────────────────────────────────────────────────────
    op.create_table(
        "api_key_usage_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint", sa.String(256), nullable=False),
        sa.Column("method", sa.String(8), nullable=False, server_default="GET"),
        sa.Column("status_code", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("response_time_ms", sa.Integer(), nullable=True),
        sa.Column("credits_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_api_key_usage_logs_api_key_id", "api_key_usage_logs", ["api_key_id"])
    op.create_index("ix_api_key_usage_logs_user_id", "api_key_usage_logs", ["user_id"])
    op.create_index("ix_api_key_usage_logs_created_at", "api_key_usage_logs", ["created_at"])

    # ── Skill quiz questions ──────────────────────────────────────────────────
    op.create_table(
        "skill_quiz_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill_category", sa.String(64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),        # list of 4 strings
        sa.Column("correct_index", sa.Integer(), nullable=False),  # 0-3
        sa.Column("difficulty", sa.Integer(), nullable=False, server_default="1"),   # 1-3
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skill_quiz_questions_skill_category", "skill_quiz_questions", ["skill_category"])

    # ── Skill quiz attempts ───────────────────────────────────────────────────
    op.create_table(
        "skill_quiz_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill_category", sa.String(64), nullable=False),
        sa.Column("question_ids", sa.JSON(), nullable=False),   # list of UUIDs
        sa.Column("answers", sa.JSON(), nullable=False),         # list of int indices
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("proficiency_level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["worker_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_skill_quiz_attempts_worker_id", "skill_quiz_attempts", ["worker_id"])


def downgrade() -> None:
    op.drop_table("skill_quiz_attempts")
    op.drop_table("skill_quiz_questions")
    op.drop_index("ix_api_key_usage_logs_created_at", "api_key_usage_logs")
    op.drop_index("ix_api_key_usage_logs_user_id", "api_key_usage_logs")
    op.drop_index("ix_api_key_usage_logs_api_key_id", "api_key_usage_logs")
    op.drop_table("api_key_usage_logs")
    op.drop_column("api_keys", "total_credits_used")
    op.drop_column("api_keys", "request_count")
