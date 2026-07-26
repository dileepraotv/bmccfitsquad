"""Shared formatting and conversion utilities.

All unit-conversion and formatting helpers live here so that
``notifications.py``, ``calculator.py``, and any future modules
share a single implementation.

Functions
---------
  seconds_to_hhmmss(seconds)          int → "HH:MM:SS"
  meters_to_km(meters)                float → float (km, 2 dp)
  ms_to_kmh(speed_ms)                 float → float (km/h, 2 dp)
  format_date(dt)                     datetime → "YYYY-MM-DDTHH:MM:SS.000Z"
  format_strava_date(date_str_or_dt)  Strava ISO string or datetime → same format
  safe_round(value, decimals)         round() that treats None as 0.0
"""
from __future__ import annotations

from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Sport taxonomy
# ---------------------------------------------------------------------------
# Single source of truth for which raw Strava activity_type / sport_type
# values belong to each logical BMCC sport bucket. Stats, goal progress, and
# notification goal-lines all import this so an activity counts the same way
# everywhere in the product — a Gravel Ride must never count for /stats but
# not for a Ride goal (or vice versa).
SPORT_ACTIVITY_TYPES: dict[str, list[str]] = {
    "Ride": [
        "Ride", "VirtualRide", "EBikeRide", "GravelRide",
        "MountainBikeRide", "EMountainBikeRide", "Handcycle",
        "Velomobile",
    ],
    "RideEndurance": [
        "Ride", "VirtualRide", "EBikeRide", "GravelRide",
        "MountainBikeRide", "EMountainBikeRide",
    ],
    "Run":    ["Run", "VirtualRun", "TrailRun"],
    "Walk":   ["Walk"],
    "Swim":   ["Swim", "OpenWaterSwim"],
    "Hiking": ["Hike"],
    "Yoga":   ["Yoga"],
    "RacketSports": ["Tennis", "TableTennis", "Badminton", "Pickleball", "Squash"],
    "StrengthTraining": [
        "WeightTraining", "Workout", "HighIntensityIntervalTraining", "Crossfit",
    ],
}

# Sports with no meaningful GPS distance (Strava reports distance = 0 for
# these) — stats/goals/recap/leaderboard track them by time practiced
# instead of kilometres. Everything else in SPORT_ACTIVITY_TYPES is
# distance-based.
DURATION_BASED_SPORTS: set[str] = {"Yoga", "RacketSports", "StrengthTraining"}

# Sports grouped under the "Other Activities" bucket in /stats — anything
# that isn't one of the five original core sports (Ride/RideEndurance/
# Run/Walk/Swim), surfaced via a secondary menu instead of cluttering the
# main sport-selection keyboard.
OTHER_ACTIVITY_SPORTS: list[str] = ["Yoga", "RacketSports", "Hiking", "StrengthTraining"]


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def seconds_to_hhmmss(seconds: int | float) -> str:
    """Convert a duration in seconds to a zero-padded ``HH:MM:SS`` string.

    Negative values are clamped to zero.

    Examples::

        >>> seconds_to_hhmmss(3661)
        '01:01:01'
        >>> seconds_to_hhmmss(28883)
        '08:01:23'
        >>> seconds_to_hhmmss(0)
        '00:00:00'
        >>> seconds_to_hhmmss(-10)   # clamped
        '00:00:00'
    """
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3_600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# ---------------------------------------------------------------------------
# Distance / speed
# ---------------------------------------------------------------------------

def meters_to_km(meters: float | int | None) -> float:
    """Convert metres to kilometres, rounded to 2 decimal places.

    Returns ``0.0`` for falsy input (``None``, ``0``, ``0.0``).

    Examples::

        >>> meters_to_km(10_000)
        10.0
        >>> meters_to_km(42_195)
        42.2
        >>> meters_to_km(None)
        0.0
    """
    if not meters:
        return 0.0
    return round(float(meters) / 1_000, 2)


