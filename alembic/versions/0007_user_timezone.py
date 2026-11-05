"""Add users.timezone — per-user IANA timezone for personal stats/goals/
recap/streak calendar boundaries.

Existing rows backfill to "Asia/Kolkata" (BMCC's original, still-largest
user base) so nobody's existing data shifts. Auto-detected from Strava's
athlete profile going forward for new connections, overridable via the new
/timezone command.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-05 19:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "timezone",
            sa.Text(),
            nullable=False,
            server_default="Asia/Kolkata",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "timezone")
