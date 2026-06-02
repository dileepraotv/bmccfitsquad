"""Background task functions.

Celery has been removed. All tasks run as asyncio coroutines directly inside
the FastAPI process using asyncio.ensure_future() so they do not block the
web server and require zero Redis broker polling.

Usage
-----
    import asyncio
    from app.tasks import send_activity_notification, sync_user_activities

    # Fire-and-forget — returns immediately, runs in the background
    asyncio.ensure_future(send_activity_notification(
        activity_data=strava_api_dict,
        user_id=str(user.id),
    ))
    asyncio.ensure_future(sync_user_activities(user_id=str(user.id)))
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telegram import Bot as TelegramBot, InlineKeyboardButton, InlineKeyboardMarkup

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

async def send_activity_notification(
    *,
    activity_data: dict,
    user_id: str,
    _retry: int = 0,
) -> None:
    """Format and send a new activity notification to the athlete and group chats.

    Runs in the background via asyncio.ensure_future — never blocks the
    webhook handler.  Retries up to 3 times with exponential back-off.
    """
    try:
        await _send_activity_notification_async(
            activity_data=activity_data,
            user_id=user_id,
        )
    except Exception as exc:
        logger.exception(
            "send_activity_notification failed (attempt %s/3) user_id=%s activity=%s",
            _retry + 1,
            user_id,
            activity_data.get("id"),
        )
        if _retry < 2:
            delay = 30 * (2 ** _retry)   # 30s, 60s
            await asyncio.sleep(delay)
            await send_activity_notification(
                activity_data=activity_data,
                user_id=user_id,
                _retry=_retry + 1,
            )


async def _send_activity_notification_async(
    activity_data: dict,
    user_id: str,
) -> None:
    async with AsyncSessionLocal() as db:
        user: User | None = await db.get(User, uuid.UUID(user_id))
        if user is None:
            logger.warning("send_activity_notification: user_id=%s not found", user_id)
            return
        if not user.is_active:
            logger.info("send_activity_notification: user_id=%s inactive — skipped", user_id)
            return

        athlete_name = (
            user.strava_athlete_name
            or user.telegram_first_name
            or f"Athlete {user.telegram_user_id}"
        )

        chats_result = await db.execute(
            select(GroupChat).where(GroupChat.notifications_enabled.is_(True))
        )
        group_chats: list[GroupChat] = chats_result.scalars().all()

        goal_lines = await _build_goal_lines(db, user)
        text = await format_activity_notification(
            activity_data, athlete_name, goal_lines=goal_lines
        )

        activity_id = activity_data.get("id")
        edit_markup = None
        if activity_id:
            edit_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Update Activity",
                    callback_data=f"activity:edit:{activity_id}",
                )
            ]])

        # Reuse a single Bot session for all sends in this notification cycle
        # to avoid per-message TLS handshake overhead.
        bot = TelegramBot(token=settings.telegram_bot_token)
        async with bot:
            # DM the athlete
            try:
                await bot.send_message(
                    chat_id=user.telegram_user_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=edit_markup,
                )
                logger.info("Activity DM sent to telegram_id=%s", user.telegram_user_id)
            except Exception as exc:
                logger.error("Failed to DM user telegram_id=%s: %s", user.telegram_user_id, exc)

            # Broadcast to group chats
            for chat in group_chats:
                try:
                    await bot.send_message(
                        chat_id=chat.id, text=text, parse_mode="Markdown"
                    )
                    logger.info("Notification sent to group chat_id=%s", chat.id)
                except Exception as exc:
                    logger.error("Failed to notify chat_id=%s: %s", chat.id, exc)


# ---------------------------------------------------------------------------
# Task 2: sync_user_activities
# ---------------------------------------------------------------------------

async def sync_user_activities(
    *,
    user_id: str,
    full: bool = False,
    _retry: int = 0,
) -> None:
    """Sync Strava activities for a user.

    Args:
        full: When True, re-fetches the entire Strava history regardless of
              what is already in the DB (use for /fullsync or first connect).
              When False (default), only fetches activities since the latest
              stored date — much faster for day-to-day use.

    Idempotent — uses ON CONFLICT DO NOTHING.
    Retries up to 2 times with exponential back-off on failure.
    """
    try:
        await _sync_user_activities_async(user_id=user_id, full=full)
    except Exception:
        logger.exception("sync_user_activities failed for user_id=%s", user_id)
        if _retry < 1:
            delay = 60 * (2 ** _retry)   # 60s, 120s
            await asyncio.sleep(delay)
            await sync_user_activities(user_id=user_id, full=full, _retry=_retry + 1)


async def _sync_user_activities_async(user_id: str, full: bool = False) -> None:
    """Sync Strava activities for a user.

    full=False (default / /sync): incremental — fetches only since the most
    recent stored activity.  Fast and cheap on Strava API quota.

    full=True (/fullsync or first connect): fetches entire history.
    Use only when the user reports inaccurate statistics.
    """
    async with AsyncSessionLocal() as db:
        user: User | None = await db.get(User, uuid.UUID(user_id))
        if user is None:
            logger.warning("sync_user_activities: user_id=%s not found", user_id)
            return
        if not user.is_active or not user.strava_access_token:
            logger.warning(
                "sync_user_activities: user_id=%s not active or not connected", user_id
            )
            return

        # Determine the `after` timestamp for Strava API pagination
        after_ts: int | None = None

        if not full:
            latest_result = await db.execute(
                select(Activity.activity_date)
                .where(Activity.user_id == user.id)
                .order_by(Activity.activity_date.desc())
                .limit(1)
            )
            latest_row = latest_result.scalar_one_or_none()

            if latest_row is not None:
                # Go back 1 day to catch activities saved slightly out of order
                after_ts = int(latest_row.timestamp()) - 86_400
                logger.info(
                    "sync_user_activities: incremental from %s for user_id=%s",
                    latest_row.isoformat(), user_id,
                )
            else:
                # No data at all — force full even if not requested
                logger.info(
                    "sync_user_activities: no existing data, full fetch for user_id=%s", user_id
                )
        else:
            logger.info(
                "sync_user_activities: FULL re-fetch requested for user_id=%s", user_id
            )

        access_token = await get_valid_access_token(db, user)
        activities = await fetch_activities(access_token, after=after_ts)

        logger.info(
            "sync_user_activities: fetched %s activities for user_id=%s",
            len(activities), user_id,
        )

        # Upsert all activities returned by Strava
        strava_ids: set[int] = set()
        for data in activities:
            strava_id = int(data["id"])
            strava_ids.add(strava_id)

            activity_date = _parse_strava_date(
                data.get("start_date") or data.get("start_date_local")
            )
            is_indoor = (
                bool(data.get("trainer", False))
                or str(data.get("sport_type") or data.get("type", "")).startswith("Virtual")
            )
            stmt = (
                pg_insert(Activity)
                .values(
                    strava_activity_id=strava_id,
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

        # On a full sync, reconcile deletions — remove any DB rows whose
        # strava_activity_id is no longer present in the API response.
        # This catches activities deleted on Strava while the bot was offline
        # or before the webhook subscription was active.
        deleted_count = 0
        if full and strava_ids:
            db_ids_result = await db.execute(
                select(Activity.strava_activity_id)
                .where(Activity.user_id == user.id)
            )
            db_ids: set[int] = {row[0] for row in db_ids_result.fetchall()}
            orphaned = db_ids - strava_ids
            if orphaned:
                from sqlalchemy import delete as sa_delete
                await db.execute(
                    sa_delete(Activity).where(
                        and_(
                            Activity.user_id == user.id,
                            Activity.strava_activity_id.in_(orphaned),
                        )
                    )
                )
                deleted_count = len(orphaned)
                logger.info(
                    "sync_user_activities: removed %s stale activities for user_id=%s: %s",
                    deleted_count, user_id, orphaned,
                )

        await db.commit()
        logger.info(
            "sync_user_activities: upserted=%s deleted=%s for user_id=%s",
            len(activities), deleted_count, user_id,
        )


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

_SPORT_ACTIVITY_TYPES: dict[str, list[str]] = {
    "Ride": [
        "Ride", "VirtualRide", "EBikeRide", "GravelRide",
        "MountainBikeRide", "EMountainBikeRide", "Handcycle",
        "Velomobile",
    ],
    "RideEndurance": [
        "Ride", "VirtualRide", "EBikeRide", "GravelRide",
        "MountainBikeRide", "EMountainBikeRide",
    ],
    "Run":  ["Run", "VirtualRun", "TrailRun"],
    "Walk": ["Walk", "Hike"],
    "Swim": ["Swim", "OpenWaterSwim"],
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
    """Return compact goal-status lines for the notification footer."""
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
        end_dt = datetime(
            g.end_date.year, g.end_date.month, g.end_date.day, tzinfo=timezone.utc
        ) + timedelta(days=1)

        act_types   = _SPORT_ACTIVITY_TYPES.get(g.activity_type, [g.activity_type])
        threshold_m = _parse_category_threshold(g.category)

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
        sport_emoji = {
            "Ride": "🚴", "RideEndurance": "🚴",
            "Run": "🏃", "Walk": "🚶", "Swim": "🏊",
        }.get(g.activity_type, "🏅")
        lines.append(
            f"{sport_emoji} {sport_label} {g.category} - {achieved}/{g.target_count}"
        )

    return lines


def _parse_strava_date(date_str: str | None) -> datetime:
    if not date_str:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(date_str.rstrip("Z")).replace(tzinfo=timezone.utc)


def _optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
