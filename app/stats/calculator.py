"""Stats calculation logic for all BMCC sport types.

Entry points
------------
  calculate_stats(db_session, user_id, sport_type, time_frame) -> dict
  format_stats_message(stats, sport_type, time_frame, athlete_name) -> str

Sport types accepted
--------------------
  "Ride"          – All rides (Ride + VirtualRide, where VirtualRide = indoor)
  "RideEndurance" – Only rides whose distance is ≥ 200 km
  "Run"           – Run + VirtualRun (VirtualRun = indoor/treadmill)
  "Walk"          – Walk + Hike
  "Swim"          – Swim + OpenWaterSwim

Time frames accepted
--------------------
  "all_time"        All stored activities for the user
  "year_to_date"    1 Jan of current year → now (UTC)
  "previous_year"   1 Jan → 31 Dec of the previous calendar year
  "current_month"   1st of current month → now (UTC)
  "previous_month"  1st → last day of the previous calendar month

Units
-----
  Strava stores distance in *metres* (Activity.distance_meters).
  All output distances are in *kilometres* (÷ 1 000).
  Strava stores elevation in *metres* (Activity.elevation_gain).
  Swim bracket thresholds are compared in *metres* to avoid float drift.
  Moving time is in seconds (Activity.moving_time_seconds) → formatted HH:MM:SS.
"""
from __future__ import annotations

import pathlib
import random
import uuid
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity
from app.utils import meters_to_km, safe_round, seconds_to_hhmmss

_QUOTES_PATH = pathlib.Path("data/quotes.txt")


def _random_quote() -> str:
    try:
        lines = [l.strip() for l in _QUOTES_PATH.read_text().splitlines() if l.strip()]
        return random.choice(lines) if lines else "Keep moving forward."
    except FileNotFoundError:
        return "Every kilometre counts."

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SportType = Literal["Ride", "RideEndurance", "Run", "Walk", "Swim"]
TimeFrame = Literal[
    "all_time", "year_to_date", "previous_year", "current_month", "previous_month"
]

# Strava activity_type strings that map to each logical sport
_SPORT_ACTIVITY_TYPES: dict[str, list[str]] = {
    "Ride":          ["Ride", "VirtualRide"],
    "RideEndurance": ["Ride", "VirtualRide"],
    "Run":           ["Run", "VirtualRun"],
    "Walk":          ["Walk", "Hike"],
    "Swim":          ["Swim", "OpenWaterSwim"],
}

_TIME_FRAME_LABELS: dict[str, str] = {
    "all_time":       "All Time",
    "year_to_date":   "Year to Date",
    "previous_year":  "Previous Year",
    "current_month":  "Current Month",
    "previous_month": "Previous Month",
}

_SPORT_LABELS: dict[str, str] = {
    "Ride":          "Ride",
    "RideEndurance": "Ride Endurance",
    "Run":           "Run",
    "Walk":          "Walk",
    "Swim":          "Swim",
}

# Run milestone distances in km
_HALF_MARATHON_KM = 21.0975
_FULL_MARATHON_KM = 42.195


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def calculate_stats(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    sport_type: str,
    time_frame: str,
) -> dict:
    """Calculate sport-specific stats for a user over the given time frame.

    Args:
        db_session:  Active async SQLAlchemy session.
        user_id:     UUID primary key from the ``users`` table (``User.id``).
        sport_type:  One of "Ride", "RideEndurance", "Run", "Walk", "Swim".
        time_frame:  One of the five time-frame strings (see module docstring).

    Returns:
        Dict of computed stats; keys vary by sport_type (see module docstring).

    Raises:
        ValueError: For unknown sport_type or time_frame values.
    """
    if sport_type not in _SPORT_ACTIVITY_TYPES:
        raise ValueError(f"Unknown sport_type {sport_type!r}. Choose from: {list(_SPORT_ACTIVITY_TYPES)}")
    if time_frame not in _TIME_FRAME_LABELS:
        raise ValueError(f"Unknown time_frame {time_frame!r}. Choose from: {list(_TIME_FRAME_LABELS)}")

    activities = await _fetch_activities(db_session, user_id, sport_type, time_frame)

    calculators = {
        "Ride":          _compute_ride_stats,
        "RideEndurance": _compute_ride_endurance_stats,
        "Run":           _compute_run_stats,
        "Walk":          _compute_walk_stats,
        "Swim":          _compute_swim_stats,
    }
    return calculators[sport_type](activities)


