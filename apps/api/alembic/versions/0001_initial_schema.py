"""Initial schema — users, api_keys, tasks, credit_transactions

Revision ID: 0001
Revises:
Create Date: 2026-03-23

"""
from __future__ import annotations
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── users ─────────────────────────────────────────────────────────────────
    # Enum types are auto-created by sa.Enum() during create_table.
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column(
            "plan",
            sa.Enum("free", "starter", "pro", "enterprise", name="plan_enum"),
            nullable=False,
            server_default="free",
        ),
        sa.Column("credits", sa.Integer, nullable=False, server_default="100"),
        sa.Column("stripe_customer_id", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_stripe_customer_id", "users", ["stripe_customer_id"], unique=True)

    # ── api_keys ──────────────────────────────────────────────────────────────
    op.create_table(
        "api_keys",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("scopes", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)

    # ── tasks ─────────────────────────────────────────────────────────────────
    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            sa.Enum(
                "web_research", "entity_lookup", "document_parse", "data_transform",
                "llm_generate", "screenshot", "audio_transcribe", "pii_detect",
                "code_execute", "web_intel",
                name="task_type_enum",

            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "queued", "running", "completed", "failed", "cancelled",
                name="task_status_enum",

            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "priority",
            sa.Enum("low", "normal", "high", "urgent", name="task_priority_enum"),
            nullable=False,
            server_default="normal",
        ),
        sa.Column("input", sa.JSON, nullable=False),
        sa.Column("output", sa.JSON, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("credits_used", sa.Integer, nullable=True),
        sa.Column("duration_ms", sa.BigInteger, nullable=True),
        sa.Column("webhook_url", sa.String(2048), nullable=True),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tasks_user_id", "tasks", ["user_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])

    # ── credit_transactions ────────────────────────────────────────────────────
    op.create_table(
        "credit_transactions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("amount", sa.Integer, nullable=False),
        sa.Column(
            "type",
            sa.Enum("charge", "credit", "refund", name="transaction_type_enum"),
            nullable=False,
        ),
        sa.Column("description", sa.String(512), nullable=False),
        sa.Column("stripe_payment_intent", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_credit_transactions_user_id", "credit_transactions", ["user_id"])


def downgrade() -> None:
    op.drop_table("credit_transactions")
    op.drop_table("tasks")
    op.drop_table("api_keys")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS transaction_type_enum")
    op.execute("DROP TYPE IF EXISTS task_priority_enum")
    op.execute("DROP TYPE IF EXISTS task_status_enum")
    op.execute("DROP TYPE IF EXISTS task_type_enum")
    op.execute("DROP TYPE IF EXISTS plan_enum")
