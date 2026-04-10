"""Add missing FK indexes and composite indexes for query performance.

Missing FK indexes cause full table scans on JOIN and CASCADE operations.
Composite indexes cover the most common query patterns identified by audit.

Indexes added:
- worker_endorsements (task_id): FK index
- worker_invites (requester_id): FK index
- org_activity_log (user_id): FK index
- task_templates_marketplace (creator_id): FK index
- ab_participants (user_id): FK index
- sla_breaches (user_id): FK index
- stripe_event_log (user_id): FK index
- task_pipeline_step_runs (step_id): FK index
- task_pipeline_step_runs (task_id): FK index
- notifications (user_id, is_read, created_at): composite for unread count + listing
- task_assignments (worker_id, submitted_at): composite for worker performance queries

(``ix_worker_endorsements_requester_id`` is created in 0052 and was
re-created here by accident.)

Revision ID: 0066
Revises: 0065
"""
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Missing FK indexes (prevent full scans on JOINs and CASCADE deletes) ──

    op.create_index(
        "ix_worker_endorsements_task_id",
        "worker_endorsements",
        ["task_id"],
    )
    op.create_index(
        "ix_worker_invites_requester_id",
        "worker_invites",
        ["requester_id"],
    )
    op.create_index(
        "ix_org_activity_log_user_id",
        "org_activity_log",
        ["user_id"],
    )
    op.create_index(
        "ix_task_templates_marketplace_creator_id",
        "task_templates_marketplace",
        ["creator_id"],
    )
    op.create_index(
        "ix_ab_participants_user_id",
        "ab_participants",
        ["user_id"],
    )
    op.create_index(
        "ix_sla_breaches_user_id",
        "sla_breaches",
        ["user_id"],
    )
    op.create_index(
        "ix_stripe_event_log_user_id",
        "stripe_event_log",
        ["user_id"],
    )
    op.create_index(
        "ix_task_pipeline_step_runs_step_id",
        "task_pipeline_step_runs",
        ["step_id"],
    )
    op.create_index(
        "ix_task_pipeline_step_runs_task_id",
        "task_pipeline_step_runs",
        ["task_id"],
    )

    # ── Composite indexes for hot query paths ──

    # Notification inbox: WHERE user_id=? AND is_read=false ORDER BY created_at DESC
    # Replaces two separate single-column indexes with one covering index.
    op.create_index(
        "ix_notifications_user_read_created",
        "notifications",
        ["user_id", "is_read", "created_at"],
    )

    # Worker performance / leaderboard: WHERE worker_id=? ORDER BY submitted_at DESC
    # Covers worker stats, earnings timeline, admin analytics
    op.create_index(
        "ix_task_assignments_worker_submitted",
        "task_assignments",
        ["worker_id", "submitted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_assignments_worker_submitted", "task_assignments")
    op.drop_index("ix_notifications_user_read_created", "notifications")
    op.drop_index("ix_task_pipeline_step_runs_task_id", "task_pipeline_step_runs")
    op.drop_index("ix_task_pipeline_step_runs_step_id", "task_pipeline_step_runs")
    op.drop_index("ix_stripe_event_log_user_id", "stripe_event_log")
    op.drop_index("ix_sla_breaches_user_id", "sla_breaches")
    op.drop_index("ix_ab_participants_user_id", "ab_participants")
    op.drop_index("ix_task_templates_marketplace_creator_id", "task_templates_marketplace")
    op.drop_index("ix_org_activity_log_user_id", "org_activity_log")
    op.drop_index("ix_worker_invites_requester_id", "worker_invites")
    op.drop_index("ix_worker_endorsements_task_id", "worker_endorsements")