def format_stats_message(
    stats: dict,
    sport_type: str,
    time_frame: str,
    athlete_name: str,
) -> str:
    """Format a stats dict into a Telegram-ready text message.

    The output matches the BMCC style exactly::

        Ride - Current Month Stats: (Dileep Rao)

        - Rides: 2
        - Distance: 152.67 km
        - Moving Time: 08:01:23 hours
        - Elevation Gain: 1.13 km
        - Biggest Ride: 100.06 km
        - 50's: 1
        - 100's: 1

    Args:
        stats:        Dict returned by :func:`calculate_stats`.
        sport_type:   Same value passed to calculate_stats.
        time_frame:   Same value passed to calculate_stats.
        athlete_name: Display name for the header (e.g. "Dileep Rao").

    Returns:
        Plain-text formatted string ready to send via Telegram.
    """
    sport_label = _SPORT_LABELS.get(sport_type, sport_type)
    time_label = _TIME_FRAME_LABELS.get(time_frame, time_frame)
    header = f"{sport_label} - {time_label} Stats: ({athlete_name})"

    formatters = {
        "Ride":          _format_ride,
        "RideEndurance": _format_ride_endurance,
        "Run":           _format_run,
        "Walk":          _format_walk,
        "Swim":          _format_swim,
    }
    body_lines = formatters.get(sport_type, lambda _: [])(stats)
    body = "\n".join(f"- {line}" for line in body_lines)
    quote = _random_quote()
    return f"{header}\n\n{body}\n\n_{quote}_"


# ---------------------------------------------------------------------------
# Database fetch
# ---------------------------------------------------------------------------

