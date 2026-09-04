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
  (Roast Mode, on by default: {emoji} {first_name}, {roast_or_kudos_line}
   new {sport} activity logged. — see _roast_or_kudos_line())
  ─────────────────
  Athlete: …
  Activity: …
  Date: Sat, 18 Jul 2026

  ```
  Distance : … km        (Run: Distance/Avg Pace/Elevation;
  Avg Speed: … km/h       Swim: Distance/Pace/Moving Time/Avg HR;
  Elevation: … m          duration sports: Duration/Calories/Avg HR)
  ```

  ─────────────────
  🎯 Goal Progress
  … (up to 10 active goals)
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
    OTHER_ACTIVITY_SPORTS,
    SPORT_ACTIVITY_TYPES,
    format_friendly_date,
    format_kv_lines,
    meters_to_km,
    ms_to_kmh,
    seconds_to_hhmmss,
)
from app.utils import SEPARATOR as _SEPARATOR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File paths (relative to the project root where uvicorn is launched)
# ---------------------------------------------------------------------------
_DATA_DIR = pathlib.Path("data")
_CLUB_MESSAGE_PATH = _DATA_DIR / "club_message.txt"
_QUOTES_PATH = _DATA_DIR / "quotes.txt"

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

_MAX_GOAL_LINES = 10


def _random_greeting() -> str:
    return random.choice(_GREETINGS)


# ---------------------------------------------------------------------------
# Roast Mode — on by default (per-user, via /roastmode), replaces the plain
# "Nice one / Kudos to you / ..." greeting with a contextual line: a mild
# roast for a short/light effort, or a kudos line for a solid one. Only
# defined for the four core distance sports below; anything else (Yoga,
# Racket Sports, Strength Training, Hiking, ...) keeps the plain greeting
# regardless of the user's Roast Mode setting, since no threshold is defined.
# ---------------------------------------------------------------------------

# Below this distance → roast; at/above → kudos. Keyed by SPORT_ACTIVITY_TYPES
# bucket name (not raw Strava type) — see _roast_bucket_for_type() below.
_ROAST_THRESHOLDS_M: dict[str, float] = {
    "Ride": 25_000,
    "Run": 2_000,
    "Swim": 500,
    "Walk": 3_000,
}

# Raw Strava activity_type -> bucket name, built once from the shared sport
# taxonomy so this stays in sync with SPORT_ACTIVITY_TYPES automatically.
_ROAST_BUCKET_BY_TYPE: dict[str, str] = {
    raw_type: bucket
    for bucket in _ROAST_THRESHOLDS_M
    for raw_type in SPORT_ACTIVITY_TYPES.get(bucket, [])
}

# Every roast line (general, per-sport, elevation, and the time-based
# "Other Activities" ones below) ends in 😝 by convention — it's the visual
# tell that a line is a roast rather than a kudos, at a glance, without
# having to read the text.
_ROAST_GENERAL: list[str] = [
    "Only {value} {unit}? My grandma's dog walk was longer 😝",
    "{value} {unit}, huh? Bold of you to call that cardio. 😝",
    "That was less a workout, more a warm-up for the warm-up. 😝",
    "Congrats, you've officially out-walked your couch. 😝",
    "I've seen coffee breaks with more mileage. 😝",
    "That's cardio in theory only. 😝",
    "The couch called, it wants its title back. 😝",
    "Effort level: barely visible. 😝",
]

_ROAST_SPORT: dict[str, list[str]] = {
    "Run": [
        "That 'run' had more walking breaks than a mall food court. 😝",
        "Marathon training? More like light jogging with commitment issues. 😝",
        "That pace filed a missing person's report. 😝",
        "The road barely noticed you were there. 😝",
    ],
    "Ride": [
        "Your average speed suggests the bike was doing you a favor. 😝",
        "That's not a ride, that's a scenic bike-shaped stroll. 😝",
        "Even the chain was bored. 😝",
        "That's not cycling, that's gentle drifting. 😝",
    ],
    "Swim": [
        "Did you swim it or float there thinking about lunch? 😝",
        "That lap count says 'effort,' your pace says 'floatation device.' 😝",
        "The pool barely got wet. 😝",
        "That's less a swim, more a polite dip. 😝",
    ],
    "Walk": [
        "A 'walk' that short barely counts as leaving the building. 😝",
        "Technically movement. Generously, exercise. 😝",
        "That's a walk to the fridge, not a workout. 😝",
        "Your step count is filing a complaint. 😝",
    ],
}

