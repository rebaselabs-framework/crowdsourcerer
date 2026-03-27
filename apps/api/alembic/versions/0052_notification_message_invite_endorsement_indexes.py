"""Add composite indexes for notifications, task_messages, team invites, endorsements

Revision ID: 0052
Revises: 0051
Create Date: 2026-03-27

Hot-path queries that were missing composite indexes:

1. notifications (user_id, is_read)
   Every notification badge/unread-count query runs:
     SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = false
   The existing standalone user_id index works but scans all of a user's
   notifications; the composite index skips over read ones immediately.
   A second composite on (user_id, is_read, created_at) serves ordered feeds.

2. task_messages (task_id, sender_id, recipient_id)
   Conversation-thread queries need to locate all messages between two parties
   on a task. The existing task_id index works but requires a heap fetch for
   each row to check sender/recipient; the composite index covers the filter.

3. worker_team_invites (invitee_id, status) and (team_id, status)
   "My pending invites" → WHERE invitee_id = ? AND status = 'pending'
   "Pending invites for this team" → WHERE team_id = ? AND status = 'pending'
   Both are extremely frequent; single-column indexes exist but require a heap
   fetch for every row to check status.

4. worker_endorsements (requester_id) and (worker_id, created_at)
   "Endorsements I gave" query needs requester_id index (currently unindexed).
   Profile timeline queries need (worker_id, created_at) for ordered results.
"""
from alembic import op


revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. notifications ────────────────────────────────────────────────────
    # Critical: unread badge count + notification feed
    op.create_index(
        "ix_notifications_user_id_is_read",
        "notifications",
        ["user_id", "is_read"],
    )
    op.create_index(
        "ix_notifications_user_id_is_read_created_at",
        "notifications",
        ["user_id", "is_read", "created_at"],
    )

    # ── 2. task_messages ────────────────────────────────────────────────────
    # Conversation lookups: all messages on a task between two parties
    op.create_index(
        "ix_task_messages_task_sender_recipient",
        "task_messages",
        ["task_id", "sender_id", "recipient_id"],
    )

    # ── 3. worker_team_invites ──────────────────────────────────────────────
    op.create_index(
        "ix_worker_team_invites_invitee_status",
        "worker_team_invites",
        ["invitee_id", "status"],
    )
    op.create_index(
        "ix_worker_team_invites_team_status",
        "worker_team_invites",
        ["team_id", "status"],
    )

    # ── 4. worker_endorsements ──────────────────────────────────────────────
    op.create_index(
        "ix_worker_endorsements_requester_id",
        "worker_endorsements",
        ["requester_id"],
    )
    op.create_index(
        "ix_worker_endorsements_worker_id_created_at",
        "worker_endorsements",
        ["worker_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_worker_endorsements_worker_id_created_at",
        table_name="worker_endorsements",
    )
    op.drop_index(
        "ix_worker_endorsements_requester_id",
        table_name="worker_endorsements",
    )
    op.drop_index(
        "ix_worker_team_invites_team_status",
        table_name="worker_team_invites",
    )
    op.drop_index(
        "ix_worker_team_invites_invitee_status",
        table_name="worker_team_invites",
    )
    op.drop_index(
        "ix_task_messages_task_sender_recipient",
        table_name="task_messages",
    )
    op.drop_index(
        "ix_notifications_user_id_is_read_created_at",
        table_name="notifications",
    )
    op.drop_index(
        "ix_notifications_user_id_is_read",
        table_name="notifications",
    )