async def _fetch_activities(
    db: AsyncSession,
    user_id: uuid.UUID,
    sport_type: str,
    time_frame: str,
) -> list[Activity]:
    """Load all matching Activity rows for stats calculation."""
    start, end = _time_frame_bounds(time_frame)
    activity_types = _SPORT_ACTIVITY_TYPES[sport_type]

    filters = [
        Activity.user_id == user_id,
        Activity.activity_type.in_(activity_types),
    ]
    if start is not None:
        filters.append(Activity.activity_date >= start)
    if end is not None:
        filters.append(Activity.activity_date < end)

    # RideEndurance only counts rides that are at least 200 km
    if sport_type == "RideEndurance":
        filters.append(Activity.distance_meters >= 200_000.0)

    result = await db.execute(
        select(Activity)
        .where(and_(*filters))
        .order_by(Activity.activity_date)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Per-sport calculators
# ---------------------------------------------------------------------------

def _compute_ride_stats(activities: list[Activity]) -> dict:
    """All rides (Ride + VirtualRide). VirtualRide counted as indoor.

    Bracket counts:
      50's  — rides 50 km to < 100 km
      100's — rides >= 100 km (no upper cap)
    """
    distances_km = [a.distance_meters / 1_000 for a in activities]
    return {
        "rides":             len(activities),
        "distance_km":       _round2(sum(distances_km)),
        "moving_time":       _fmt_duration(sum(a.moving_time_seconds for a in activities)),
        "elevation_gain_km": _round2(sum(a.elevation_gain for a in activities) / 1_000),
        "biggest_ride_km":   _round2(max(distances_km, default=0.0)),
        "fifties":           sum(1 for d in distances_km if 50.0 <= d < 100.0),
        "hundreds":          sum(1 for d in distances_km if d >= 100.0),
    }


def _compute_ride_endurance_stats(activities: list[Activity]) -> dict:
    """Only rides ≥ 200 km (already filtered in the DB query)."""
    distances_km = [a.distance_meters / 1_000 for a in activities]
    return {
        "rides":             len(activities),
        "distance_km":       _round2(sum(distances_km)),
        "moving_time":       _fmt_duration(sum(a.moving_time_seconds for a in activities)),
        "elevation_gain_km": _round2(sum(a.elevation_gain for a in activities) / 1_000),
        "biggest_ride_km":   _round2(max(distances_km, default=0.0)),
        "two_hundreds":      sum(1 for d in distances_km if 200.0 <= d < 300.0),
        "three_hundreds":    sum(1 for d in distances_km if 300.0 <= d < 400.0),
        "four_hundreds":     sum(1 for d in distances_km if 400.0 <= d < 600.0),
        "six_hundreds":      sum(1 for d in distances_km if 600.0 <= d < 1_000.0),
        "thousands":         sum(1 for d in distances_km if 1_000.0 <= d < 1_200.0),
        "twelve_hundreds":   sum(1 for d in distances_km if d >= 1_200.0),
    }


def _compute_run_stats(activities: list[Activity]) -> dict:
    """Runs and VirtualRuns. VirtualRun (is_indoor=True) tracked separately."""
    all_distances_km  = [a.distance_meters / 1_000 for a in activities]
    indoor            = [a for a in activities if a.is_indoor]
    indoor_dist_km    = [a.distance_meters / 1_000 for a in indoor]

    return {
        "runs":                len(activities),
        "indoor_runs":         len(indoor),
        "distance_km":         _round2(sum(all_distances_km)),
        "indoor_distance_km":  _round2(sum(indoor_dist_km)),
        "moving_time":         _fmt_duration(sum(a.moving_time_seconds for a in activities)),
        "indoor_time":         _fmt_duration(sum(a.moving_time_seconds for a in indoor)),
        "elevation_gain_km":   _round2(sum(a.elevation_gain for a in activities) / 1_000),
        "biggest_run_km":      _round2(max(all_distances_km, default=0.0)),
        "fives":               sum(1 for d in all_distances_km if 5.0 <= d < 10.0),
        "tens":                sum(1 for d in all_distances_km if 10.0 <= d < _HALF_MARATHON_KM),
        "half_marathons":      sum(1 for d in all_distances_km if _HALF_MARATHON_KM <= d < _FULL_MARATHON_KM),
        "full_marathons":      sum(1 for d in all_distances_km if _FULL_MARATHON_KM <= d < 50.0),
        "ultras":              sum(1 for d in all_distances_km if d >= 50.0),
    }


def _compute_swim_stats(activities: list[Activity]) -> dict:
    """Swims. Bracket thresholds compared in metres to avoid float rounding."""
    # Keep metres for bracket counting; divide by 1 000 for display
    distances_m  = [a.distance_meters for a in activities]
    distances_km = [m / 1_000 for m in distances_m]

    return {
        "swims":               len(activities),
        "distance_km":         _round2(sum(distances_km)),
        "moving_time":         _fmt_duration(sum(a.moving_time_seconds for a in activities)),
        "biggest_swim_km":     _round2(max(distances_km, default=0.0)),
        "five_hundreds":       sum(1 for m in distances_m if 500.0 <= m < 1_000.0),
        "thousands":           sum(1 for m in distances_m if 1_000.0 <= m < 1_500.0),
        "fifteen_hundreds":    sum(1 for m in distances_m if 1_500.0 <= m < 2_000.0),
        "two_thousands":       sum(1 for m in distances_m if 2_000.0 <= m < 3_800.0),
        "thirty_eight_hundreds": sum(1 for m in distances_m if m >= 3_800.0),
    }


def _compute_walk_stats(activities: list[Activity]) -> dict:
    """Walks and Hikes."""
    distances_km = [a.distance_meters / 1_000 for a in activities]
    return {
        "walks":             len(activities),
        "distance_km":       _round2(sum(distances_km)),
        "moving_time":       _fmt_duration(sum(a.moving_time_seconds for a in activities)),
        "elevation_gain_km": _round2(sum(a.elevation_gain for a in activities) / 1_000),
        "biggest_walk_km":   _round2(max(distances_km, default=0.0)),
        "twos":              sum(1 for d in distances_km if 2.0 <= d < 5.0),
        "fives":             sum(1 for d in distances_km if 5.0 <= d < 10.0),
        "tens":              sum(1 for d in distances_km if d >= 10.0),
    }


# ---------------------------------------------------------------------------
# Per-sport formatters
# ---------------------------------------------------------------------------

def _format_ride(s: dict) -> list[str]:
    return [
        f"Rides: {s['rides']}",
        f"Distance: {s['distance_km']:.2f} km",
        f"Moving Time: {s['moving_time']} hours",
        f"Elevation Gain: {s['elevation_gain_km']:.2f} km",
        f"Biggest Ride: {s['biggest_ride_km']:.2f} km",
        f"50's: {s['fifties']}",
        f"100's: {s['hundreds']}",
    ]


def _format_ride_endurance(s: dict) -> list[str]:
    return [
        f"Rides: {s['rides']}",
        f"Distance: {s['distance_km']:.2f} km",
        f"Moving Time: {s['moving_time']} hours",
        f"Elevation Gain: {s['elevation_gain_km']:.2f} km",
        f"Biggest Ride: {s['biggest_ride_km']:.2f} km",
        f"200's: {s['two_hundreds']}",
        f"300's: {s['three_hundreds']}",
        f"400's: {s['four_hundreds']}",
        f"600's: {s['six_hundreds']}",
        f"1000's: {s['thousands']}",
        f"1200's: {s['twelve_hundreds']}",
    ]


def _format_run(s: dict) -> list[str]:
    return [
        f"Runs: {s['runs']}",
        f"Indoor Runs: {s['indoor_runs']}",
        f"Distance: {s['distance_km']:.2f} km",
        f"Indoor Distance: {s['indoor_distance_km']:.2f} km",
        f"Moving Time: {s['moving_time']} hours",
        f"Indoor Time: {s['indoor_time']} hours",
        f"Elevation Gain: {s['elevation_gain_km']:.2f} km",
        f"Biggest Run: {s['biggest_run_km']:.2f} km",
        f"5 Km's: {s['fives']}",
        f"10 Km's: {s['tens']}",
        f"Half Marathons: {s['half_marathons']}",
        f"Full Marathons: {s['full_marathons']}",
        f"Ultra's: {s['ultras']}",
    ]


def _format_swim(s: dict) -> list[str]:
    return [
        f"Swims: {s['swims']}",
        f"Distance: {s['distance_km']:.2f} km",
        f"Moving Time: {s['moving_time']} hours",
        f"Biggest Swim: {s['biggest_swim_km']:.2f} km",
        f"500's: {s['five_hundreds']}",
        f"1000's: {s['thousands']}",
        f"1500's: {s['fifteen_hundreds']}",
        f"2000's: {s['two_thousands']}",
        f"3800's: {s['thirty_eight_hundreds']}",
    ]


def _format_walk(s: dict) -> list[str]:
    return [
        f"Walks: {s['walks']}",
        f"Distance: {s['distance_km']:.2f} km",
        f"Moving Time: {s['moving_time']} hours",
        f"Elevation Gain: {s['elevation_gain_km']:.2f} km",
        f"Biggest Walk: {s['biggest_walk_km']:.2f} km",
        f"2 Km's: {s['twos']}",
        f"5 Km's: {s['fives']}",
        f"10 Km's: {s['tens']}",
    ]


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _time_frame_bounds(time_frame: str) -> tuple[datetime | None, datetime | None]:
    """Return (start_utc, end_utc) for the given time frame.

    A None value means "no bound on that side".
    The end bound is *exclusive* — use ``activity_date < end`` in queries.
    """
    now = datetime.now(timezone.utc)

    if time_frame == "all_time":
        return None, None

    if time_frame == "year_to_date":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc), None

    if time_frame == "previous_year":
        return (
            datetime(now.year - 1, 1, 1, tzinfo=timezone.utc),
            datetime(now.year,     1, 1, tzinfo=timezone.utc),
        )

    if time_frame == "current_month":
        return datetime(now.year, now.month, 1, tzinfo=timezone.utc), None

    if time_frame == "previous_month":
        if now.month == 1:
            start = datetime(now.year - 1, 12, 1, tzinfo=timezone.utc)
            end   = datetime(now.year,      1, 1, tzinfo=timezone.utc)
        else:
            start = datetime(now.year, now.month - 1, 1, tzinfo=timezone.utc)
            end   = datetime(now.year, now.month,     1, tzinfo=timezone.utc)
        return start, end

    raise ValueError(f"Unknown time_frame: {time_frame!r}")


# Convenience aliases used throughout this module
_fmt_duration = seconds_to_hhmmss
_round2       = safe_round
