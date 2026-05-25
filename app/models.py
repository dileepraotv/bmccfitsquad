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
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


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

    # Human-readable category that defines a single achievement unit.
    # Examples: "100 Km", "10 Km", "1000 m" (elevation)
    category: Mapped[str] = mapped_column(Text, nullable=False)

    # How many times the user wants to hit the category within the date range
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)

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
