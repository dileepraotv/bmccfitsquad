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
import time
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
from app.utils import DURATION_BASED_SPORTS as _DURATION_BASED_SPORTS
from app.utils import SPORT_ACTIVITY_TYPES as _SPORT_ACTIVITY_TYPES

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Task reference keeper — prevents GC of fire-and-forget tasks
# ---------------------------------------------------------------------------
# asyncio.ensure_future / create_task hold only a *weak* reference internally.
# If nothing else holds a strong reference the task can be garbage-collected
# before it runs, silently dropping work.  Store tasks here until they finish.
_background_tasks: set[asyncio.Task] = set()


def fire_and_forget(coro) -> asyncio.Task:
    """Schedule *coro* as a background task and keep a strong reference.

    Use this everywhere instead of ``asyncio.ensure_future`` or
    ``asyncio.create_task`` so tasks are never silently dropped by the GC.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ---------------------------------------------------------------------------
# In-process GroupChat cache
# ---------------------------------------------------------------------------
# The notification-enabled group chat list changes only when someone
# explicitly adds/removes the bot or toggles notifications — essentially
# never compared to how often it's read (every single new activity). A
# plain module-level cache (not Redis) is enough since it only needs to be
# correct within one process, and skips a network round-trip entirely
# rather than trading a Postgres query for a Redis one.

_group_chats_cache: list[GroupChat] | None = None
_group_chats_cache_at: float = 0.0
_GROUP_CHATS_CACHE_TTL_S = 300


async def _get_notification_group_chats(db) -> list[GroupChat]:
    global _group_chats_cache, _group_chats_cache_at
    now = time.monotonic()
    if _group_chats_cache is not None and (now - _group_chats_cache_at) < _GROUP_CHATS_CACHE_TTL_S:
        return _group_chats_cache

    result = await db.execute(select(GroupChat).where(GroupChat.notifications_enabled.is_(True)))
    _group_chats_cache = list(result.scalars().all())
    _group_chats_cache_at = now
    return _group_chats_cache


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

        group_chats: list[GroupChat] = await _get_notification_group_chats(db)

        goal_lines = await _build_goal_lines(db, user)
        text = await format_activity_notification(
            activity_data,
            athlete_name,
            goal_lines=goal_lines,
            roast_mode_enabled=user.roast_mode_enabled,
        )

        activity_id = activity_data.get("id")
        edit_markup = None
        if activity_id:
            edit_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Update Activity",
                    callback_data=f"activity:edit:{activity_id}",
                ),
                InlineKeyboardButton(
                    "Dismiss",
                    callback_data="activity:dismiss",
                ),
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
                    disable_web_page_preview=True,
                )
                logger.info("Activity DM sent to telegram_id=%s", user.telegram_user_id)
            except Exception as exc:
                logger.error("Failed to DM user telegram_id=%s: %s", user.telegram_user_id, exc)

            # Broadcast to group chats
            for chat in group_chats:
                try:
                    await bot.send_message(
                        chat_id=chat.id, text=text, parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
                    logger.info("Notification sent to group chat_id=%s", chat.id)
                except Exception as exc:
                    logger.error("Failed to notify chat_id=%s: %s", chat.id, exc)


# ---------------------------------------------------------------------------
# Possible-duplicate detection
# ---------------------------------------------------------------------------
# Strava's API has never exposed a delete-activity endpoint (removed in
# 2017), so the bot cannot clean up a duplicate on the user's behalf — the
# best we can do is flag it and point the user at the Strava app. This is a
# pure alert: nothing is excluded from stats/goals/leaderboard automatically.

_DUPLICATE_TIME_WINDOW = timedelta(minutes=2)
_DUPLICATE_DURATION_TOLERANCE_FLOOR_S = 30  # minimum tolerance for very short activities


async def _find_possible_duplicate(
    db,
    *,
    user_id: uuid.UUID,
    activity_type: str,
    activity_date: datetime,
    moving_time_seconds: int,
    exclude_strava_id: int,
) -> Activity | None:
    """Look for an existing activity that looks like the same workout.

    Matches on same user + same sport + start time within ±2 minutes +
    moving time within ±10% (or ±30s, whichever is larger). Deliberately
    conservative — a false negative just means no alert, a false positive
    means an unnecessary (but harmless) heads-up message.
    """
    tolerance_s = max(_DUPLICATE_DURATION_TOLERANCE_FLOOR_S, moving_time_seconds * 0.1)
    result = await db.execute(
        select(Activity)
        .where(
            Activity.user_id == user_id,
            Activity.activity_type == activity_type,
            Activity.strava_activity_id != exclude_strava_id,
            Activity.activity_date >= activity_date - _DUPLICATE_TIME_WINDOW,
            Activity.activity_date <= activity_date + _DUPLICATE_TIME_WINDOW,
            func.abs(Activity.moving_time_seconds - moving_time_seconds) <= tolerance_s,
        )
        .order_by(Activity.activity_date)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def send_possible_duplicate_alert(
    *,
    user_id: str,
    strava_ids: list[int],
    activity_type: str,
) -> None:
    """DM the athlete that a cluster of activities looks like the same workout
    logged more than once (2 or more strava_ids — always sent as ONE message
    per cluster, never one message per pair, so a 6-way duplicate doesn't
    turn into 15 separate DMs).

    Informational only — never touches stats, goals, or the leaderboard.
    Strava's API has no delete-activity endpoint, so the message directs
    the user to clean it up manually in the Strava app if they agree.
    """
    if len(strava_ids) < 2:
        return

    async with AsyncSessionLocal() as db:
        user: User | None = await db.get(User, uuid.UUID(user_id))
        if user is None or not user.is_active:
            return
        telegram_user_id = user.telegram_user_id

    sport_label = _sport_display_label(activity_type)
    n = len(strava_ids)
    links = "\n".join(
        f"{i}. [Activity](https://www.strava.com/activities/{sid})"
        for i, sid in enumerate(strava_ids, start=1)
    )
    text = (
        "⚠️ *Possible duplicate activity*\n\n"
        f"`We found {n} {sport_label} activities that look like the same workout "
        f"— same start time, same duration:`\n\n"
        f"{links}\n\n"
        "`If your device or app uploaded this more than once, please review them and "
        "delete the extra copies directly in the Strava app — the bot can't delete "
        "activities on your behalf (Strava's API doesn't support it).`"
    )
    bot = TelegramBot(token=settings.telegram_bot_token)
    async with bot:
        try:
            await bot.send_message(
                chat_id=telegram_user_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            logger.info(
                "Possible-duplicate alert sent to telegram_id=%s ids=%s",
                telegram_user_id, strava_ids,
            )
        except Exception as exc:
            logger.error(
                "Failed to send duplicate alert to telegram_id=%s: %s", telegram_user_id, exc
            )


async def _find_duplicate_clusters(user_id: uuid.UUID | None = None) -> list[dict]:
    """Scan stored activities for possible-duplicate clusters.

    Finds every pair of activities for the same user + same sport whose
    start times are within 2 minutes and durations within 10% (min 30s),
    then unions overlapping pairs into clusters (so e.g. 6 mutually-duplicate
    uploads become ONE cluster, not 15 pairwise entries). Pass user_id to
    scope the scan to a single athlete (used by the /duplicates command);
    leave it None to scan the whole database (used by the ops backfill scan).
    """
    from sqlalchemy import text as _text

    params: dict = {}
    user_filter = ""
    if user_id is not None:
        user_filter = "AND a.user_id = :user_id"
        params["user_id"] = str(user_id)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(_text(
            f"""
            SELECT
                a.user_id             AS user_id,
                a.strava_activity_id  AS a_id,
                a.activity_date       AS a_date,
                b.strava_activity_id  AS b_id,
                b.activity_date       AS b_date,
                a.activity_type       AS activity_type
            FROM activities a
            JOIN activities b
              ON a.user_id = b.user_id
             AND a.activity_type = b.activity_type
             AND a.strava_activity_id < b.strava_activity_id
             AND ABS(EXTRACT(EPOCH FROM (a.activity_date - b.activity_date))) <= 120
             AND ABS(a.moving_time_seconds - b.moving_time_seconds) <=
                 GREATEST(30, a.moving_time_seconds * 0.1)
            {user_filter}
            ORDER BY a.user_id, a.activity_date
            """
        ), params)).fetchall()

    # Union-find, keyed by (user_id, activity_type) so clusters never span
    # sports even if two strava_activity_ids happen to collide across users.
    parent: dict[tuple, tuple] = {}

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(x, y):
        rx, ry = _find(x), _find(y)
        if rx != ry:
            parent[rx] = ry

    dates: dict[tuple, datetime] = {}
    for row in rows:
        a_key = (str(row.user_id), row.activity_type, row.a_id)
        b_key = (str(row.user_id), row.activity_type, row.b_id)
        parent.setdefault(a_key, a_key)
        parent.setdefault(b_key, b_key)
        dates[a_key] = row.a_date
        dates[b_key] = row.b_date
        _union(a_key, b_key)

    clusters: dict[tuple, list[tuple]] = {}
    for key in parent:
        clusters.setdefault(_find(key), []).append(key)

    cluster_list: list[dict] = []
    for members in clusters.values():
        members_sorted = sorted(members, key=lambda k: dates[k])
        c_user_id, activity_type, _ = members_sorted[0]
        cluster_list.append({
            "user_id": c_user_id,
            "activity_type": activity_type,
            "strava_ids": [m[2] for m in members_sorted],
        })

    return cluster_list


async def scan_and_alert_duplicates(*, dry_run: bool = True) -> dict:
    """One-off historical scan (all users) for possible-duplicate activity
    clusters already sitting in the database — the backfill counterpart to
    _find_possible_duplicate, which only looks at newly-ingested activities
    going forward. DMs each affected athlete once per cluster. Set
    dry_run=True (default) to preview without sending anything.
    """
    cluster_list = await _find_duplicate_clusters()

    users_affected: set[str] = {c["user_id"] for c in cluster_list}
    sent = 0
    if not dry_run:
        for c in cluster_list:
            await send_possible_duplicate_alert(
                user_id=c["user_id"],
                strava_ids=c["strava_ids"],
                activity_type=c["activity_type"],
            )
            sent += 1

    return {
        "dry_run": dry_run,
        "clusters_found": len(cluster_list),
        "users_affected": len(users_affected),
        "alerts_sent": sent,
        "clusters": cluster_list,
    }


async def check_duplicates_for_user(user_id: str) -> list[dict]:
    """User-triggered version of the duplicate scan (/duplicates command).

    Scans only this athlete's own history and immediately DMs one alert per
    cluster found (reusing the exact same message as the live/backfill
    paths). Returns the cluster list so the caller can report a count.
    """
    cluster_list = await _find_duplicate_clusters(uuid.UUID(user_id))
    for c in cluster_list:
        await send_possible_duplicate_alert(
            user_id=c["user_id"],
            strava_ids=c["strava_ids"],
            activity_type=c["activity_type"],
        )
    return cluster_list


class _NotConnected(Exception):
    """Raised internally when a user has no valid Strava connection to sync.

    This is deliberately distinct from a real failure — it must never be
    reported to the user as "sync complete" (nothing ran) nor trigger the
    retry loop (retrying won't help until they reconnect).
    """


# ---------------------------------------------------------------------------
# Task 2: sync_user_activities
# ---------------------------------------------------------------------------

async def sync_user_activities(
    *,
    user_id: str,
    full: bool = False,
    notify_telegram_id: int | None = None,
    _retry: int = 0,
) -> None:
    """Sync Strava activities for a user.

    Args:
        full:               When True, re-fetches the entire Strava history
                            (use for /fullsync or first connect).
        notify_telegram_id: If set, DM this Telegram user ID when the sync
                            completes (or fails after all retries).

    Idempotent — uses ON CONFLICT DO NOTHING.
    Retries up to 2 times with exponential back-off on failure.
    """
    try:
        await _sync_user_activities_async(user_id=user_id, full=full)
        if notify_telegram_id:
            msg = (
                "✅ *Full sync complete\\!* Your entire Strava history has been "
                "rebuilt\\. Use /stats to see your updated numbers\\."
                if full else
                "✅ *Sync complete\\!* Use /stats to see your latest numbers\\."
            )
            try:
                bot = TelegramBot(token=settings.telegram_bot_token)
                async with bot:
                    await bot.send_message(
                        chat_id=notify_telegram_id,
                        text=msg,
                        parse_mode="MarkdownV2",
                    )
            except Exception:
                logger.warning("sync completion DM failed for telegram_id=%s", notify_telegram_id)
    except _NotConnected:
        # Nothing actually ran — never send a "success" message for this.
        logger.info(
            "sync_user_activities: user_id=%s has no valid Strava connection — skipped",
            user_id,
        )
        if notify_telegram_id:
            try:
                bot = TelegramBot(token=settings.telegram_bot_token)
                async with bot:
                    await bot.send_message(
                        chat_id=notify_telegram_id,
                        text=(
                            "⚠️ Your Strava connection isn't active\\. "
                            "Use /connect to relink your account, then try again\\."
                        ),
                        parse_mode="MarkdownV2",
                    )
            except Exception:
                logger.warning("sync not-connected DM failed for telegram_id=%s", notify_telegram_id)
    except Exception:
        logger.exception("sync_user_activities failed for user_id=%s", user_id)
        if _retry < 1:
            delay = 60 * (2 ** _retry)   # 60s, 120s
            await asyncio.sleep(delay)
            await sync_user_activities(
                user_id=user_id,
                full=full,
                notify_telegram_id=notify_telegram_id,
                _retry=_retry + 1,
            )
        elif notify_telegram_id:
            try:
                bot = TelegramBot(token=settings.telegram_bot_token)
                async with bot:
                    await bot.send_message(
                        chat_id=notify_telegram_id,
                        text="⚠️ Sync ran into an issue\\. Please try again in a moment\\.",
                        parse_mode="MarkdownV2",
                    )
            except Exception:
                logger.warning("sync failure DM failed for telegram_id=%s", notify_telegram_id)


async def _sync_user_activities_async(user_id: str, full: bool = False) -> None:
    """Sync Strava activities for a user.

    full=False (default / /sync): incremental — fetches only since the most
    recent stored activity.  Fast and cheap on Strava API quota.

    full=True (/fullsync or first connect): fetches entire history.
    Use only when the user reports inaccurate statistics.

    Raises:
        _NotConnected: If the user doesn't exist, is inactive, or has no
            valid Strava token — the caller must not report this as success.
    """
    async with AsyncSessionLocal() as db:
        user: User | None = await db.get(User, uuid.UUID(user_id))
        if user is None:
            logger.warning("sync_user_activities: user_id=%s not found", user_id)
            raise _NotConnected()
        if not user.is_active or not user.strava_access_token:
            logger.warning(
                "sync_user_activities: user_id=%s not active or not connected", user_id
            )
            raise _NotConnected()

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

def _parse_category_threshold(category: str) -> float:
    try:
        parts = category.strip().split()
        val  = float(parts[0].replace(",", "."))
        unit = parts[1].lower() if len(parts) > 1 else "km"
        return val * 1_000 if unit == "km" else val
    except (IndexError, ValueError):
        return 0.0


def _parse_duration_threshold_s(category: str) -> float:
    """Convert a "30 min" style duration category to seconds."""
    try:
        return float(category.strip().split()[0].replace(",", ".")) * 60
    except (IndexError, ValueError):
        return 0.0


_SPORT_TYPE_MAP_REVERSE = {
    "RideEndurance":     "Ride Endurance",
    "RacketSports":      "Racket Sports",
    "StrengthTraining":  "Strength Training",
}


def _sport_display_label(activity_type: str) -> str:
    return _SPORT_TYPE_MAP_REVERSE.get(activity_type, activity_type)


async def get_goal_achieved_count(db, user: User, goal: Goal) -> int:
    """Count of activities satisfying *goal* so far this period.

    Cached briefly per-goal (see key_goal_count) since this exact query is
    run independently by both the activity-notification goal footer
    (_build_goal_lines, below) and the /goals status screen
    (handlers._show_goal_status) — often within seconds of each other for
    the same goal. A fresh activity for this user always recomputes and
    re-caches before the cache is read again, so staleness in practice is
    bounded by the TTL, not by "did an activity get added since".
    """
    from app.redis_client import get_redis, key_goal_count
    from app.redis_client import _GOAL_COUNT_CACHE_TTL_SECONDS as _TTL

    redis = await get_redis()
    cache_key = key_goal_count(goal.id)
    try:
        cached = await redis.get(cache_key)
        if cached is not None:
            return int(cached)
    except Exception:
        pass

    start_dt = datetime(
        goal.start_date.year, goal.start_date.month, goal.start_date.day, tzinfo=timezone.utc
    )
    end_dt = datetime(
        goal.end_date.year, goal.end_date.month, goal.end_date.day, tzinfo=timezone.utc
    ) + timedelta(days=1)
    act_types = _SPORT_ACTIVITY_TYPES.get(goal.activity_type, [goal.activity_type])

    if goal.activity_type in _DURATION_BASED_SPORTS:
        threshold_s = _parse_duration_threshold_s(goal.category)
        metric_filter = Activity.moving_time_seconds >= threshold_s
    else:
        threshold_m = _parse_category_threshold(goal.category)
        metric_filter = Activity.distance_meters >= threshold_m

    count_result = await db.execute(
        select(func.count(Activity.id)).where(
            and_(
                Activity.user_id == user.id,
                Activity.activity_type.in_(act_types),
                Activity.activity_date >= start_dt,
                Activity.activity_date < end_dt,
                metric_filter,
            )
        )
    )
    achieved = count_result.scalar_one() or 0

    try:
        await redis.set(cache_key, str(achieved), ex=_TTL)
    except Exception:
        logger.warning("goal count cache: failed to write cache for goal_id=%s", goal.id)

    return achieved


async def _build_goal_lines(db, user: User) -> list[tuple[str, str]]:
    """Return (label, value) pairs for the notification's goal-progress
    footer — e.g. ("🧘 Yoga 30 min", "3/8") — so they render as an aligned
    monospace column via format_kv_lines, same as the metrics block."""
    goals_res = await db.execute(
        select(Goal).where(Goal.user_id == user.id, Goal.is_active == True)  # noqa: E712
    )
    goals = goals_res.scalars().all()
    if not goals:
        return []

    lines: list[tuple[str, str]] = []
    for i, g in enumerate(goals, start=1):
        achieved = await get_goal_achieved_count(db, user, g)

        sport_label = _sport_display_label(g.activity_type)
        sport_emoji = {
            "Ride": "🚴", "RideEndurance": "🚴",
            "Run": "🏃", "Walk": "🚶", "Swim": "🏊",
            "Hiking": "🥾", "Yoga": "🧘",
            "RacketSports": "🏸", "StrengthTraining": "🏋️",
        }.get(g.activity_type, "🏅")
        lines.append(
            (f"{sport_emoji} {sport_label} {g.category}", f"{achieved}/{g.target_count}")
        )

    return lines


# ---------------------------------------------------------------------------
# Task 3: catchup_sync_all_users  (cron safety net)
# ---------------------------------------------------------------------------
#
# Architecture (see conversation "Strava daily API quota" fix):
#
# The old version of this task called Strava's /athlete/activities once per
# CONNECTED USER on every single cron tick, regardless of whether anything
# had actually gone wrong. That scales Strava usage as O(users x ticks) —
# at 500 users on a 5-minute tick that's ~144,000 calls/day against a
# 2,000/day quota. This version makes catch-up event-driven instead of a
# blind poll, in three layers, each only spending Strava calls when there's
# real evidence something might have been missed:
#
#   Layer 1 (repair pending webhook events) — retries any WebhookEvent row
#     left unprocessed by a crash/restart. Cost: 1 Strava call per row that
#     is ACTUALLY pending, typically zero on a healthy tick. See
#     process_pending_webhook_events().
#
#   Layer 2 (heartbeat-gated outage recovery) — only runs a full per-user
#     Strava scan when a gap in the /ping + /cron/sync-all heartbeat proves
#     the process was actually unreachable (not just quiet), and only for
#     the gap window itself, not a blind rolling lookback. Cost: 0 on every
#     healthy tick; O(users) only right after a genuine outage.
#
#   Layer 3 (daily rotation safety net) — a distant backstop in case Layers
#     1-2 miss something (e.g. a silent bug). Each user is checked at most
#     once per rotation window (default 24h), spread evenly across ticks via
#     a deterministic hash — so total daily cost is O(users), not
#     O(users x ticks), regardless of how often the cron actually fires.
#
#   Layer 4 (monthly full-history reconciliation) — Layers 1-3 only ever
#     look at *recent* activity (an incremental fetch since the last known
#     timestamp), so they can't catch things that don't show up as "new":
#     an activity edited long ago, a type change, or a deletion that
#     happened while a webhook was missed. Each user gets one full /fullsync
#     -equivalent re-fetch per calendar month, on a fixed day derived from
#     their user id (1-28, spread evenly), so the cost is O(users/28) per
#     day, not O(users) all at once. See monthly_reconcile_sweep().
#
# All four layers respect Strava's rate-limit headers via
# app.strava.client.is_rate_limited() and back off before ever hitting 429.

# Must match webhook.py so both systems agree on dedup key lifetime.
_DEDUP_TTL_SECONDS = 86_400  # 24 hours

# Heartbeat gap beyond which we assume the process was genuinely unreachable
# (not just a quiet night with no user activity) and worth a bounded repair
# scan. ~2.4x the nominal 5-min keep-alive cadence, to absorb one missed
# ping without false-triggering a full scan.
_HEARTBEAT_GAP_THRESHOLD_SECONDS = 720  # 12 minutes

# Layer 3: each user is swept at most once per this window, spread evenly
# across ticks — bounds the safety-net's total Strava usage to ~users/day
# regardless of actual cron frequency.
_DAILY_SWEEP_WINDOW_SECONDS = 86_400  # 24 hours
_DAILY_SWEEP_SLOTS = 288  # 5-minute-equivalent granularity within the window

# Layer 4: each user is assigned a fixed day-of-month (1-28) on which their
# entire Strava history is reconciled. Capped at 28 (not the calendar's 28-31)
# so every user gets exactly one reconciliation day every month regardless
# of how long that month is.
_MONTHLY_RECONCILE_DAYS = 28
# Longer than any month so a user can never be double-reconciled even if
# their assigned day is re-evaluated on a later cron tick before the key
# would otherwise expire.
_MONTHLY_RECONCILE_DEDUP_TTL_SECONDS = 40 * 86_400


async def _sync_recent_activities_for_user(
    user: User, *, after_ts: int, redis, source: str,
) -> tuple[int, int]:
    """Fetch activities for one user since after_ts and insert any new ones.

    Shared by the Layer 2 (outage gap) and Layer 3 (daily rotation) scans —
    both need the exact same "fetch, dedup, insert, notify" pipeline, just
    triggered for different reasons (hence the *source* label in logs).

    Batches the dedup checks (one pipelined Redis round-trip + one DB query
    for the whole page of activities, instead of one Redis EXISTS + one DB
    session per activity) since this is the hot path during an outage-gap
    catchup, where a single tick can be processing many missed activities
    across many users against a 5-connection Postgres pool.

    Returns (activities_seen, new_activities_inserted).
    """
    from app.redis_client import key_activity_seen

    async with AsyncSessionLocal() as db:
        user_db = await db.get(User, user.id)
        if not user_db:
            return (0, 0)
        access_token = await get_valid_access_token(db, user_db)
        user_id_str = str(user_db.id)

    activities = await fetch_activities(access_token, after=after_ts)
    if not activities:
        return (0, 0)

    strava_ids = [int(a["id"]) for a in activities]
    dedup_keys = [key_activity_seen(sid) for sid in strava_ids]
    try:
        pipe = redis.pipeline()
        for k in dedup_keys:
            pipe.exists(k)
        seen_flags = await pipe.execute()
    except Exception:
        logger.warning("catchup_sync[%s]: batched dedup check failed, falling back to no pre-filter", source)
        seen_flags = [False] * len(dedup_keys)
    already_seen_ids = {sid for sid, flag in zip(strava_ids, seen_flags) if flag}

    candidates = [a for a in activities if int(a["id"]) not in already_seen_ids]
    if not candidates:
        return (len(activities), 0)

    new_count = 0
    async with AsyncSessionLocal() as db:
        existing_rows = await db.execute(
            select(Activity.strava_activity_id).where(
                Activity.strava_activity_id.in_([int(a["id"]) for a in candidates])
            )
        )
        already_in_db = {row[0] for row in existing_rows}

        for data in candidates:
            strava_id = int(data["id"])
            if strava_id in already_in_db:
                continue

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
            await db.commit()

            possible_duplicate = await _find_possible_duplicate(
                db,
                user_id=user.id,
                activity_type=data.get("sport_type") or data.get("type") or "Unknown",
                activity_date=activity_date,
                moving_time_seconds=int(data.get("moving_time") or 0),
                exclude_strava_id=strava_id,
            )

            dedup_key = key_activity_seen(strava_id)
            try:
                await redis.set(dedup_key, "1", ex=_DEDUP_TTL_SECONDS, nx=True)
            except Exception:
                logger.warning("catchup_sync[%s]: failed to set dedup key for strava_id=%s", source, strava_id)
            fire_and_forget(send_activity_notification(activity_data=data, user_id=user_id_str))
            if possible_duplicate is not None:
                fire_and_forget(send_possible_duplicate_alert(
                    user_id=user_id_str,
                    strava_ids=[possible_duplicate.strava_activity_id, strava_id],
                    activity_type=data.get("sport_type") or data.get("type") or "Unknown",
                ))
            new_count += 1
            logger.info(
                "catchup_sync[%s]: notifying missed activity strava_id=%s user_id=%s",
                source, strava_id, user_id_str,
            )

    return (len(activities), new_count)


async def process_pending_webhook_events(*, max_events: int = 50) -> dict:
    """Layer 1 — retry any durably-queued webhook event left unprocessed.

    This is the primary recovery path: every webhook POST is persisted
    before being acked (see strava/webhook.py), so anything the process
    didn't get around to (crash, transient Strava/DB error) shows up here
    as a WebhookEvent row with processed_at IS NULL. Cost is exactly one
    Strava call per row that is genuinely pending — zero on a healthy tick.
    """
    from app.models import WebhookEvent
    from app.strava.webhook import process_webhook_event

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(WebhookEvent.id)
            .where(WebhookEvent.processed_at.is_(None))
            .order_by(WebhookEvent.received_at)
            .limit(max_events)
        )
        pending_ids = [str(row) for row in result.scalars().all()]

    for event_id in pending_ids:
        try:
            await process_webhook_event(event_id)
        except Exception:
            logger.exception("process_pending_webhook_events: failed event_id=%s", event_id)

    if pending_ids:
        logger.info("process_pending_webhook_events: repaired %s event(s)", len(pending_ids))
    return {"repaired": len(pending_ids)}


async def monthly_reconcile_sweep(users: list[User], *, redis, now: datetime) -> dict:
    """Layer 4 — low-frequency full-history reconciliation safety net.

    Each connected user is assigned a fixed day-of-month (1-28, derived
    deterministically from their user id) on which their entire Strava
    history is silently re-fetched and reconciled against the local DB —
    the exact same path as /fullsync (upsert everything, delete local rows
    no longer present on Strava), just silent (no completion DM) and run
    automatically instead of waiting for the user to notice something's off.

    A Redis dedup key ensures each user is only reconciled once per calendar
    month even though this is called every few minutes on their assigned day.
    """
    from app.redis_client import key_monthly_reconcile
    from app.strava.client import is_rate_limited

    period = f"{now.year}-{now.month:02d}"
    today = now.day
    due_users = [
        u for u in users
        if (u.id.int % _MONTHLY_RECONCILE_DAYS) + 1 == today
    ]

    processed, errors = 0, 0
    for user in due_users:
        dedup_key = key_monthly_reconcile(user.id, period)
        if not await redis.set(dedup_key, "1", ex=_MONTHLY_RECONCILE_DEDUP_TTL_SECONDS, nx=True):
            continue  # already reconciled this user this month
        if await is_rate_limited():
            logger.warning("monthly_reconcile: rate limit reached mid-sweep — stopping")
            break
        try:
            await _sync_user_activities_async(user_id=str(user.id), full=True)
            processed += 1
            logger.info("monthly_reconcile: reconciled user_id=%s", user.id)
        except Exception:
            logger.exception("monthly_reconcile: error for user_id=%s", user.id)
            errors += 1

    if due_users:
        logger.info(
            "monthly_reconcile: day=%s due=%s processed=%s errors=%s",
            today, len(due_users), processed, errors,
        )
    return {"due": len(due_users), "processed": processed, "errors": errors}


async def catchup_sync_all_users() -> dict:
    """Reliability safety net — see module comment above for the 3-layer design.

    Called every few minutes via GET /cron/sync-all. Unlike the old blind
    per-user scan, this only spends Strava API calls when there's concrete
    evidence something might have been missed.
    """
    from app.redis_client import get_redis, key_heartbeat
    from app.strava.client import is_rate_limited

    redis = await get_redis()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    # --- Layer 1: repair anything left unprocessed by a crash/restart -----
    repair_result = await process_pending_webhook_events()

    # --- Heartbeat: read the previous value, then stamp "now" -------------
    prev_heartbeat_raw = await redis.get(key_heartbeat())
    await redis.set(key_heartbeat(), str(now_ts))
    prev_heartbeat = int(prev_heartbeat_raw) if prev_heartbeat_raw else None
    gap_seconds = (now_ts - prev_heartbeat) if prev_heartbeat else None

    if await is_rate_limited():
        logger.warning("catchup_sync: Strava daily usage near quota — skipping bulk scans this tick")
        return {
            "repaired": repair_result["repaired"],
            "outage_gap_seconds": gap_seconds,
            "outage_scan": None,
            "daily_sweep": None,
            "monthly_reconcile": None,
            "rate_limited": True,
        }

    # Single query — active, connected users are needed by both remaining layers
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(
                User.is_active.is_(True),
                User.strava_athlete_id.isnot(None),
                User.strava_access_token.isnot(None),
            )
        )
        users = result.scalars().all()

    outage_scan: dict | None = None
    daily_sweep: dict | None = None

    # --- Layer 2: bounded full scan, only if a real outage gap is detected
    if users and (prev_heartbeat is None or gap_seconds is not None and gap_seconds > _HEARTBEAT_GAP_THRESHOLD_SECONDS):
        # No prior heartbeat (fresh deploy) → be conservative, cover 3h.
        gap_lookback = gap_seconds if gap_seconds is not None else 10_800
        after_ts = now_ts - min(gap_lookback + 300, 10_800)  # +5min pad, capped at 3h
        logger.info(
            "catchup_sync: outage gap detected (%ss) — scanning %s user(s) since %s",
            gap_seconds, len(users), after_ts,
        )
        processed, new_activities, errors = 0, 0, 0
        for user in users:
            try:
                _, new_count = await _sync_recent_activities_for_user(
                    user, after_ts=after_ts, redis=redis, source="outage-gap",
                )
                processed += 1
                new_activities += new_count
            except Exception:
                logger.exception("catchup_sync[outage-gap]: error for user_id=%s", user.id)
                errors += 1
        outage_scan = {"users_processed": processed, "new_activities": new_activities, "errors": errors}

    # --- Layer 3: low-frequency daily rotation safety net ------------------
    if users:
        slot_now = (now_ts % _DAILY_SWEEP_WINDOW_SECONDS) // (
            _DAILY_SWEEP_WINDOW_SECONDS // _DAILY_SWEEP_SLOTS
        )
        sweep_after_ts = now_ts - _DAILY_SWEEP_WINDOW_SECONDS
        due_users = [
            u for u in users
            if (u.id.int % _DAILY_SWEEP_SLOTS) == slot_now
        ]
        if due_users:
            processed, new_activities, errors = 0, 0, 0
            for user in due_users:
                if await is_rate_limited():
                    logger.warning("catchup_sync[daily-sweep]: rate limit reached mid-sweep — stopping")
                    break
                try:
                    _, new_count = await _sync_recent_activities_for_user(
                        user, after_ts=sweep_after_ts, redis=redis, source="daily-sweep",
                    )
                    processed += 1
                    new_activities += new_count
                except Exception:
                    logger.exception("catchup_sync[daily-sweep]: error for user_id=%s", user.id)
                    errors += 1
            daily_sweep = {"users_processed": processed, "new_activities": new_activities, "errors": errors}

    # --- Layer 4: low-frequency monthly full-history reconciliation -------
    monthly_reconcile: dict | None = None
    if users:
        monthly_reconcile = await monthly_reconcile_sweep(
            users, redis=redis, now=datetime.now(timezone.utc)
        )

    logger.info(
        "catchup_sync complete — repaired=%s gap=%ss outage_scan=%s daily_sweep=%s monthly_reconcile=%s",
        repair_result["repaired"], gap_seconds, outage_scan, daily_sweep, monthly_reconcile,
    )
    return {
        "repaired": repair_result["repaired"],
        "outage_gap_seconds": gap_seconds,
        "outage_scan": outage_scan,
        "daily_sweep": daily_sweep,
        "monthly_reconcile": monthly_reconcile,
        "rate_limited": False,
    }


# ---------------------------------------------------------------------------
# Task 4: maybe_send_monthly_recaps  (scheduled monthly recap, per user)
# ---------------------------------------------------------------------------
# Piggybacks on the same /cron/sync-all ping that already runs every few
# minutes — no separate scheduler/cron registration needed. Fires once,
# guarded by a Redis key, the first time this is called at/after 21:00 IST
# on the last calendar day of the month (28/29/30/31 handled via
# calendar.monthrange, so no month-length special-casing is needed).

_IST = timezone(timedelta(hours=5, minutes=30))
_RECAP_HOUR_IST = 21


async def maybe_send_monthly_recaps() -> dict:
    """Check whether it's time for the monthly recap and send it to every
    connected user if so. Safe to call on every cron tick — a Redis flag
    ensures it only actually sends once per calendar month."""
    import calendar as _calendar

    from app.redis_client import get_redis
    from app.stats.recap import get_or_build_recap

    now_ist = datetime.now(_IST)
    last_day = _calendar.monthrange(now_ist.year, now_ist.month)[1]
    if now_ist.day != last_day or now_ist.hour < _RECAP_HOUR_IST:
        return {"skipped": True, "reason": "not yet"}

    redis = await get_redis()
    dedup_key = f"recap:sent:{now_ist.year}-{now_ist.month:02d}"
    if not await redis.set(dedup_key, "1", ex=40 * 86_400, nx=True):
        return {"skipped": True, "reason": "already sent this month"}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(
                User.is_active.is_(True),
                User.strava_athlete_id.isnot(None),
            )
        )
        users = result.scalars().all()

    logger.info("monthly_recap: sending to %s connected user(s)", len(users))

    sent, errors = 0, 0
    bot = TelegramBot(token=settings.telegram_bot_token)
    async with bot:
        for user in users:
            try:
                async with AsyncSessionLocal() as db:
                    text = await get_or_build_recap(db, user, now_ist.year, now_ist.month)

                from app.telegram.keyboards import recap_goal_prompt_keyboard
                from app.telegram.notifications import send_recap_message

                await send_recap_message(
                    bot,
                    user.telegram_user_id,
                    text,
                    reply_markup=recap_goal_prompt_keyboard(),
                )
                sent += 1
            except Exception:
                logger.exception("monthly_recap: failed for telegram_id=%s", user.telegram_user_id)
                errors += 1

    logger.info("monthly_recap complete — sent=%s errors=%s", sent, errors)
    return {"skipped": False, "sent": sent, "errors": errors}


# ---------------------------------------------------------------------------
# Task 5: maybe_send_yearly_recap  (scheduled yearly recap, per user)
# ---------------------------------------------------------------------------
# Fires on the same 21:00 IST / last-day trigger as the monthly recap, but
# only actually does anything on 31 December — and is always run *after*
# maybe_send_monthly_recaps() so December's monthly card lands first,
# immediately followed by the full-year one.

async def maybe_send_yearly_recap() -> dict:
    """Check whether it's time for the yearly recap (21:00 IST on 31 Dec)
    and send it to every connected user if so. Safe to call on every cron
    tick — a Redis flag ensures it only actually sends once per year."""
    from app.redis_client import get_redis
    from app.stats.recap import get_or_build_yearly_recap

    now_ist = datetime.now(_IST)
    if now_ist.month != 12 or now_ist.day != 31 or now_ist.hour < _RECAP_HOUR_IST:
        return {"skipped": True, "reason": "not yet"}

    redis = await get_redis()
    dedup_key = f"yearrecap:sent:{now_ist.year}"
    if not await redis.set(dedup_key, "1", ex=40 * 86_400, nx=True):
        return {"skipped": True, "reason": "already sent this year"}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(
                User.is_active.is_(True),
                User.strava_athlete_id.isnot(None),
            )
        )
        users = result.scalars().all()

    logger.info("yearly_recap: sending to %s connected user(s)", len(users))

    sent, errors = 0, 0
    bot = TelegramBot(token=settings.telegram_bot_token)
    async with bot:
        for user in users:
            try:
                async with AsyncSessionLocal() as db:
                    text = await get_or_build_yearly_recap(db, user, now_ist.year)

                from app.telegram.keyboards import recap_goal_prompt_keyboard
                from app.telegram.notifications import send_recap_message

                await send_recap_message(
                    bot,
                    user.telegram_user_id,
                    text,
                    reply_markup=recap_goal_prompt_keyboard(),
                )
                sent += 1
            except Exception:
                logger.exception("yearly_recap: failed for telegram_id=%s", user.telegram_user_id)
                errors += 1

    logger.info("yearly_recap complete — sent=%s errors=%s", sent, errors)
    return {"skipped": False, "sent": sent, "errors": errors}


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
