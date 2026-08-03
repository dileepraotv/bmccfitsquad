"""SQLAlchemy ORM models.

All primary keys are UUIDs generated server-side.  Strava OAuth tokens are
stored as encrypted ciphertext — use app.crypto to encrypt/decrypt them.
"""
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

class User(Base):
    """A Telegram user, optionally linked to a Strava athlete account."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )

    # Telegram identity
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    telegram_username: Mapped[str | None] = mapped_column(Text)
    telegram_first_name: Mapped[str] = mapped_column(Text, nullable=False)

    # Strava identity — all nullable until the user completes OAuth
    strava_athlete_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    strava_athlete_name: Mapped[str | None] = mapped_column(Text)

    # Fernet-encrypted tokens (use app.crypto to read/write)
    strava_access_token: Mapped[str | None] = mapped_column(Text)
    strava_refresh_token: Mapped[str | None] = mapped_column(Text)
    strava_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Misc
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Opt-out (on by default) — swaps the activity notification's greeting
    # for a contextual roast/kudos line based on distance vs sport threshold.
    # See app.telegram.notifications for the selection logic.
    roast_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    # Relationships
    activities: Mapped[list["Activity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    goals: Mapped[list["Goal"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_strava_connected(self) -> bool:
        return self.strava_athlete_id is not None

    def __repr__(self) -> str:
        return (
            f"<User id={self.id} telegram_id={self.telegram_user_id} "
            f"strava_id={self.strava_athlete_id}>"
        )


# ---------------------------------------------------------------------------
# activities
# ---------------------------------------------------------------------------

class Activity(Base):
    """A Strava activity that has been synced for a user."""

    __tablename__ = "activities"
    __table_args__ = (
        # Backs stats/goals/recap: per-user date-range aggregation
        # (activity_date filtered/ordered within one user_id).
        Index("ix_activities_user_id_activity_date", "user_id", "activity_date"),
        # Backs the leaderboard: a date-range scan across *all* users
        # (activity_date >= month_start, grouped by user) with no user_id
        # filter, so it needs activity_date leading rather than user_id.
        # See alembic/versions/0004_activity_date_index.py.
        Index("ix_activities_activity_date", "activity_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )

    # Strava's own ID — used for deduplication
    strava_activity_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)

    # Owner
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Core fields
    activity_name: Mapped[str] = mapped_column(Text, nullable=False)
    activity_type: Mapped[str] = mapped_column(Text, nullable=False)  # Ride, Run, Walk, Swim …
    activity_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Distance & time
    distance_meters: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    moving_time_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    elapsed_time_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Elevation & speed
    elevation_gain: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    average_speed: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_speed: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Optional metrics
    average_heartrate: Mapped[float | None] = mapped_column(Float)
    max_heartrate: Mapped[float | None] = mapped_column(Float)
    calories: Mapped[float | None] = mapped_column(Float)

    is_indoor: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="activities")

    # ---------------------------------------------------------------------------
    # Derived helpers (no DB column, computed on the Python object)
    # ---------------------------------------------------------------------------

    @property
    def distance_km(self) -> float:
        return round(self.distance_meters / 1000, 2)

    @property
    def moving_time_h(self) -> float:
        return round(self.moving_time_seconds / 3600, 2)

    @property
    def average_speed_kmh(self) -> float:
        return round(self.average_speed * 3.6, 2)

    def __repr__(self) -> str:
        return (
            f"<Activity strava_id={self.strava_activity_id} "
            f"type={self.activity_type!r} user_id={self.user_id}>"
        )


# ---------------------------------------------------------------------------
# goals
# ---------------------------------------------------------------------------

class Goal(Base):
    """A time-boxed repetition goal, e.g. complete a 100 km ride 4 times in a month."""

    __tablename__ = "goals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # e.g. "Ride", "RideEndurance", "Run", "Swim", "Walk"
    activity_type: Mapped[str] = mapped_column(Text, nullable=False)

    # Display-cache string, derived from the structured columns below at
    # write time (e.g. "100 km", "Total 150 km"). No longer the source of
    # truth for progress calculation — kept so any legacy display code
    # reading it during the Phase 1 transition keeps working.
    category: Mapped[str] = mapped_column(Text, nullable=False)

    # What's being measured per activity: "distance" | "elevation" | "duration"
    metric: Mapped[str] = mapped_column(Text, nullable=False, default="distance")

    # "cumulative" — sum metric across all qualifying activities vs target_value
    # "frequency"  — count activities where metric >= target_value, vs target_count
    aggregation: Mapped[str] = mapped_column(Text, nullable=False, default="frequency")

    # Canonical-unit target: meters for distance/elevation, seconds for
    # duration. In "frequency" mode this is the per-activity threshold; in
    # "cumulative" mode this is the total target for the whole period.
    target_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # How many times the user wants to hit the threshold within the date
    # range. Only meaningful in "frequency" mode; ignored in "cumulative".
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # If False, only the day's single best activity (by this goal's own
    # metric) counts toward progress — see get_goal_achieved_count().
    allow_multiple_daily: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # "none" | "monthly" | "quarterly" — reserved for the Phase 2 recurrence
    # engine; Phase 1 only ever writes "none".
    recurrence: Mapped[str] = mapped_column(Text, nullable=False, default="none")

    # Goal window
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="goals")

    def __repr__(self) -> str:
        return (
            f"<Goal id={self.id} type={self.activity_type!r} "
            f"category={self.category!r} target={self.target_count}x "
            f"{self.start_date}→{self.end_date}>"
        )


# ---------------------------------------------------------------------------
# group_chats  (unchanged from original scaffold)
# ---------------------------------------------------------------------------

class GroupChat(Base):
    """A Telegram group chat where the bot posts activity notifications."""

    __tablename__ = "group_chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram chat ID
    title: Mapped[str | None] = mapped_column(Text)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    def __repr__(self) -> str:
        return f"<GroupChat id={self.id} title={self.title!r}>"


# ---------------------------------------------------------------------------
# webhook_events  — durable ack/process queue for Strava push events
# ---------------------------------------------------------------------------
# Strava requires a 200 response within 2 seconds of a webhook POST. Rather
# than doing the real work (token refresh, Strava fetch, DB write, Telegram
# notify) inside that request — where a cold start or a mid-flight exception
# can silently lose the event forever — we durably persist the raw payload
# here FIRST and ack immediately. A separate step processes the row and
# marks it done. Any row left unprocessed (crash, transient error) is picked
# up by the next cron tick's repair pass — this is what makes catch-up sync
# event-driven (repair only what's actually pending) instead of a blind
# poll-every-user-every-tick scan. See catchup_sync_all_users() in tasks.py.
class WebhookEvent(Base):
    """One raw Strava push-event payload, durable from ack to processing."""

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )

    object_type: Mapped[str] = mapped_column(Text, nullable=False)   # "activity" | "athlete"
    aspect_type: Mapped[str] = mapped_column(Text, nullable=False)   # "create" | "update" | "delete"
    object_id: Mapped[int] = mapped_column(BigInteger, nullable=False)   # activity_id or athlete_id
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)    # Strava athlete id
    updates_json: Mapped[str | None] = mapped_column(Text)   # raw "updates" dict, JSON-encoded

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return (
            f"<WebhookEvent id={self.id} {self.object_type}/{self.aspect_type} "
            f"object_id={self.object_id} processed={self.processed_at is not None}>"
        )
