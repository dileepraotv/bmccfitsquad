"""Flexible Goal Engine Phase 1 — add metric / aggregation / target_value /
allow_multiple_daily / recurrence columns to goals, with a one-time data
backfill so every existing goal (today's only shape: per-activity threshold
x count, i.e. "frequency" mode) keeps behaving exactly as it does today.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-03 12:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

# Mirrors app.utils.DURATION_BASED_SPORTS at the time this migration was
# written — intentionally duplicated (not imported) since migrations must
# stay correct even if the app module changes shape later.
_DURATION_BASED_SPORTS = {"Yoga", "RacketSports", "StrengthTraining"}


def _parse_category_threshold(category: str) -> float:
    """Mirrors app.tasks._parse_category_threshold at migration time."""
    try:
        parts = category.strip().split()
        val = float(parts[0].replace(",", "."))
        unit = parts[1].lower() if len(parts) > 1 else "km"
        return val * 1_000 if unit == "km" else val
    except (IndexError, ValueError):
        return 0.0


def _parse_duration_threshold_s(category: str) -> float:
    """Mirrors app.tasks._parse_duration_threshold_s at migration time."""
    try:
        return float(category.strip().split()[0].replace(",", ".")) * 60
    except (IndexError, ValueError):
        return 0.0


def upgrade() -> None:
    op.add_column(
        "goals",
        sa.Column("metric", sa.Text(), nullable=False, server_default="distance"),
    )
    op.add_column(
        "goals",
        sa.Column("aggregation", sa.Text(), nullable=False, server_default="frequency"),
    )
    op.add_column(
        "goals",
        sa.Column("target_value", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "goals",
        sa.Column(
            "allow_multiple_daily", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "goals",
        sa.Column("recurrence", sa.Text(), nullable=False, server_default="none"),
    )

    # One-time backfill: every existing row is a "frequency" goal (server
    # default already covers aggregation/allow_multiple_daily/recurrence),
    # so only metric + target_value need computing per-row from the old
    # free-text category string. Skipped in `--sql` offline mode, which has
    # no live connection to SELECT from — there the ADD COLUMN statements
    # above are still emitted for review, just without the backfill.
    if not context.is_offline_mode():
        conn = op.get_bind()
        rows = conn.execute(sa.text("SELECT id, activity_type, category FROM goals")).fetchall()
        for goal_id, activity_type, category in rows:
            if activity_type in _DURATION_BASED_SPORTS:
                metric = "duration"
                target_value = _parse_duration_threshold_s(category or "")
            else:
                metric = "distance"
                target_value = _parse_category_threshold(category or "")
            conn.execute(
                sa.text(
                    "UPDATE goals SET metric = :metric, target_value = :target_value "
                    "WHERE id = :id"
                ),
                {"metric": metric, "target_value": target_value, "id": goal_id},
            )

    # Drop the server defaults now that backfill is complete — application
    # code always sets these columns explicitly on every new goal going
    # forward, so a stale default should never silently paper over a bug.
    op.alter_column("goals", "metric", server_default=None)
    op.alter_column("goals", "aggregation", server_default=None)
    op.alter_column("goals", "target_value", server_default=None)
    op.alter_column("goals", "allow_multiple_daily", server_default=None)
    op.alter_column("goals", "recurrence", server_default=None)


def downgrade() -> None:
    op.drop_column("goals", "recurrence")
    op.drop_column("goals", "allow_multiple_daily")
    op.drop_column("goals", "target_value")
    op.drop_column("goals", "aggregation")
    op.drop_column("goals", "metric")
