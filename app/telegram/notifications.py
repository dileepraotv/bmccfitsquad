"""Activity notification formatter and dispatcher.

Public API
----------
  format_activity_notification(activity_dict, athlete_name) -> str
      Formats a raw Strava activity dict into a Telegram message string.

  send_activity_notification(bot, activity, user, chat_ids)
      Converts an ORM Activity to a dict, formats it, and sends it to each chat.

  send_goal_progress_notification(bot, user, goal, progress_pct)
      DMs the user when they hit a goal milestone.

Template anatomy
----------------
  {emoji} New Activity!

  Athlete Name: …
  Activity: …
  Activity Date: YYYY-MM-DDTHH:MM:SS.000Z
  Activity Type: …

  Distance: … km
  Moving Time: HH:MM:SS
  Elapsed Time: HH:MM:SS
  Calories: …
  Avg Speed: … km/h
  Max Speed: … km/h

  Elevation Gain: … meters
  Avg HR: … bpm          ← omitted if no HR data
  Max HR: … bpm          ← omitted if no HR data

  ─────────────────
  {club_message}
  💬 "{random quote}"

  Click /stats to check your updated stats
"""
from __future__ import annotations

import logging
import pathlib
import random
from datetime import datetime, timezone

from telegram import Bot

from app.models import Activity, User
from app.utils import format_strava_date, meters_to_km, ms_to_kmh, seconds_to_hhmmss

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File paths (relative to the project root where uvicorn is launched)
# ---------------------------------------------------------------------------
_DATA_DIR = pathlib.Path("data")
_CLUB_MESSAGE_PATH = _DATA_DIR / "club_message.txt"
_QUOTES_PATH = _DATA_DIR / "quotes.txt"

_SEPARATOR = "─────────────────"

# ---------------------------------------------------------------------------
# Sport emoji map
# ---------------------------------------------------------------------------
_EMOJI: dict[str, str] = {
    "Ride":            "🚴",
    "VirtualRide":     "🚴",
    "EBikeRide":       "🚴",
    "Run":             "🏃",
    "VirtualRun":      "🏃",
    "TrailRun":        "🏃",
    "Walk":            "🚶",
    "Hike":            "🚶",
    "Swim":            "🏊",
    "OpenWaterSwim":   "🏊",
    "WeightTraining":  "🏋️",
    "Workout":         "💪",
    "Yoga":            "🧘",
    "Rowing":          "🚣",
    "Kayaking":        "🚣",
    "Soccer":          "⚽",
    "Tennis":          "🎾",
    "Golf":            "⛳",
    "Crossfit":        "💪",
    "RockClimbing":    "🧗",
    "Skiing":          "⛷️",
    "Snowboard":       "🏂",
    "Skateboard":      "🛹",
}
_DEFAULT_EMOJI = "🏅"


# ---------------------------------------------------------------------------
# Primary public formatter
# ---------------------------------------------------------------------------

