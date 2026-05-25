"""Initial schema — users, activities, goals, group_chats.

Revision ID: 0001
Revises:
Create Date: 2026-05-25 00:00:00.000000
"""
from __future__ import annotations

import uuid
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # users
    # -----------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_username", sa.Text(), nullable=True),
        sa.Column("telegram_first_name", sa.Text(), nullable=False),
        sa.Column("strava_athlete_id", sa.BigInteger(), nullable=True),
        sa.Column("strava_athlete_name", sa.Text(), nullable=True),
        # Fernet-encrypted; never store plaintext tokens in the DB
        sa.Column("strava_access_token", sa.Text(), nullable=True),
        sa.Column("strava_refresh_token", sa.Text(), nullable=True),
        sa.Column("strava_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_users_telegram_user_id", "users", ["telegram_user_id"], unique=True)
    op.create_index("ix_users_strava_athlete_id", "users", ["strava_athlete_id"], unique=True)

    # -----------------------------------------------------------------------
    # activities
    # -----------------------------------------------------------------------
    op.create_table(
        "activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("strava_activity_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("activity_name", sa.Text(), nullable=False),
        sa.Column("activity_type", sa.Text(), nullable=False),
        sa.Column("activity_date", sa.DateTime(timezone=True), nullable=False),
        # Distance & time
        sa.Column("distance_meters", sa.Float(), nullable=False, server_default="0"),
        sa.Column("moving_time_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("elapsed_time_seconds", sa.Integer(), nullable=False, server_default="0"),
        # Elevation & speed
        sa.Column("elevation_gain", sa.Float(), nullable=False, server_default="0"),
        sa.Column("average_speed", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_speed", sa.Float(), nullable=False, server_default="0"),
        # Optional metrics
        sa.Column("average_heartrate", sa.Float(), nullable=True),
        sa.Column("max_heartrate", sa.Float(), nullable=True),
        sa.Column("calories", sa.Float(), nullable=True),
        sa.Column("is_indoor", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_activities_strava_activity_id", "activities", ["strava_activity_id"], unique=True
    )
    op.create_index("ix_activities_user_id", "activities", ["user_id"])

    # -----------------------------------------------------------------------
    # goals
    # -----------------------------------------------------------------------
    op.create_table(
        "goals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("activity_type", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_goals_user_id", "goals", ["user_id"])

    # -----------------------------------------------------------------------
    # group_chats
    # -----------------------------------------------------------------------
    op.create_table(
        "group_chats",
        sa.Column("id", sa.BigInteger(), primary_key=True),  # Telegram chat ID
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("group_chats")
    op.drop_index("ix_goals_user_id", table_name="goals")
    op.drop_table("goals")
    op.drop_index("ix_activities_user_id", table_name="activities")
    op.drop_index("ix_activities_strava_activity_id", table_name="activities")
    op.drop_table("activities")
    op.drop_index("ix_users_strava_athlete_id", table_name="users")
    op.drop_index("ix_users_telegram_user_id", table_name="users")
    op.drop_table("users")
