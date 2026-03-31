"""Add webhook_delivery_queue table for persistent retry queue.

Replaces in-memory asyncio.sleep() retries with a database-backed queue
that survives server restarts.  Background worker polls every 30 seconds
for due items with exponential backoff (30s -> 2m -> 10m -> 1h -> 4h).

Revision ID: 0058
Revises: 0057
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_delivery_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
                  nullable=True, index=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("payload", postgresql.JSON(), nullable=False),
        sa.Column("headers", postgresql.JSON(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending", index=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    # Composite index for the main polling query: pending items due for retry
    op.create_index(
        "ix_webhook_delivery_queue_poll",
        "webhook_delivery_queue",
        ["status", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_delivery_queue_poll", table_name="webhook_delivery_queue")
    op.drop_table("webhook_delivery_queue")