# Kudos lines are layered so a big enough effort feels noticeably bigger,
# not just a random pick from the same bucket every time:
#   _KUDOS_GENERAL      — the everyday "solid effort" pool
#   _KUDOS_LEADERBOARD  — cheeky, group-banter-flavored lines (this is a
#                         group leaderboard bot, so these fit right into the
#                         regular mix rather than needing their own trigger)
#   _KUDOS_SPORT        — sport-specific flavor
#   _KUDOS_SMASH        — extra "absolutely crushed it"-flavored lines
# All four are mixed into one flat pool for every qualifying activity (see
# _roast_or_kudos_line) rather than gated behind how far past the threshold
# it landed — simpler, and keeps the rotation varied regardless of margin.
_KUDOS_GENERAL: list[str] = [
    "Now THAT is how you move. 🔥",
    "You understood the assignment. 💪",
    "That distance deserves a double-tap. 👏",
    "Okay, show-off. 😎",
    "The legs have spoken.",
    "That's a serious chunk of mileage.",
    "Someone woke up and chose distance.",
    "That's a proper session. 🔥",
    "Respect the grind. 🫡",
    "Big distance. Bigger effort.",
    "You came. You moved. You conquered.",
    "Another one for the books. 📖",
    "That's some serious engine work.",
    "The leaderboard just got nervous.",
    "Casually putting in the work. 😏",
    "That's how you make kilometers count.",
    "No shortcuts. Just kilometers.",
    "That's a whole lot of moving.",
    "Distance doesn't lie. Great work.",
    "You didn't come to participate. 🔥",
    "That's commitment measured in kilometers.",
    "The consistency is getting dangerous.",
    "That's not luck. That's legs.",
    "Another day, another solid effort.",
    "Your future self just said thank you.",
    "That's one way to raise the bar.",
    "Quietly stacking those kilometers. 👏",
    "The mileage is speaking for itself.",
    "That's a session worth bragging about.",
    "And just like that, the bar moves higher.",
]

# Leaderboard-flavored cheeky lines — mixed into the same pool as
# _KUDOS_GENERAL (see _roast_or_kudos_line) rather than gated behind an
# actual leaderboard-rank check, since this bot's notifications already post
# into the group chat these jokes are aimed at.
_KUDOS_LEADERBOARD: list[str] = [
    "Leaderboard: nervous sweating 😅",
    "Someone's coming for the top spot.",
    "The competition just got interesting.",
    "That's going straight onto the leaderboard.",
    "And there goes another person ruining everyone's chances. 😂",
    "Please leave some kilometers for the rest of us.",
    "At this rate, you're going to need your own leaderboard.",
    "Casual flex. Very casual. 😎",
    "Who gave you permission to go this far?",
    "The leaderboard committee has taken notice.",
    "Okay, we get it. You're fit. 😂",
    "That's getting uncomfortably close to the top.",
    "Someone woke up competitive.",
    "That's not helping the rest of us catch up.",
    "Respectfully… calm down. 😂",
    "The leaderboard has entered its villain era.",
    "Another activity. Another problem for the competition.",
    "Your competitors would like a word.",
    "This is becoming a habit. And we're here for it.",
    "At this point, the leaderboard owes you commission.",
]

