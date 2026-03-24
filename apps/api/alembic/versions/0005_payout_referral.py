"""Add payout_requests, referrals tables and referral_code/credits_pending to users.

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-23

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Users: referral_code + credits_pending ─────────────────────────────
    op.add_column("users", sa.Column("referral_code", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("credits_pending", sa.Integer(), nullable=False,
                                     server_default="0"))
    op.create_index("ix_users_referral_code", "users", ["referral_code"], unique=True)

    # ── payout enums ──────────────────────────────────────────────────────
    for stmt in [
        "CREATE TYPE payout_status_enum AS ENUM ('pending', 'processing', 'paid', 'rejected')",
        "CREATE TYPE payout_method_enum AS ENUM ('paypal', 'bank_transfer', 'crypto')",
    ]:
        op.execute(sa.text(f"DO $$ BEGIN {stmt}; EXCEPTION WHEN duplicate_object THEN NULL; END $$"))

    # ── payout_requests table ─────────────────────────────────────────────
    op.create_table(
        "payout_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credits_requested", sa.Integer(), nullable=False),
        sa.Column("usd_amount", sa.Float(), nullable=False),
        sa.Column("status", sa.Enum("pending", "processing", "paid", "rejected",
                                    name="payout_status_enum", create_type=False), nullable=False,
                  server_default="pending"),
        sa.Column("payout_method", sa.Enum("paypal", "bank_transfer", "crypto",
                                           name="payout_method_enum", create_type=False), nullable=False),
        sa.Column("payout_details", postgresql.JSON(), nullable=False),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_payout_requests_worker_id", "payout_requests", ["worker_id"])
    op.create_index("ix_payout_requests_status", "payout_requests", ["status"])

    # ── referrals table ───────────────────────────────────────────────────
    op.create_table(
        "referrals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("referrer_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("referred_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
                  unique=True),
        sa.Column("referrer_bonus_credits", sa.Integer(), nullable=False,
                  server_default="50"),
        sa.Column("referred_bonus_credits", sa.Integer(), nullable=False,
                  server_default="50"),
        sa.Column("bonus_paid", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_referrals_referrer_id", "referrals", ["referrer_id"])
    op.create_index("ix_referrals_referred_id", "referrals", ["referred_id"], unique=True)


def downgrade() -> None:
    op.drop_table("referrals")
    op.drop_table("payout_requests")
    op.drop_index("ix_users_referral_code", "users")
    op.drop_column("users", "credits_pending")
    op.drop_column("users", "referral_code")
    sa.Enum(name="payout_status_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="payout_method_enum").drop(op.get_bind(), checkfirst=True)
