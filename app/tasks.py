"""Celery task definitions.

All tasks execute async code via ``asyncio.run()``, which is safe in Celery
workers because each worker process has no pre-existing event loop.

Telegram messages are sent directly through the Bot HTTP API (not through the
PTB Application instance) so these tasks work in the Celery worker process
independently of the FastAPI web process.

Dispatch examples
-----------------
    from app.tasks import send_activity_notification, sync_user_activities

    # After saving a new activity from Strava webhook:
    send_activity_notification.delay(
        activity_data=strava_api_dict,
        user_id=str(user.id),          # UUID string
    )

    # After a user connects Strava:
    sync_user_activities.delay(user_id=str(user.id))
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telegram import Bot as TelegramBot

from app.celery_app import celery_app
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import Activity, Goal, GroupChat, User
from app.strava.auth import get_valid_access_token
from app.strava.client import fetch_activities
from app.telegram.notifications import format_activity_notification

logger = logging.getLogger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# Task 1: send_activity_notification
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    name="tasks.send_activity_notification",
    max_retries=3,
    default_retry_delay=30,
)
def send_activity_notification(
    self,
    *,
    activity_data: dict,
    user_id: str,
) -> None:
    """Format and broadcast a new Strava activity to all group chats.

    This task is dispatched by the Strava webhook handler immediately after
    the activity is saved to the database.

    Args:
        activity_data: Full Strava activity detail dict (Strava API format).
                       Must contain at minimum: name, type, start_date,
                       distance, moving_time, elapsed_time, total_elevation_gain.
        user_id:       ``User.id`` as a UUID string — used to look up the
                       athlete name and Telegram user ID from the database.
    """
    try:
        asyncio.run(
            _send_activity_notification_async(
                activity_data=activity_data,
                user_id=user_id,
            )
        )
    except Exception as exc:
        logger.exception(
            "send_activity_notification failed (attempt %s/%s) user_id=%s activity=%s",
            self.request.retries + 1,
            self.max_retries + 1,
            user_id,
            activity_data.get("id"),
        )
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


async def _send_activity_notification_async(
    activity_data: dict,
    user_id: str,
) -> None:
    async with AsyncSessionLocal() as db:
        # ------------------------------------------------------------------
        # 1. Fetch user from DB to get telegram_user_id and strava_athlete_name
        # ------------------------------------------------------------------
        user: User | None = await db.get(User, uuid.UUID(user_id))

        if user is None:
            logger.warning("send_activity_notification: user_id=%s not found", user_id)
            return
        if not user.is_active:
            logger.info("send_activity_notification: user_id=%s is inactive — skipped", user_id)
            return

        athlete_name = (
            user.strava_athlete_name
            or user.telegram_first_name
            or f"Athlete {user.telegram_user_id}"
        )

        # ------------------------------------------------------------------
        # 2. Load all group chats with notifications enabled
        # ------------------------------------------------------------------
        chats_result = await db.execute(
            select(GroupChat).where(GroupChat.notifications_enabled.is_(True))
        )
        group_chats: list[GroupChat] = chats_result.scalars().all()

        # ------------------------------------------------------------------
        # 3. Build goal summary lines for the footer
        # ------------------------------------------------------------------
        goal_lines = await _build_goal_lines(db, user)

        # ------------------------------------------------------------------
        # 4. Format the notification
        # ------------------------------------------------------------------
        text = await format_activity_notification(activity_data, athlete_name, goal_lines=goal_lines)

        # ------------------------------------------------------------------
        # 4. Send via Telegram Bot API directly (no PTB Application needed)
        # ------------------------------------------------------------------
        bot = TelegramBot(token=settings.telegram_bot_token)

        # Always DM the athlete directly so they get a confirmation even if
        # no group chats are configured yet.
        try:
            async with bot:
                await bot.send_message(
                    chat_id=user.telegram_user_id,
                    text=text,
                    parse_mode="Markdown",
                )
            logger.info(
                "Activity notification DM sent to telegram_id=%s", user.telegram_user_id
            )
        except Exception as exc:
            logger.error(
                "Failed to DM user telegram_id=%s: %s", user.telegram_user_id, exc
            )

        # Also broadcast to any registered group chats
        if not group_chats:
            logger.info(
                "No group chats configured — skipping group broadcast for user=%s",
                user_id,
            )
            return

        for chat in group_chats:
            try:
                async with bot:
                    await bot.send_message(chat_id=chat.id, text=text, parse_mode="Markdown")
                logger.info(
                    "Activity notification sent to group chat_id=%s user_id=%s",
                    chat.id,
                    user_id,
                )
            except Exception as exc:
                logger.error(
                    "Failed to notify chat_id=%s for user_id=%s: %s",
                    chat.id,
                    user_id,
                    exc,
                )


# ---------------------------------------------------------------------------
# Task 2: sync_user_activities
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    name="tasks.sync_user_activities",
    max_retries=2,
    default_retry_delay=60,
)
def sync_user_activities(self, *, user_id: str) -> None:
    """Back-fill activity history for a newly connected user.

    Fetches all Strava activities since Jan 1 of the current year and upserts
    them into the activities table.  Uses ``ON CONFLICT DO NOTHING`` so re-runs
    are fully idempotent.

    Triggered from the OAuth callback after a user successfully connects Strava.

    Args:
        user_id: ``User.id`` as a UUID string.
    """
    try:
        asyncio.run(_sync_user_activities_async(user_id=user_id))
    except Exception as exc:
        logger.exception("sync_user_activities failed for user_id=%s", user_id)
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


async def _sync_user_activities_async(user_id: str) -> None:
    async with AsyncSessionLocal() as db:
        # ------------------------------------------------------------------
        # 1. Fetch user
        # ------------------------------------------------------------------
        user: User | None = await db.get(User, uuid.UUID(user_id))

        if user is None:
            logger.warning("sync_user_activities: user_id=%s not found", user_id)
            return
        if not user.is_active or not user.strava_access_token:
            logger.warning(
                "sync_user_activities: user_id=%s not active or not connected — skipping",
                user_id,
            )
            return

        # ------------------------------------------------------------------
        # 2. Fetch ALL activities from Strava (no time bound — full history)
        # ------------------------------------------------------------------
        access_token = await get_valid_access_token(db, user)
        activities = await fetch_activities(access_token)

        logger.info(
            "sync_user_activities: fetched %s activities for user_id=%s",
            len(activities),
            user_id,
        )

        if not activities:
            return

        # ------------------------------------------------------------------
        # 3. Upsert each activity — ON CONFLICT DO NOTHING is idempotent
        # ------------------------------------------------------------------
        for data in activities:
            activity_date = _parse_strava_date(
                data.get("start_date") or data.get("start_date_local")
            )
            is_indoor = (
                bool(data.get("trainer", False))
                or str(data.get("type", "")).startswith("Virtual")
            )

            stmt = (
                pg_insert(Activity)
                .values(
                    strava_activity_id=int(data["id"]),
                    user_id=user.id,
                    activity_name=data.get("name") or "Unnamed Activity",
                    activity_type=data.get("sport_type") or data.get("type") or "Unknown",
                    activity_date=activity_date,
                    distance_meters=float(data.get("distance") or 0),
                    moving_time_seconds=int(data.get("moving_time") or 0),
                    elapsed_time_seconds=int(data.get("elapsed_time") or 0),
                    elevation_gain=float(data.get("total_elevation_gain") or 0),
                    average_speed=float(data.get("average_speed") or 0),
                    max_speed=float(data.get("max_speed") or 0),
                    average_heartrate=_optional_float(data.get("average_heartrate")),
                    max_heartrate=_optional_float(data.get("max_heartrate")),
                    calories=_optional_float(data.get("calories")),
                    is_indoor=is_indoor,
                )
                .on_conflict_do_nothing(index_elements=["strava_activity_id"])
            )
            await db.execute(stmt)

        await db.commit()
        logger.info(
            "sync_user_activities: sync complete — %s activities processed for user_id=%s",
            len(activities),
            user_id,
        )


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

_SPORT_ACTIVITY_TYPES: dict[str, list[str]] = {
    "Ride":          ["Ride", "VirtualRide"],
    "RideEndurance": ["Ride", "VirtualRide"],
    "Run":           ["Run", "VirtualRun"],
    "Walk":          ["Walk", "Hike"],
    "Swim":          ["Swim", "OpenWaterSwim"],
}


def _parse_category_threshold(category: str) -> float:
    try:
        parts = category.strip().split()
        val  = float(parts[0].replace(",", "."))
        unit = parts[1].lower() if len(parts) > 1 else "km"
        return val * 1_000 if unit == "km" else val
    except (IndexError, ValueError):
        return 0.0


async def _build_goal_lines(db, user: User) -> list[str]:
    """Return a list of formatted goal-status strings for the notification footer.

    Example output lines:
        "Goal 1: Run — 10 km — Status 3/10"
        "Goal 2: Ride — 100 km — Status 17/50"
    Returns an empty list if the user has no active goals.
    """
    from datetime import timedelta

    goals_res = await db.execute(
        select(Goal).where(Goal.user_id == user.id, Goal.is_active == True)  # noqa: E712
    )
    goals = goals_res.scalars().all()
    if not goals:
        return []

    lines: list[str] = []
    for i, g in enumerate(goals, start=1):
        start_dt = datetime(
            g.start_date.year, g.start_date.month, g.start_date.day, tzinfo=timezone.utc
        )
        # end_date is inclusive — add 1 day for exclusive SQL upper bound
        end_dt = datetime(
            g.end_date.year, g.end_date.month, g.end_date.day, tzinfo=timezone.utc
        ) + timedelta(days=1)

        act_types    = _SPORT_ACTIVITY_TYPES.get(g.activity_type, [g.activity_type])
        threshold_m  = _parse_category_threshold(g.category)

        count_result = await db.execute(
            select(func.count(Activity.id)).where(
                and_(
                    Activity.user_id == user.id,
                    Activity.activity_type.in_(act_types),
                    Activity.activity_date >= start_dt,
                    Activity.activity_date < end_dt,
                    Activity.distance_meters >= threshold_m,
                )
            )
        )
        achieved = count_result.scalar_one() or 0

        sport_label = "Ride Endurance" if g.activity_type == "RideEndurance" else g.activity_type
        lines.append(
            f"Goal {i}: {sport_label} — {g.category} — Status {achieved}/{g.target_count}"
        )

    return lines


def _parse_strava_date(date_str: str | None) -> datetime:
    """Parse a Strava ISO 8601 string into a UTC-aware datetime."""
    if not date_str:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(date_str.rstrip("Z")).replace(tzinfo=timezone.utc)


def _optional_float(value) -> float | None:
    """Return float(value) or None if the value is falsy or unconvertible."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
