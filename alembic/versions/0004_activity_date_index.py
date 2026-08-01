"""Add activity_date indexes — backs stats/goals/recap per-user date-range
aggregation and the /leaderboard all-user date-range scan, none of which
had activity_date indexed before (only user_id and strava_activity_id
were).

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-01 20:15:00.000000
"""
from __future__ import annotations

from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_activities_user_id_activity_date",
        "activities",
        ["user_id", "activity_date"],
    )
    op.create_index(
        "ix_activities_activity_date",
        "activities",
        ["activity_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_activities_activity_date", table_name="activities")
    op.drop_index("ix_activities_user_id_activity_date", table_name="activities")
