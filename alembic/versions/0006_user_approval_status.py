"""Add users.approval_status — join-request gate for new onboarding.

New /start or /connect users are inserted as "pending" and only handed a
Strava OAuth link once the admin approves them via inline Approve/Reject
buttons (see app.telegram.handlers). Existing rows backfill to "approved"
so nobody already using the bot is locked out.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-13 22:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "approval_status",
            sa.Text(),
            nullable=False,
            server_default="approved",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "approval_status")
