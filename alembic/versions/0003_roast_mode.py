"""Add users.roast_mode_enabled — opt-out toggle for the contextual
roast/kudos activity notification greeting.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-01 13:30:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "roast_mode_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "roast_mode_enabled")