_KUDOS_SPORT: dict[str, list[str]] = {
    "Run": [
        "Those legs clearly had somewhere to be.",
        "Run now, brag later. 😎",
        "That wasn't a run. That was a mission.",
        "Miles conquered. Excuses defeated.",
        "You chased the distance and caught it.",
        "That's some serious roadwork. 🏃",
        "The pavement didn't stand a chance.",
        "Another run, another victory.",
        "You really don't believe in easy days, do you?",
        "That's a proper leg-day without the gym.",
        "Running like the finish line owes you money.",
        "That's how you put the 'run' in run-rate. 😄",
        "The road got a little shorter today.",
        "That's a lot of forward motion.",
        "Your shoes are filing for overtime.",
        "Cardio department: absolutely nailed it.",
        "That's some serious kilometre therapy.",
        "You didn't run out of excuses — you ran out of road.",
        "Pace, distance, attitude — all showing up.",
        "The road was yours today.",
    ],
    "Ride": [
        "Two wheels, zero excuses. 🚴",
        "That's a serious amount of saddle time.",
        "The bike clearly had places to be.",
        "Pedals were put to very good use.",
        "That's not a ride. That's an expedition.",
        "Another day, another road conquered.",
        "Legs + pedals = results.",
        "That's some serious road therapy.",
        "The chain is probably asking for a day off.",
        "You didn't ride the distance. You owned it.",
        "That's a proper sufferfest. Respect.",
        "Someone's been feeding the legs watts. ⚡",
        "The bike and legs were clearly having a good day.",
        "That's a lot of road disappearing behind you.",
        "The saddle deserves a medal too.",
        "That's how you turn Sunday into a cycling story.",
        "No traffic jam can stop these legs.",
        "Rubber down, excuses up.",
        "That's a long conversation between legs and pedals.",
        "The bike computer is going to need a lie-down.",
    ],
    "Swim": [
        "Making kilometers look effortless. 🏊",
        "That's a serious amount of water covered.",
        "The pool got a proper workout too.",
        "Lane domination. 💦",
        "Just casually converting water into distance.",
        "That's some serious aquatic mileage.",
        "The pool wasn't ready for this.",
        "Smooth strokes, serious distance.",
        "That's a lot of laps. Respect.",
        "You basically paid rent in that pool today.",
        "The water level may have dropped slightly. 😄",
        "Another session, another lane conquered.",
        "That's how you make a splash without making a splash.",
        "Goggles on. Excuses off.",
        "The chlorine knows your name now.",
        "That's some serious time underwater.",
        "Swimming laps like they're going out of fashion.",
        "The pool has officially seen enough of you today.",
        "Stroke after stroke, kilometer after kilometer.",
        "That's a whole lot of blue mileage.",
    ],
    "Walk": [
        "One step at a time. A LOT of steps. 👟",
        "That's some serious footwork.",
        "Walking? More like quietly crushing it.",
        "The steps just kept coming.",
        "That's how you turn a walk into a workout.",
        "No rush. Just results.",
        "Slow and steady? More like steady and unstoppable.",
        "That's a serious amount of pavement covered.",
        "Every step counted today.",
        "The feet were clearly on a mission.",
        "That's what we call productive wandering.",
        "You walked your way straight onto the leaderboard.",
        "Proof that you don't need to run to put in the work.",
        "That's a lot of steps and zero shortcuts.",
        "The neighborhood knows you by now.",
        "Just walking... and casually stacking kilometers.",
        "Those shoes have earned their dinner.",
        "A leisurely stroll with highly non-leisurely mileage.",
        "The legs don't care what the activity is. They showed up.",
        "Walking strong. Walking long. 👏",
    ],
}

# "Absolutely smashed it" flavored lines — mixed into the same flat kudos
# pool as everything else (see _roast_or_kudos_line), not gated behind any
# particular margin over the threshold.
_KUDOS_SMASH: list[str] = [
    "Threshold? Absolutely obliterated. 💥",
    "You didn't clear the bar. You launched it.",
    "Threshold destroyed. 🔥",
    "That's not meeting the target. That's making the target look small.",
    "Target achieved. Then casually kept going.",
    "You saw the threshold and said, 'Is that all?'",
    "Minimum distance? Maximum disrespect. 😂",
    "The target was merely a suggestion.",
    "You didn't cross the line. You left it behind.",
    "That threshold didn't stand a chance.",
    "Target: ✅ Overachievement: ✅",
    "That's how you turn a minimum into a masterpiece.",
    "Mission accomplished. Then some.",
    "The bar has officially been raised.",
    "That's an emphatic YES. 💪",
]

# Suspiciously flat route roast — overrides the distance-earned kudos when a
# ride/run/walk clears its distance bar but barely climbed anything. Matches
# e.g. a 100 km ride with < 500 m elevation gain (5 m of gain per km).
_ELEVATION_ROAST_SPORTS: set[str] = {"Ride", "Run", "Walk"}
_ELEVATION_ROAST_M_PER_KM = 5.0

_ELEVATION_ROAST: list[str] = [
    "{km} km and barely a bump in sight — that's a runway, not a route. 😝",
    "Flat as a pancake out there. Where were the actual hills? 😝",
    "Zero hills were harmed in the making of this activity. 😝",
    "That elevation graph is basically a flat line with main character energy. 😝",
]

# "Other Activities" (Yoga, Racket Sports, Strength Training, Hiking) have no
# distance/pace threshold defined, so they're judged on time instead: under
# 30 minutes gets roasted, 30 minutes or more earns a kudos line.
_OTHER_SPORT_ROAST_MAX_SECONDS = 30 * 60
_OTHER_SPORT_RAW_TYPES: set[str] = {
    t for sport in OTHER_ACTIVITY_SPORTS for t in SPORT_ACTIVITY_TYPES.get(sport, [])
}

_OTHER_SPORT_ROAST: list[str] = [
    "{minutes} minutes? That's a warm-up, not a session. 😝",
    "Blink and you'd have missed the whole workout. 😝",
    "That barely counts as showing up. 😝",
    "Short and not-so-sweet — give it a real effort next time. 😝",
]