async def format_activity_notification(
    activity: dict,
    athlete_name: str,
    goal_lines: list[str] | None = None,
) -> str:
    """Format a raw Strava activity dict into a BMCC Telegram notification.

    Args:
        activity:     Dict with Strava activity fields.
        athlete_name: Full name of the athlete (e.g. "Dileep Rao").
        goal_lines:   Optional pre-built goal status lines to show in the footer.
                      If None or empty the quote + /stats CTA is shown instead.
    """
    activity_type: str = activity.get("sport_type") or activity.get("type") or "Unknown"
    emoji = _EMOJI.get(activity_type, _DEFAULT_EMOJI)
    activity_id   = activity.get("id")
    activity_name = activity.get("name") or "Unnamed Activity"

    if activity_id:
        activity_link = f"[{activity_name}](https://www.strava.com/activities/{activity_id})"
    else:
        activity_link = activity_name

    lines: list[str] = [
        f"{emoji} *New Activity!*",
        "",
        f"Athlete Name: {athlete_name}",
        f"Activity: {activity_link}",
        f"Activity Date: {format_strava_date(activity.get('start_date'))}",
        f"Activity Type: {activity_type}",
    ]

    distance_km   = meters_to_km(activity.get("distance"))
    moving_secs   = int(activity.get("moving_time")  or 0)
    elapsed_secs  = int(activity.get("elapsed_time") or 0)
    calories      = int(activity.get("calories")     or 0)
    avg_speed_kmh = ms_to_kmh(activity.get("average_speed"))
    max_speed_kmh = ms_to_kmh(activity.get("max_speed"))

    lines += [
        "",
        f"Distance: {distance_km:.2f} km",
        f"Moving Time: {seconds_to_hhmmss(moving_secs)}",
        f"Elapsed Time: {seconds_to_hhmmss(elapsed_secs)}",
        f"Calories: {calories}",
        f"Avg Speed: {avg_speed_kmh:.2f} km/h",
        f"Max Speed: {max_speed_kmh:.2f} km/h",
    ]

    elevation_m = activity.get("total_elevation_gain") or 0
    avg_hr      = activity.get("average_heartrate")
    max_hr      = activity.get("max_heartrate")

    lines += ["", f"Elevation Gain: {int(elevation_m)} m"]
    if avg_hr is not None:
        lines.append(f"Avg HR: {int(avg_hr)} bpm")
    if max_hr is not None:
        lines.append(f"Max HR: {int(max_hr)} bpm")

    # ------------------------------------------------------------------
    # Footer — goal summary if available, otherwise quote + CTA
    # ------------------------------------------------------------------
    lines += ["", _SEPARATOR]

    if goal_lines:
        lines += ["", "*My Goals*"] + goal_lines
    else:
        lines += [
            "",
            f'*"{_random_quote()}"*',
            "",
            "Use /stats · /goals to check your progress",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dispatcher — sends to all group chats
# ---------------------------------------------------------------------------

async def send_activity_notification(
    bot: Bot,
    activity: Activity,
    user: User,
    chat_ids: list[int],
) -> None:
    """Format and broadcast a new activity to every group chat.

    Converts the ORM ``Activity`` instance into the dict format expected by
    :func:`format_activity_notification`, then sends the message to each chat.

    Args:
        bot:      Telegram Bot instance.
        activity: ORM Activity row (already committed to the database).
        user:     ORM User row for the athlete who recorded the activity.
        chat_ids: List of Telegram chat IDs to send the notification to.
    """
    athlete_name = (
        user.strava_athlete_name
        or user.telegram_first_name
        or f"Athlete {user.telegram_user_id}"
    )

    # Convert ORM Activity → Strava-shaped dict so format_activity_notification
    # can stay agnostic about the source (API response vs database).
    activity_dict: dict = {
        "name":                 activity.activity_name,
        "type":                 activity.activity_type,
        "start_date":           _orm_date_to_str(activity.activity_date),
        "distance":             activity.distance_meters,
        "moving_time":          activity.moving_time_seconds,
        "elapsed_time":         activity.elapsed_time_seconds,
        "calories":             activity.calories,
        "average_speed":        activity.average_speed,   # stored as m/s
        "max_speed":            activity.max_speed,       # stored as m/s
        "total_elevation_gain": activity.elevation_gain,
        "average_heartrate":    activity.average_heartrate,
        "max_heartrate":        activity.max_heartrate,
    }

    text = await format_activity_notification(activity_dict, athlete_name)

    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            logger.info(
                "Notification sent: activity_id=%s chat_id=%s",
                activity.strava_activity_id,
                chat_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to notify chat_id=%s for activity_id=%s: %s",
                chat_id,
                activity.strava_activity_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Goal milestone DM
# ---------------------------------------------------------------------------

async def send_goal_progress_notification(
    bot: Bot,
    user: User,
    goal,
    progress_pct: float,
) -> None:
    """DM the user when they hit a 25 / 50 / 75 / 100 % milestone on a goal.

    Args:
        bot:          Telegram Bot instance.
        user:         ORM User to DM.
        goal:         ORM Goal instance.
        progress_pct: Current completion percentage (0–100+).
    """
    # Snap to nearest 25 % milestone to avoid sending duplicates
    milestone = int(progress_pct // 25) * 25
    if milestone == 0:
        return

    milestone_emoji = {25: "🌱", 50: "⚡", 75: "🔥", 100: "🏆"}.get(milestone, "🎯")

    text = (
        f"{milestone_emoji} Goal milestone: {milestone}%!\n\n"
        f"You've reached *{progress_pct:.0f}%* of your "
        f"{goal.target_count}× {goal.category} {goal.activity_type} goal "
        f"({goal.start_date} → {goal.end_date}).\n\n"
        f"Keep going! 💪"
    )

    try:
        await bot.send_message(
            chat_id=user.telegram_user_id,
            text=text,
            parse_mode="Markdown",
        )
        logger.info(
            "Goal milestone notification sent: telegram_user_id=%s milestone=%s%%",
            user.telegram_user_id,
            milestone,
        )
    except Exception as exc:
        logger.error(
            "Failed to send goal milestone to telegram_user_id=%s: %s",
            user.telegram_user_id,
            exc,
        )


# ---------------------------------------------------------------------------
# File loaders
# ---------------------------------------------------------------------------

def _load_club_message() -> str:
    """Read data/club_message.txt, strip whitespace.  Returns '' on missing file."""
    try:
        return _CLUB_MESSAGE_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning("club_message.txt not found at %s", _CLUB_MESSAGE_PATH)
        return ""


def _random_quote() -> str:
    """Return a random non-empty line from data/quotes.txt."""
    try:
        lines = [
            ln.strip()
            for ln in _QUOTES_PATH.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        return random.choice(lines) if lines else "Keep moving forward."
    except FileNotFoundError:
        logger.warning("quotes.txt not found at %s", _QUOTES_PATH)
        return "Keep moving forward."


# ---------------------------------------------------------------------------
# Date helper (ORM → Strava-style string, used internally in send_*)
# ---------------------------------------------------------------------------

def _orm_date_to_str(dt: datetime | None) -> str | None:
    """Convert an ORM datetime (UTC-aware) to a Strava-style ISO string."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
