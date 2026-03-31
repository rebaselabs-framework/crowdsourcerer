"""Add encryption-at-rest support for webhook secrets.

- Widens ``secret`` column to 512 chars to accommodate encrypted ciphertext.
- Adds ``previous_secret`` and ``previous_secret_expires_at`` columns for
  rotation grace period (24-hour dual-signature window after secret rotation).

Note: This migration does NOT encrypt existing plaintext secrets automatically.
The encryption module handles both plaintext (legacy) and encrypted values
transparently. Existing secrets will be encrypted on the next rotation.

Revision ID: 0059
Revises: 0058
"""
from alembic import op
import sqlalchemy as sa

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Widen secret column for encrypted values
    op.alter_column(
        "webhook_endpoints",
        "secret",
        type_=sa.String(512),
        existing_type=sa.String(128),
        existing_nullable=False,
    )
    # Add rotation grace period columns
    op.add_column(
        "webhook_endpoints",
        sa.Column("previous_secret", sa.String(512), nullable=True),
    )
    op.add_column(
        "webhook_endpoints",
        sa.Column("previous_secret_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("webhook_endpoints", "previous_secret_expires_at")
    op.drop_column("webhook_endpoints", "previous_secret")
    op.alter_column(
        "webhook_endpoints",
        "secret",
        type_=sa.String(128),
        existing_type=sa.String(512),
        existing_nullable=False,
    )