def _roast_or_kudos_line(
    activity_type: str,
    distance_m: float | int | None,
    elevation_gain_m: float | int | None = None,
    moving_time_s: float | int | None = None,
) -> str | None:
    """Pick a contextual roast/kudos line for this activity, or None if the
    caller should fall back to the plain rotating greeting.

    Ride/Run/Swim/Walk are judged on distance vs. a per-sport threshold.
    Ride/Run/Walk additionally get roasted for a suspiciously flat route even
    if the distance itself clears the bar. Everything else ("Other
    Activities") is judged on time: under 30 minutes is roasted, 30+ earns
    a kudos line.
    """
    bucket = _ROAST_BUCKET_BY_TYPE.get(activity_type)

    if bucket is None:
        if activity_type not in _OTHER_SPORT_RAW_TYPES:
            return None
        moving_time_s = moving_time_s or 0
        if moving_time_s < _OTHER_SPORT_ROAST_MAX_SECONDS:
            minutes = max(1, round(moving_time_s / 60))
            return random.choice(_OTHER_SPORT_ROAST).format(minutes=minutes)
        return random.choice(_KUDOS_GENERAL + _KUDOS_LEADERBOARD + _KUDOS_SMASH)

    threshold_m = _ROAST_THRESHOLDS_M[bucket]
    distance_m = distance_m or 0

    if distance_m < threshold_m:
        pool = _ROAST_GENERAL + _ROAST_SPORT.get(bucket, [])
        line = random.choice(pool)
        if bucket == "Swim":
            value, unit = int(distance_m), "m"
        else:
            value, unit = round(distance_m / 1_000), "km"
        return line.format(value=value, unit=unit)

    if bucket in _ELEVATION_ROAST_SPORTS and distance_m > 0:
        elevation_gain_m = elevation_gain_m or 0
        distance_km = distance_m / 1_000
        if (elevation_gain_m / distance_km) < _ELEVATION_ROAST_M_PER_KM:
            return random.choice(_ELEVATION_ROAST).format(km=round(distance_km))

    # One flat mix-and-match pool for any qualifying activity, regardless of
    # by how much it cleared the threshold — general, leaderboard-flavored,
    # "smashed it", and sport-specific lines all in the same rotation.
    pool = _KUDOS_GENERAL + _KUDOS_LEADERBOARD + _KUDOS_SMASH + _KUDOS_SPORT.get(bucket, [])
    return random.choice(pool)


_SWIM_TYPES = {"Swim", "OpenWaterSwim"}
_RUN_TYPES = {"Run", "VirtualRun", "TrailRun"}

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


def _format_pace_per_km(avg_speed_ms: float | int | None) -> str:
    """Convert average speed (m/s) to a run pace string like ``"5:42"`` per km —
    runners think in pace, not speed, so this replaces Avg Speed for Run
    notifications the same way _format_pace_per_100m does for swims."""
    if not avg_speed_ms:
        return "N/A"
    pace_seconds = 1000 / float(avg_speed_ms)
    minutes, secs = divmod(round(pace_seconds), 60)
    return f"{minutes}:{secs:02d}"


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
    roast_mode_enabled: bool = True,
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
    sport_word = _friendly_sport_word(activity_type)
    roast_line = (
        _roast_or_kudos_line(
            activity_type,
            activity.get("distance"),
            elevation_gain_m=activity.get("total_elevation_gain"),
            moving_time_s=activity.get("moving_time"),
        )
        if roast_mode_enabled
        else None
    )
    if roast_line:
        header = f"{emoji} *{first_name}, {roast_line} New {sport_word} activity logged.*"
    else:
        header = f"{emoji} *{_random_greeting()}, {first_name} — new {sport_word} activity logged!*"
    lines: list[str] = [
        header,
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
    elif activity_type in _RUN_TYPES:
        distance_km = meters_to_km(activity.get("distance"))
        pace        = _format_pace_per_km(activity.get("average_speed"))
        elevation_m = activity.get("total_elevation_gain") or 0

        pairs = [
            ("Distance", f"{distance_km:.2f} km"),
            ("Avg Pace", f"{pace} /km"),
            ("Elevation Gain", f"{int(elevation_m)} m"),
        ]
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
    # block above. Capped at 10 so a member with many active goals doesn't
    # turn every activity notification into a wall of text.
    # ------------------------------------------------------------------
    lines += [_SEPARATOR, "🎯 *Goal Progress*", ""]
    if goal_lines:
        lines.append(format_kv_lines(goal_lines[:_MAX_GOAL_LINES]))
    else:
        lines.append("No active goals. Use /goals to set one.")

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
# Monthly/yearly recap delivery
# ---------------------------------------------------------------------------

async def send_recap_message(bot: Bot, chat_id: int, text: str, reply_markup=None) -> None:
    """Send a monthly/yearly recap: the monospace text block, with the
    goal-prompt keyboard (if any) attached directly to it.

    Kept as a thin wrapper (rather than calling bot.send_message directly at
    each call site) so /recap, /yearrecap, the scheduled cron senders, and
    the /ops/send-recap testing endpoint all stay in sync if the delivery
    format changes again later.
    """
    await bot.send_message(
        chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=reply_markup,
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
