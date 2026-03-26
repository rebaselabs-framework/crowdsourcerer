"""Add indexes on sla_breaches hot-path columns

Revision ID: 0047
Revises: 0046
Create Date: 2026-03-26

The admin SLA endpoints filter and sort on sla_breaches columns that have
no index:

  sla_breaches.breach_at   — WHERE breach_at >= since (summary + list)
                             ORDER BY breach_at DESC (list)
  sla_breaches.resolved_at — WHERE resolved_at IS NULL/NOT NULL (list filter)
  sla_breaches.plan        — WHERE plan = ? (optional list filter)
  sla_breaches.priority    — WHERE priority = ? (optional list filter)

Without these, every admin SLA query performs a full table scan.
"""
from alembic import op


revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_sla_breaches_breach_at", "sla_breaches", ["breach_at"])
    op.create_index("ix_sla_breaches_resolved_at", "sla_breaches", ["resolved_at"])
    op.create_index("ix_sla_breaches_plan", "sla_breaches", ["plan"])
    op.create_index("ix_sla_breaches_priority", "sla_breaches", ["priority"])


def downgrade() -> None:
    op.drop_index("ix_sla_breaches_priority", table_name="sla_breaches")
    op.drop_index("ix_sla_breaches_plan", table_name="sla_breaches")
    op.drop_index("ix_sla_breaches_resolved_at", table_name="sla_breaches")
    op.drop_index("ix_sla_breaches_breach_at", table_name="sla_breaches")