def ms_to_kmh(speed_ms: float | int | None) -> float:
    """Convert metres-per-second (Strava's native speed unit) to km/h.

    Returns ``0.0`` for falsy input.

    Examples::

        >>> ms_to_kmh(10.0)
        36.0
        >>> ms_to_kmh(None)
        0.0
    """
    if not speed_ms:
        return 0.0
    return round(float(speed_ms) * 3.6, 2)


# ---------------------------------------------------------------------------
# Date / time formatting
# ---------------------------------------------------------------------------

def format_date(dt: datetime | None) -> str:
    """Format a ``datetime`` object to ``YYYY-MM-DDTHH:MM:SS.000Z``.

    The ``.000`` millisecond suffix is a literal string (not computed) to
    match the exact format used in activity notification messages.

    Returns ``"N/A"`` if ``dt`` is ``None``.

    Examples::

        >>> from datetime import datetime, timezone
        >>> format_date(datetime(2026, 5, 25, 10, 30, 0, tzinfo=timezone.utc))
        '2026-05-25T10:30:00.000Z'
        >>> format_date(None)
        'N/A'
    """
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def format_friendly_date(date_input: str | datetime | None) -> str:
    """Return a human-readable date string like ``Tue, 3 Jun 2026``.

    Accepts a Strava ISO string (``"2026-06-03T07:30:00Z"``) or a datetime.
    Returns ``"N/A"`` for falsy input.

    Examples::

        >>> format_friendly_date("2026-06-03T07:30:00Z")
        'Tue, 3 Jun 2026'
    """
    if not date_input:
        return "N/A"
    if isinstance(date_input, str):
        s = date_input
        if s.endswith("Z"):
            s = s[:-1]
        elif s.endswith("+00:00"):
            s = s[:-6]
        try:
            dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        except ValueError:
            return str(date_input)
    else:
        dt = date_input
    return dt.strftime("%a, %-d %b %Y")


def format_strava_date(date_input: str | datetime | None) -> str:
    """Normalise a Strava date string or datetime to ``YYYY-MM-DDTHH:MM:SS.000Z``.

    Strava returns ISO 8601 strings such as ``"2024-05-01T10:30:00Z"``.
    This function strips the trailing ``Z`` / ``+00:00``, parses, and
    reformats with the explicit ``.000`` millisecond literal.

    Examples::

        >>> format_strava_date("2024-05-01T10:30:00Z")
        '2024-05-01T10:30:00.000Z'
        >>> format_strava_date(None)
        'N/A'
    """
    if date_input is None:
        return "N/A"
    if isinstance(date_input, datetime):
        return format_date(date_input)
    s = str(date_input)
    # Remove timezone suffix before parsing — fromisoformat on Python < 3.11
    # does not accept the trailing "Z" or "+00:00".
    if s.endswith("Z"):
        s = s[:-1]
    elif s.endswith("+00:00"):
        s = s[:-6]
    try:
        dt = datetime.fromisoformat(s)
        return format_date(dt)
    except ValueError:
        return str(date_input)


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def safe_round(value: float | int | None, decimals: int = 2) -> float:
    """Round ``value`` to ``decimals`` places, treating ``None`` as ``0.0``.

    Useful when aggregating optional Strava fields that may be absent.

    Examples::

        >>> safe_round(3.14159, 2)
        3.14
        >>> safe_round(None)
        0.0
        >>> safe_round(42)
        42.0
    """
    if value is None:
        return 0.0
    return round(float(value), decimals)


# ---------------------------------------------------------------------------
# Telegram text formatting
# ---------------------------------------------------------------------------

def format_kv_lines(pairs: list[tuple[str, str]]) -> str:
    """Render label/value pairs as fixed-width monospace lines that line up
    in a straight column, for use inside a Telegram message.

    Each line is wrapped as its own inline `code` span rather than a single
    fenced ``` block — a fenced block renders as a separate, narrower boxed
    element with a "Copy" button in Telegram clients, whereas inline code
    keeps the monospace alignment while flowing as normal message text at
    full bubble width. Shared by activity notifications (metrics + goal
    progress lines) and /stats output so all three read consistently.
    """
    if not pairs:
        return ""
    width = max(len(label) for label, _ in pairs)
    return "\n".join(f"`{label.ljust(width)} : {value}`" for label, value in pairs)
