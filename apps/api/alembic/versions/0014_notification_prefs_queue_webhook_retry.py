"""notification_prefs_queue_webhook_retry

Add notification preferences table and webhook retry_of FK.

Revision ID: 0014
Revises: 0013
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Notification Preferences ──────────────────────────────────────────────
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        # Email toggles
        sa.Column("email_task_completed", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("email_task_failed", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("email_submission_received", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("email_worker_approved", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("email_payout_update", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("email_daily_challenge", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("email_referral_bonus", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("email_sla_breach", sa.Boolean(), nullable=False, server_default="true"),
        # In-app toggles
        sa.Column("notif_task_events", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notif_submissions", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notif_payouts", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notif_gamification", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notif_system", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notification_preferences_user_id", "notification_preferences", ["user_id"], unique=True)

    # ── WebhookLogs: retry_of FK for manual retries ───────────────────────────
    op.add_column(
        "webhook_logs",
        sa.Column("retry_of", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "webhook_logs",
        sa.Column("is_manual_retry", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("webhook_logs", "is_manual_retry")
    op.drop_column("webhook_logs", "retry_of")
    op.drop_index("ix_notification_preferences_user_id", table_name="notification_preferences")
    op.drop_table("notification_preferences")
