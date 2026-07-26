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
  {emoji} {greeting}, {first_name} — new {sport} activity logged!
  ─────────────────
  Athlete: …
  Activity: …
  Date: Sat, 18 Jul 2026

  ```
  Distance : … km        (Swim: Distance/Pace/Moving Time/Avg HR;
  Avg Speed: … km/h       duration sports: Duration/Calories/Avg HR)
  Elevation: … m
  ```

  ─────────────────
  🎯 Goal Progress
  …

  ─────────────────
  "{random quote}"

  *BMCC* - _Beyond Miles, Beyond Limits_
"""
from __future__ import annotations

import logging
import pathlib
import random
from datetime import datetime, timezone

from telegram import Bot

from app.models import Activity, User
from app.utils import (
    DURATION_BASED_SPORTS,
    SPORT_ACTIVITY_TYPES,
    format_friendly_date,
    format_kv_lines,
    meters_to_km,
    ms_to_kmh,
    seconds_to_hhmmss,
)

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
    "Hike":            "🥾",
    "Swim":            "🏊",
    "OpenWaterSwim":   "🏊",
    "WeightTraining":  "🏋️",
    "Workout":         "💪",
    "HighIntensityIntervalTraining": "🔥",
    "Yoga":            "🧘",
    "Rowing":          "🚣",
    "Kayaking":        "🚣",
    "Soccer":          "⚽",
    "Tennis":          "🎾",
    "TableTennis":     "🏓",
    "Badminton":       "🏸",
    "Pickleball":      "🏓",
    "Squash":          "🎾",
    "Golf":            "⛳",
    "Crossfit":        "💪",
    "RockClimbing":    "🧗",
    "Skiing":          "⛷️",
    "Snowboard":       "🏂",
    "Skateboard":      "🛹",
}
_DEFAULT_EMOJI = "🏅"

# Rotated so the opener doesn't feel copy-pasted across consecutive posts.
_GREETINGS: list[str] = ["Nice one", "Kudos to you", "Amazing", "Wow", "Great stuff"]


def _random_greeting() -> str:
    return random.choice(_GREETINGS)


_SWIM_TYPES = {"Swim", "OpenWaterSwim"}

# Raw Strava activity_type strings with no meaningful GPS distance (Yoga,
# Racket Sports, Strength Training) — these get a time/effort-focused
# metrics block instead of distance/speed/elevation.
_DURATION_TYPES: set[str] = {
    t for sport in DURATION_BASED_SPORTS for t in SPORT_ACTIVITY_TYPES[sport]
}


def _format_pace_per_100m(avg_speed_ms: float | int | None) -> str:
    """Convert average speed (m/s) to a swim pace string like ``"01:45"`` per 100m."""
    if not avg_speed_ms:
        return "N/A"
    pace_seconds = 100 / float(avg_speed_ms)
    minutes, secs = divmod(round(pace_seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


# Friendly one/two-word names for the greeting line ("new <word> activity
# logged!") — overrides for raw Strava types that would otherwise read
# awkwardly if just space-split from CamelCase (e.g. "high intensity
# interval training"). Anything not listed here falls back to a generic
# CamelCase → lowercase words split.
_SPORT_WORD_OVERRIDES: dict[str, str] = {
    "VirtualRide": "ride", "EBikeRide": "ride", "GravelRide": "ride",
    "MountainBikeRide": "ride", "EMountainBikeRide": "ride",
    "Handcycle": "ride", "Velomobile": "ride",
    "VirtualRun": "run", "TrailRun": "run",
    "OpenWaterSwim": "swim",
    "HighIntensityIntervalTraining": "HIIT",
}


def _camel_to_words(text: str) -> str:
    out = []
    for i, ch in enumerate(text):
        if ch.isupper() and i > 0:
            out.append(" ")
        out.append(ch)
    return "".join(out)


def _friendly_sport_word(activity_type: str) -> str:
    """"Ride" -> "ride", "TableTennis" -> "table tennis", etc. — used in the
    greeting line so the notification reads "new <sport> activity logged!"."""
    if activity_type in _SPORT_WORD_OVERRIDES:
        return _SPORT_WORD_OVERRIDES[activity_type]
    return _camel_to_words(activity_type).lower()


# format_kv_lines (aligned monospace label/value lines) now lives in
# app.utils so notifications, /stats, and goal-progress lines all share the
# exact same rendering — see the import above.


# ---------------------------------------------------------------------------
# Primary public formatter
# ---------------------------------------------------------------------------

async def format_activity_notification(
    activity: dict,
    athlete_name: str,
    goal_lines: list[tuple[str, str]] | None = None,
) -> str:
    """Format a raw Strava activity dict into a BMCC Telegram notification."""
    activity_type: str = activity.get("sport_type") or activity.get("type") or "Unknown"
    emoji         = _EMOJI.get(activity_type, _DEFAULT_EMOJI)
    activity_id   = activity.get("id")
    activity_name = activity.get("name") or "Unnamed Activity"
    first_name    = athlete_name.split()[0] if athlete_name else athlete_name

    if activity_id:
        activity_link = f"[{activity_name}](https://www.strava.com/activities/{activity_id})"
    else:
        activity_link = activity_name

    # ------------------------------------------------------------------
    # Header — Athlete/Date (label + colon + value) are fully monospace
    # `code` lines like the metrics block below. Activity only has its
    # label wrapped in `code` (same width as "Athlete"/"Date " so the
    # colons line up) — the colon and link stay outside the code span and
    # unstyled, since wrapping a markdown link in `code` would print the
    # raw "[text](url)" instead of rendering it as tappable.
    # ------------------------------------------------------------------
    greeting   = _random_greeting()
    sport_word = _friendly_sport_word(activity_type)
    lines: list[str] = [
        f"{emoji} *{greeting}, {first_name} — new {sport_word} activity logged!*",
        _SEPARATOR,
        f"`Athlete : {athlete_name}`",
        f"`Activity` : {activity_link}",
        f"`Date    : {format_friendly_date(activity.get('start_date'))}`",
    ]

    # ------------------------------------------------------------------
    # Metrics — kept intentionally short; the full breakdown is one tap
    # away on Strava via the activity link above. Swims get pace-focused
    # metrics instead of speed/elevation, which don't mean much in water.
    # ------------------------------------------------------------------
    if activity_type in _SWIM_TYPES:
        distance_m  = int(activity.get("distance") or 0)
        pace        = _format_pace_per_100m(activity.get("average_speed"))
        moving_secs = int(activity.get("moving_time") or 0)
        avg_hr      = activity.get("average_heartrate")

        pairs = [
            ("Distance", f"{distance_m} m"),
            ("Pace", f"{pace} /100m"),
            ("Moving Time", seconds_to_hhmmss(moving_secs)),
        ]
        if avg_hr is not None:
            pairs.append(("Avg HR", f"{int(avg_hr)} bpm"))
        lines += ["", format_kv_lines(pairs)]
    elif activity_type in _DURATION_TYPES:
        moving_secs = int(activity.get("moving_time") or 0)
        calories    = activity.get("calories")
        avg_hr      = activity.get("average_heartrate")

        pairs = [("Duration", seconds_to_hhmmss(moving_secs))]
        if calories:
            pairs.append(("Calories", f"{int(calories)} kcal"))
        if avg_hr is not None:
            pairs.append(("Avg HR", f"{int(avg_hr)} bpm"))
        lines += ["", format_kv_lines(pairs)]
    else:
        distance_km   = meters_to_km(activity.get("distance"))
        avg_speed_kmh = ms_to_kmh(activity.get("average_speed"))
        elevation_m   = activity.get("total_elevation_gain") or 0

        pairs = [
            ("Distance", f"{distance_km:.2f} km"),
            ("Avg Speed", f"{avg_speed_kmh:.2f} km/h"),
            ("Elevation Gain", f"{int(elevation_m)} m"),
        ]
        lines += ["", format_kv_lines(pairs)]

    # ------------------------------------------------------------------
    # Goal progress section — each goal renders as "<emoji sport category>
    # : <achieved>/<target>", column-aligned the same way as the metrics
    # block above.
    # ------------------------------------------------------------------
    lines += [_SEPARATOR, "🎯 *Goal Progress*", ""]
    if goal_lines:
        lines.append(format_kv_lines(goal_lines))
    else:
        lines.append("No active goals. Use /goals to set one.")

    # ------------------------------------------------------------------
    # Quote + tagline
    # ------------------------------------------------------------------
    lines += [
        _SEPARATOR,
        f'*"{_random_quote()}"*',
        "",
        "*BMCC* - _Beyond Miles, Beyond Limits_",
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
            await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
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
