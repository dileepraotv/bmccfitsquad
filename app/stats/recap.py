"""Monthly/yearly recap: data aggregation + monospace Telegram text message.

Public API
----------
  compute_monthly_recap(db, user, year, month) -> dict
      Aggregates Ride/Run/Walk/Swim/"Other Activities" distance, elevation,
      moving time, and calories for the given calendar month (up to *now* if
      the month is still in progress), trend vs the previous month, any new
      personal records set that period, and the athlete's current activity
      streak as of the period end.

  compute_yearly_recap(db, user, year) -> dict
      Same shape as compute_monthly_recap, trended against the prior year.

  get_or_build_recap(db, user, year, month) -> str
  get_or_build_yearly_recap(db, user, year) -> str
      Cached (Redis, short TTL) end-to-end text message for the athlete —
      the value used directly as the Telegram message body.

  month_bounds(year, month) -> tuple[datetime, datetime]
      UTC (start, end-exclusive) bounds for a calendar month.
"""
from __future__ import annotations

import calendar
import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, User
from app.utils import DURATION_BASED_SPORTS as _DURATION_BASED_SPORTS
from app.utils import SPORT_ACTIVITY_TYPES as _SPORT_ACTIVITY_TYPES
from app.utils import format_kv_lines

logger = logging.getLogger(__name__)

# Deliberately short — this exists purely to absorb an accidental double-tap
# of /recap or /yearrecap (or the scheduled send immediately followed by a
# manual check within the same minute), not to avoid the underlying cost.
# The DB aggregation only hits our own indexed Postgres tables (no Strava API
# calls, no external quota at risk) and text formatting is essentially free,
# so there's no real cost benefit to caching longer — and a long TTL only
# creates a class of "why hasn't this updated" bugs.
_RECAP_CACHE_TTL_SECONDS = 60

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Base disciplines shown on the recap — "RideEndurance" is a filtered subset
# of "Ride" (200km+ rides) used only for goals/stats, not a separate sport.
_RECAP_SPORTS: list[str] = ["Ride", "Run", "Walk", "Swim"]

# "Other Activities" sports — these only earn a row on the recap for a given
# period if the athlete actually logged one, so the message stays short for
# the majority of members who only train the four core sports above.
_RECAP_OTHER_SPORTS: list[str] = ["Hiking", "Yoga", "RacketSports", "StrengthTraining"]

_SPORT_LABELS: dict[str, str] = {
    "Ride": "RIDE", "Run": "RUN", "Walk": "WALK", "Swim": "SWIM",
    "Hiking": "HIKE", "Yoga": "YOGA", "RacketSports": "RACKET", "StrengthTraining": "STRENGTH",
}

_RECAP_EMOJI: dict[str, str] = {
    "Ride": "🚴", "Run": "🏃", "Walk": "🚶", "Swim": "🏊",
    "Hiking": "🥾", "Yoga": "🧘", "RacketSports": "🏸", "StrengthTraining": "🏋️",
}

# Ride/Run/Walk/Hiking share GPS distance (km), elevation, and calories.
# Swim is distance-based but in metres, with no meaningful elevation.
# Yoga/RacketSports/StrengthTraining are duration-based (see
# DURATION_BASED_SPORTS) — their headline value is hours, detail is minutes.
_KM_SPORTS: set[str] = {"Ride", "Run", "Walk", "Hiking"}

_SPORT_UNIT: dict[str, str] = {
    "Ride": "km", "Run": "km", "Walk": "km", "Hiking": "km", "Swim": "m",
    "Yoga": "hrs", "RacketSports": "hrs", "StrengthTraining": "hrs",
}

_MIN_STREAK_TO_SHOW = 3  # days — shorter streaks aren't worth calling out


# ---------------------------------------------------------------------------
# Month/year arithmetic
# ---------------------------------------------------------------------------

def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """UTC (start, end-exclusive) bounds for a calendar month."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def _previous_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def year_bounds(year: int) -> tuple[datetime, datetime]:
    """UTC (start, end-exclusive) bounds for a calendar year."""
    return datetime(year, 1, 1, tzinfo=timezone.utc), datetime(year + 1, 1, 1, tzinfo=timezone.utc)


def next_month_name(year: int, month: int) -> str:
    """Full month name of the month immediately after (year, month)."""
    if month == 12:
        return calendar.month_name[1]
    return calendar.month_name[month + 1]


# ---------------------------------------------------------------------------
# Cached fetch — a completed month's/year's recap never changes once
# generated, so repeat requests (manual re-runs, the scheduled send picking
# up a period a user already peeked at) reuse the same text instead of
# paying the DB aggregation cost again.
# ---------------------------------------------------------------------------

async def get_or_build_recap(db: AsyncSession, user: User, year: int, month: int) -> str:
    """Return the full recap message text for this user + month, using the
    Redis cache when available and populating it otherwise."""
    from app.redis_client import get_redis, key_recap_text

    redis = await get_redis()
    key = key_recap_text(user.id, year, month)

    cached = await redis.get(key)
    if cached:
        return cached

    data = await compute_monthly_recap(db, user, year, month)
    upcoming = next_month_name(year, month)
    text = build_recap_message(data, user.telegram_first_name or "there", upcoming)

    try:
        await redis.set(key, text, ex=_RECAP_CACHE_TTL_SECONDS)
    except Exception:
        logger.warning("recap cache: failed to write cache for %s", key)

    return text


async def get_or_build_yearly_recap(db: AsyncSession, user: User, year: int) -> str:
    """Return the full yearly recap message text for this user + year, using
    the Redis cache when available and populating it otherwise.

    With only a 1-minute TTL (see _RECAP_CACHE_TTL_SECONDS), an in-progress
    /yearrecap preview and a genuinely completed year can safely share the
    same short-lived cache — there's no realistic way a preview taken
    months earlier is still sitting in the cache by the time the real
    scheduled send happens on 31 December."""
    from app.redis_client import get_redis, key_yearly_recap_text

    redis = await get_redis()
    key = key_yearly_recap_text(user.id, year)

    cached = await redis.get(key)
    if cached:
        return cached

    data = await compute_yearly_recap(db, user, year)
    text = build_recap_message(data, user.telegram_first_name or "there", str(year + 1))

    try:
        await redis.set(key, text, ex=_RECAP_CACHE_TTL_SECONDS)
    except Exception:
        logger.warning("recap cache: failed to write cache for %s", key)

    return text


# ---------------------------------------------------------------------------
# Data aggregation
# ---------------------------------------------------------------------------

def _sport_metric_column(sport: str):
    """Distance-based sports are measured in metres; the duration-based
    "Other Activities" sports (Yoga, Racket Sports, Strength Training) have
    no meaningful GPS distance, so they're measured by moving time instead."""
    return Activity.moving_time_seconds if sport in _DURATION_BASED_SPORTS else Activity.distance_meters


async def _sport_month_stats(
    db: AsyncSession, user_id, sport: str, start: datetime, end: datetime,
) -> tuple[float, int, float, float, float, float]:
    """Return (total_metric, activity_count, max_single_activity_metric,
    total_moving_time_s, total_elevation_m, total_calories_kcal).

    metric is metres for distance sports, seconds for duration sports.
    total_moving_time_s is *always* seconds regardless of sport, so callers
    can compare "how much this sport dominated the period" across sports
    that use different display units (e.g. Ride's km vs Yoga's hours) — see
    _build_sport_row's effort_seconds. Elevation/calories are fetched for
    every sport uniformly (harmless zeros for sports where they don't
    apply) so the recap's "fun index" totals can sum across all of them
    without a second query.
    """
    types = _SPORT_ACTIVITY_TYPES[sport]
    metric_col = _sport_metric_column(sport)
    result = await db.execute(
        select(
            func.coalesce(func.sum(metric_col), 0.0),
            func.count(Activity.id),
            func.coalesce(func.max(metric_col), 0.0),
            func.coalesce(func.sum(Activity.moving_time_seconds), 0.0),
            func.coalesce(func.sum(Activity.elevation_gain), 0.0),
            func.coalesce(func.sum(Activity.calories), 0.0),
        ).where(
            Activity.user_id == user_id,
            Activity.activity_type.in_(types),
            Activity.activity_date >= start,
            Activity.activity_date < end,
        )
    )
    total, count, mx, total_time_s, elevation_m, calories = result.one()
    return (
        float(total or 0.0), int(count or 0), float(mx or 0.0),
        float(total_time_s or 0.0), float(elevation_m or 0.0), float(calories or 0.0),
    )


async def _sport_best_before(db: AsyncSession, user_id, sport: str, before: datetime) -> float:
    """Best (longest) single activity of `sport` strictly before `before`."""
    types = _SPORT_ACTIVITY_TYPES[sport]
    metric_col = _sport_metric_column(sport)
    result = await db.execute(
        select(func.coalesce(func.max(metric_col), 0.0))
        .where(
            Activity.user_id == user_id,
            Activity.activity_type.in_(types),
            Activity.activity_date < before,
        )
    )
    return float(result.scalar_one() or 0.0)


async def _distinct_active_days(db: AsyncSession, user_id, start: datetime, end: datetime) -> int:
    """Count of distinct calendar dates with at least one activity of any
    type (not just Ride/Run/Walk/Swim) in [start, end)."""
    result = await db.execute(
        select(func.count(func.distinct(func.date(Activity.activity_date))))
        .where(
            Activity.user_id == user_id,
            Activity.activity_date >= start,
            Activity.activity_date < end,
        )
    )
    return int(result.scalar_one() or 0)


async def _current_streak_days(db: AsyncSession, user_id, as_of_end: datetime) -> int:
    """Consecutive-day activity streak ending on the most recent active day
    at or before `as_of_end` (looking back up to 60 days)."""
    lookback_start = as_of_end - timedelta(days=60)
    result = await db.execute(
        select(Activity.activity_date)
        .where(
            Activity.user_id == user_id,
            Activity.activity_date >= lookback_start,
            Activity.activity_date < as_of_end,
        )
    )
    dates = {row[0].date() for row in result.fetchall()}
    if not dates:
        return 0

    d = max(dates)
    streak = 0
    while d in dates:
        streak += 1
        d -= timedelta(days=1)
    return streak


def _trend(current_m: float, previous_m: float) -> tuple[str, str]:
    """Return (label, color_key) for the trend badge — color_key in
    {"up", "down", "flat", "new"}.

    Handled explicitly rather than via a raw percentage: a sport going to
    zero this month would otherwise show an alarming "-100%", and a sport
    with no history the previous month has no percentage to show at all.
    """
    if current_m <= 0 and previous_m <= 0:
        return "—", "flat"
    if current_m <= 0:
        return "None", "flat"
    if previous_m <= 0:
        return "New!", "new"
    pct = round((current_m - previous_m) / previous_m * 100)
    if pct > 0:
        return f"+{pct}%", "up"
    if pct < 0:
        return f"{pct}%", "down"
    return "0%", "flat"


def _format_metric_value(total: float, unit: str) -> str:
    """Render a raw metric total (metres, or seconds for duration sports)
    into the display string shown as the recap's headline number."""
    if unit == "m":
        return f"{int(round(total)):,}"
    if unit == "hrs":
        return f"{total / 3600:.1f}" if total > 0 else "0"
    if total <= 0:
        return "0"
    if total >= 1000:
        return f"{total / 1000:.0f}"
    return f"{total / 1000:.1f}"


def _format_pr_value(mx: float, unit: str) -> str:
    if unit == "m":
        return f"{int(round(mx)):,} m"
    if unit == "hrs":
        return f"{mx / 3600:.1f} hrs"
    return f"{mx / 1000:.1f} km"


def _format_hm(seconds: float) -> str:
    """Convert seconds to a short "9h 12m" (or "45m" if under an hour)
    display string, used for moving-time lines throughout the recap."""
    total_min = round(max(0.0, seconds) / 60)
    h, m = divmod(total_min, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m"


async def _build_sport_row(
    db: AsyncSession, user: User, sport: str, start: datetime, end: datetime,
    prev_start: datetime, prev_end: datetime,
) -> tuple[dict, str | None, float]:
    """Return (row_dict, pr_highlight_or_None, effort_seconds) for one sport
    over one period. Shared by the monthly and yearly recap aggregators.
    effort_seconds is total moving time regardless of sport — used by
    callers to pick the period's "top sport" fairly across sports that
    display different units (km vs hrs)."""
    unit = _SPORT_UNIT[sport]
    total, count, mx, effort_seconds, elevation_m, calories = await _sport_month_stats(
        db, user.id, sport, start, end
    )
    prev_total, *_rest = await _sport_month_stats(db, user.id, sport, prev_start, prev_end)
    trend_label, trend_color = _trend(total, prev_total)

    row = {
        "key":         sport,
        "label":       _SPORT_LABELS[sport],
        "value_text":  _format_metric_value(total, unit),
        "unit":        unit,
        "count":       count,
        "trend_label": trend_label,
        "trend_color": trend_color,
        "moving_time_s": effort_seconds,
        "elevation_m": elevation_m,
        "calories":    calories,
        "_raw_total":  total,
    }

    highlight = None
    if mx > 0:
        best_before = await _sport_best_before(db, user.id, sport, start)
        if mx > best_before:
            noun = "session" if sport in _DURATION_BASED_SPORTS else sport
            highlight = f"New PR: Longest {noun} — {_format_pr_value(mx, unit)}"

    return row, highlight, effort_seconds


async def _aggregate_recap(
    db: AsyncSession, user: User, start: datetime, end: datetime,
    prev_start: datetime, prev_end: datetime, trend_suffix: str,
) -> dict:
    """Shared aggregation body for a monthly or yearly recap — builds every
    sport row, highlights, and the period-wide fun-index totals."""
    sports: list[dict] = []
    highlights: list[str] = []
    total_activities = 0
    top_sport_label = None
    top_sport_effort = 0.0

    for sport in _RECAP_SPORTS:
        row, highlight, effort = await _build_sport_row(db, user, sport, start, end, prev_start, prev_end)
        sports.append(row)
        total_activities += row["count"]
        if effort > top_sport_effort:
            top_sport_effort = effort
            top_sport_label = sport
        if highlight:
            highlights.append(highlight)

    for sport in _RECAP_OTHER_SPORTS:
        row, highlight, effort = await _build_sport_row(db, user, sport, start, end, prev_start, prev_end)
        if row["count"] <= 0:
            continue
        sports.append(row)
        total_activities += row["count"]
        if effort > top_sport_effort:
            top_sport_effort = effort
            top_sport_label = sport
        if highlight:
            highlights.append(highlight)

    streak = await _current_streak_days(db, user.id, end)
    if streak >= _MIN_STREAK_TO_SHOW:
        highlights.append(f"{streak}-day activity streak")

    for s in sports:
        if s["trend_label"].endswith("%"):
            s["trend_label"] = f"{s['trend_label']} {trend_suffix}"

    total_distance_km = sum(s["_raw_total"] / 1000 for s in sports if s["key"] in _KM_SPORTS)
    total_elevation_m = sum(s["elevation_m"] for s in sports if s["key"] in _KM_SPORTS)
    total_moving_time_s = sum(s["moving_time_s"] for s in sports)
    total_calories = sum(s["calories"] for s in sports)

    return {
        "sports": sports,
        "highlights": highlights,
        "total_activities": total_activities,
        "top_sport_label": top_sport_label,
        "total_distance_km": total_distance_km,
        "total_elevation_m": total_elevation_m,
        "total_moving_time_s": total_moving_time_s,
        "total_calories": total_calories,
    }


async def compute_monthly_recap(db: AsyncSession, user: User, year: int, month: int) -> dict:
    """Aggregate this athlete's recap for a calendar month, up to *now* if
    the month is still in progress — the four core sports always appear;
    "Other Activities" sports only appear if the athlete actually logged
    one that month."""
    start, end = month_bounds(year, month)
    prev_year, prev_month = _previous_month(year, month)
    prev_start, prev_end = month_bounds(prev_year, prev_month)
    prev_month_abbr = calendar.month_abbr[prev_month]

    agg = await _aggregate_recap(db, user, start, end, prev_start, prev_end, f"vs {prev_month_abbr}")

    # A completed past month uses its full day count as the rest-day
    # denominator. But an in-progress current month (the point-in-time
    # /recap preview) would otherwise count every day from today through
    # month-end as a "rest day" it hasn't even happened yet — cap the
    # denominator at days actually elapsed (inclusive of today) instead,
    # same fix already applied to the yearly recap below.
    now = datetime.now(timezone.utc)
    if now >= end:
        days_in_month = (end - start).days
    else:
        days_in_month = (now.date() - start.date()).days + 1
    active_days = await _distinct_active_days(db, user.id, start, end)
    rest_days = max(0, days_in_month - active_days)

    return {
        "year": year,
        "month": month,
        "month_label": f"{calendar.month_name[month]} {year}",
        "athlete_name": user.strava_athlete_name or user.telegram_first_name,
        "active_days": active_days,
        "rest_days": rest_days,
        **agg,
    }


async def compute_yearly_recap(db: AsyncSession, user: User, year: int) -> dict:
    """Aggregate this athlete's recap for a full calendar year — same shape
    as compute_monthly_recap, trended against the prior year instead of the
    prior month."""
    start, end = year_bounds(year)
    prev_start, prev_end = year_bounds(year - 1)

    agg = await _aggregate_recap(db, user, start, end, prev_start, prev_end, f"vs {year - 1}")

    # /yearrecap is explicitly a preview of the *current, still in-progress*
    # year — using the full year length there wrongly counts every day from
    # today through Dec 31 as a "rest day" it hasn't even happened yet.
    now = datetime.now(timezone.utc)
    if now >= end:
        days_in_year = (end - start).days
    else:
        days_in_year = (now.date() - start.date()).days + 1
    active_days = await _distinct_active_days(db, user.id, start, end)
    rest_days = max(0, days_in_year - active_days)

    return {
        "year": year,
        "athlete_name": user.strava_athlete_name or user.telegram_first_name,
        "active_days": active_days,
        "rest_days": rest_days,
        **agg,
    }


# ---------------------------------------------------------------------------
# "Fun index" — personal calorie/distance/elevation/time comparisons.
# Each builder returns a formatted line or None if its specific stat isn't
# available (e.g. Everest needs elevation, Moonwalk needs a big distance) —
# the picker shuffles the pool and returns the first line that renders.
# Roast-flavored comparisons (Slowpoke, Nap) are deliberately excluded here;
# this section is celebratory, separate from the opt-out Roast Mode feature
# on activity notifications.
# ---------------------------------------------------------------------------

def _idx_pizza(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 2024))
    noun = "large pepperoni pizza" if n == 1 else "large pepperoni pizzas"
    return f"🍕 *The Pizza Index*: You burned {int(round(total_kcal)):,} calories this {period_word} — that's about {n} {noun}."


def _idx_burger(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 563))
    noun = "Big Mac" if n == 1 else "Big Macs"
    return f"🍔 *The Burger Index*: You burned {int(round(total_kcal)):,} calories this {period_word} — that's about {n} {noun}, guilt-free."


def _idx_chai(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 50))
    return f"☕ *The Chai Index*: {int(round(total_kcal)):,} calories burned this {period_word} — that's {n} cup{'s' if n != 1 else ''} of chai, sugar included."


def _idx_chocolate(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 150))
    return f"🍫 *The Chocolate Index*: {int(round(total_kcal)):,} calories burned this {period_word} — that's {n} bar{'s' if n != 1 else ''} of Dairy Milk. Worth it."


def _idx_thali(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 1233))
    noun = "butter chicken thali" if n == 1 else "butter chicken thalis"
    return f"🍛 *The Thali Index*: {int(round(total_kcal)):,} calories burned this {period_word} — that's {n} full {noun}."


def _idx_beer(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 210))
    return f"🍺 *The Beer Index*: {int(round(total_kcal)):,} calories burned this {period_word} — that's {n} pint{'s' if n != 1 else ''}, earned honestly."


def _idx_jalebi(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 150))
    noun = "jalebi" if n == 1 else "jalebis"
    return f"🍩 *The Jalebi Index*: {int(round(total_kcal)):,} calories burned this {period_word} — that's roughly {n} {noun}."


def _idx_samosa(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 150))
    noun = "samosa" if n == 1 else "samosas"
    return f"🥟 *The Samosa Index*: You burned {int(round(total_kcal)):,} calories this {period_word} — that's about {n} {noun}, chutney included."


def _idx_vadapav(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 300))
    noun = "vada pav" if n == 1 else "vada pavs"
    return f"🍔 *The Vada Pav Index*: {int(round(total_kcal)):,} calories burned this {period_word} — that's {n} {noun}, Mumbai's finest fuel."


def _idx_dosa(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 350))
    noun = "masala dosa" if n == 1 else "masala dosas"
    return f"🥞 *The Dosa Index*: {int(round(total_kcal)):,} calories burned this {period_word} — that's {n} {noun}, sambar and all."


def _idx_biryani(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 650))
    noun = "plate of biryani" if n == 1 else "plates of biryani"
    return f"🍚 *The Biryani Index*: {int(round(total_kcal)):,} calories burned this {period_word} — that's {n} full {noun}. Hyderabadi, obviously."


def _idx_ladoo(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 180))
    noun = "ladoo" if n == 1 else "ladoos"
    return f"🍬 *The Ladoo Index*: {int(round(total_kcal)):,} calories burned this {period_word} — that's {n} {noun} — festival-season guilt-free."


def _idx_gulabjamun(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 150))
    noun = "gulab jamun" if n == 1 else "gulab jamuns"
    return f"🍯 *The Gulab Jamun Index*: {int(round(total_kcal)):,} calories burned this {period_word} — that's {n} {noun} swimming in syrup."


def _idx_filtercoffee(total_kcal: float, period_word: str) -> str | None:
    n = max(1, round(total_kcal / 60))
    noun = "cup" if n == 1 else "cups"
    return f"☕ *The Filter Coffee Index*: {int(round(total_kcal)):,} calories burned this {period_word} — that's {n} {noun} of filter kaapi, tumbler-and-davara style."


_CALORIE_INDEX_BUILDERS = [
    _idx_pizza, _idx_burger, _idx_chai, _idx_chocolate, _idx_thali, _idx_beer, _idx_jalebi,
    _idx_samosa, _idx_vadapav, _idx_dosa, _idx_biryani, _idx_ladoo, _idx_gulabjamun, _idx_filtercoffee,
]


def _idx_commute(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if total_km <= 0:
        return None
    pct = total_km / 2800 * 100
    return f"🛣️ *The Commute Index*: You covered {total_km:.0f} km this {period_word} — that's {pct:.0f}% of a Mumbai–Delhi round trip."


def _idx_globe(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if total_km <= 0:
        return None
    pct = total_km / 40_075 * 100
    return f"🌍 *The Globe Index*: {total_km:.0f} km covered this {period_word} — that's {pct:.1f}% of the way around the Earth."


def _idx_everest(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if elevation_m <= 0:
        return None
    n = max(1, round(elevation_m / 8849))
    climbs = "climb" if n == 1 else "climbs"
    return f"🏔️ *The Everest Index*: You climbed {int(round(elevation_m)):,} m of elevation this {period_word} — that's {n} Mount Everest {climbs}, base camp to summit."


def _idx_marathon(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if total_km <= 0:
        return None
    n = total_km / 42.195
    return f"🎽 *The Marathon Index*: {total_km:.0f} km logged this {period_word} — that's {n:.1f} marathons back to back."


def _idx_stadium(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if total_km <= 0:
        return None
    fields = round(total_km * 1000 / 105)
    return f"⚽ *The Stadium Index*: {total_km:.0f} km covered this {period_word} — that's {fields:,} football fields laid end to end."


def _idx_moonwalk(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if total_km <= 0:
        return None
    years = max(1, round(384_400 / total_km))
    return f"🌕 *The Moonwalk Index*: You covered {total_km:.0f} km this {period_word} — at this rate, the Moon is only {years} years away."


def _idx_mumbai_local(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if total_km <= 0:
        return None
    n = total_km / 120  # Churchgate–Virar round trip, ~60 km one-way
    return f"🚆 *The Mumbai Local Index*: You covered {total_km:.0f} km this {period_word} — that's {n:.1f} Churchgate-to-Virar round trips."


def _idx_golden_quad(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if total_km <= 0:
        return None
    pct = total_km / 5_846 * 100
    return f"🛣️ *The Golden Quadrilateral Index*: {total_km:.0f} km covered this {period_word} — that's {pct:.0f}% of the Golden Quadrilateral (Delhi–Mumbai–Chennai–Kolkata highway loop)."


def _idx_cricket_pitch(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if total_km <= 0:
        return None
    pitches = round(total_km * 1_000 / 20.12)
    return f"🏏 *The Cricket Pitch Index*: {total_km:.0f} km covered this {period_word} — that's {pitches:,} cricket pitches laid end to end."


def _idx_statue_of_unity(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if elevation_m <= 0:
        return None
    n = max(1, round(elevation_m / 182))
    noun = "Statue of Unity height" if n == 1 else "Statue of Unity heights"
    return f"🗿 *The Statue of Unity Index*: You climbed {int(round(elevation_m)):,} m of elevation this {period_word} — that's {n} {noun} (182 m), the tallest statue in the world."


def _idx_qutub_minar(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if elevation_m <= 0:
        return None
    n = max(1, round(elevation_m / 73))
    noun = "Qutub Minar" if n == 1 else "Qutub Minars"
    return f"🕌 *The Qutub Minar Index*: {int(round(elevation_m)):,} m climbed this {period_word} — that's {n} {noun}, stacked on top of each other."


def _idx_kedarnath(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if elevation_m <= 0:
        return None
    n = elevation_m / 1_500  # ~1,500 m gain over the Gaurikund–Kedarnath trek
    return f"🏔️ *The Kedarnath Trek Index*: {int(round(elevation_m)):,} m of elevation gain this {period_word} — that's roughly {n:.1f} Kedarnath treks (~1,500 m gain over 16 km)."


def _idx_kashmir_kanyakumari(total_km: float, elevation_m: float, period_word: str) -> str | None:
    if total_km <= 0:
        return None
    pct = total_km / 3_700 * 100
    return f"🇮🇳 *The Kashmir–Kanyakumari Index*: {total_km:.0f} km logged this {period_word} — that's {pct:.0f}% of the length of India, tip to tip."


_DISTANCE_INDEX_BUILDERS = [
    _idx_commute, _idx_globe, _idx_everest, _idx_marathon, _idx_stadium,
    _idx_mumbai_local, _idx_golden_quad, _idx_cricket_pitch,
    _idx_statue_of_unity, _idx_qutub_minar, _idx_kedarnath,
]
# Only makes sense at yearly scale — a monthly total projected to the Moon,
# or a large % of India's length, always reads as an absurdly large/small
# number at monthly scale.
_DISTANCE_INDEX_BUILDERS_YEARLY = _DISTANCE_INDEX_BUILDERS + [_idx_moonwalk, _idx_kashmir_kanyakumari]


def _idx_netflix(total_s: float, period_word: str) -> str | None:
    if total_s <= 0:
        return None
    n = max(1, round((total_s / 60) / 22))
    ep = "episode" if n == 1 else "episodes"
    return f"🎬 *The Netflix Index*: You spent {_format_hm(total_s)} moving this {period_word} — that's {n} {ep} of a 22-min sitcom you didn't watch."


def _idx_bollywood(total_s: float, period_word: str) -> str | None:
    if total_s <= 0:
        return None
    n = max(1, round((total_s / 60) / 150))
    mv = "movie" if n == 1 else "movies"
    return f"🎥 *The Bollywood Index*: {_format_hm(total_s)} of activity this {period_word} — that's {n} full Bollywood {mv}, interval included."


def _idx_flight(total_s: float, period_word: str) -> str | None:
    if total_s <= 0:
        return None
    n = (total_s / 3600) / 9
    return f"✈️ *The Flight Index*: {_format_hm(total_s)} moving this {period_word} — that's {n:.1f} Mumbai–London flights."


def _idx_workday(total_s: float, period_word: str) -> str | None:
    if total_s <= 0:
        return None
    n = (total_s / 3600) / 8
    return f"💼 *The Workday Index*: {_format_hm(total_s)} logged this {period_word} — that's {n:.1f} full work days spent moving instead of in meetings."


def _idx_ipl(total_s: float, period_word: str) -> str | None:
    if total_s <= 0:
        return None
    n = max(1, round((total_s / 3600) / 3.5))
    match = "IPL match" if n == 1 else "IPL matches"
    return f"🏏 *The IPL Match Index*: You spent {_format_hm(total_s)} moving this {period_word} — that's {n} {match}, strategic timeouts included."


def _idx_mumbai_commute(total_s: float, period_word: str) -> str | None:
    if total_s <= 0:
        return None
    n = max(1, round((total_s / 3600) / 1.5))
    trip = "one-way Mumbai office commute" if n == 1 else "one-way Mumbai office commutes"
    return f"🚆 *The Mumbai Commute Index*: {_format_hm(total_s)} moving this {period_word} — that's {n} {trip}, except you were the one actually moving."


def _idx_ramayan(total_s: float, period_word: str) -> str | None:
    if total_s <= 0:
        return None
    n = max(1, round((total_s / 60) / 45))
    ep = "episode" if n == 1 else "episodes"
    return f"📺 *The Ramayan Rerun Index*: {_format_hm(total_s)} of activity this {period_word} — that's {n} {ep} of the 90s Ramayan, no ad breaks for you though."


def _idx_test_match(total_s: float, period_word: str) -> str | None:
    if total_s <= 0:
        return None
    n = (total_s / 3600) / 6
    return f"🏟️ *The Test Match Day Index*: {_format_hm(total_s)} moving this {period_word} — that's {n:.1f} full days of Test cricket, tea break included."


_TIME_INDEX_BUILDERS = [
    _idx_netflix, _idx_bollywood, _idx_flight, _idx_workday,
    _idx_ipl, _idx_mumbai_commute, _idx_ramayan, _idx_test_match,
]


def _pick_line(builders: list, *args) -> str | None:
    pool = builders[:]
    random.shuffle(pool)
    for builder in pool:
        line = builder(*args)
        if line:
            return line
    return None


def build_fun_facts_lines(data: dict, period_word: str, is_yearly: bool) -> list[str]:
    """Pick one comparison per category (calories / distance-elevation /
    time) from the athlete's own totals for the period. A category is
    skipped entirely if its underlying stat is zero."""
    lines: list[str] = []

    cal_line = _pick_line(_CALORIE_INDEX_BUILDERS, data.get("total_calories", 0.0), period_word)
    if cal_line:
        lines.append(cal_line)

    distance_builders = _DISTANCE_INDEX_BUILDERS_YEARLY if is_yearly else _DISTANCE_INDEX_BUILDERS
    dist_line = _pick_line(
        distance_builders, data.get("total_distance_km", 0.0), data.get("total_elevation_m", 0.0), period_word,
    )
    if dist_line:
        lines.append(dist_line)

    time_line = _pick_line(_TIME_INDEX_BUILDERS, data.get("total_moving_time_s", 0.0), period_word)
    if time_line:
        lines.append(time_line)

    return lines


# ---------------------------------------------------------------------------
# Message rendering
# ---------------------------------------------------------------------------

_SPORT_DISPLAY_NAME: dict[str, str] = {
    "Ride": "Ride", "Run": "Run", "Walk": "Walk", "Swim": "Swim",
    "Hiking": "Hike", "Yoga": "Yoga", "RacketSports": "Racket Sports",
    "StrengthTraining": "Strength Training",
}


def _trend_arrow(color_key: str) -> str:
    # Telegram's Bot API has no way to color plain text/arrow characters, so
    # a colored circle stands in for the requested "up in blue / down in
    # red" treatment — closest achievable equivalent without custom emoji.
    return {"up": "🔵↑", "down": "🔴↓", "flat": "🔵–", "new": "🔵–"}.get(color_key, "🔵–")


def _sport_lines(row: dict) -> list[str]:
    """One bold "<emoji> <Sport Name> (<trend>)" title line, followed by an
    aligned monospace key/value block (via format_kv_lines) with the
    per-sport-family detail metrics. The trend badge sits on the bold title
    line rather than the Distance/Duration value line — keeping that line
    short enough to not wrap on narrow (mobile) screens."""
    key = row["key"]
    emoji = _RECAP_EMOJI.get(key, "")
    name = _SPORT_DISPLAY_NAME.get(key, key)
    trend = f"{_trend_arrow(row['trend_color'])} {row['trend_label']}"
    header = f"*{emoji} {name} ({trend})*"

    headline_value = f"{row['value_text']} {row['unit']}"

    if key in _KM_SPORTS:
        pairs = [
            ("Distance", headline_value),
            ("Activities", str(row["count"])),
            ("Elevation", f"{int(round(row['elevation_m'])):,} m"),
            ("Moving Time", _format_hm(row["moving_time_s"])),
            ("Calories", f"{int(round(row['calories'])):,} kcal"),
        ]
    elif key == "Swim":
        pairs = [
            ("Distance", headline_value),
            ("Activities", str(row["count"])),
            ("Active Time", _format_hm(row["moving_time_s"])),
        ]
    else:
        pairs = [
            ("Duration", headline_value),
            ("Sessions", str(row["count"])),
            ("Minutes", str(round(row["moving_time_s"] / 60))),
        ]

    return [header, format_kv_lines(pairs)]


def render_recap_text(data: dict) -> str:
    """Render the recap dict into the stats block — title+athlete name (one
    combined bold line), active/rest days, and one bold title + aligned
    monospace detail block per sport. Detail lines are wrapped via
    format_kv_lines, which uses inline `code` spans (not a fenced ``` block)
    so they stay monospace-aligned without triggering Telegram's narrower
    boxed rendering + "Copy" button — the same convention already used for
    activity notifications and /stats."""
    is_yearly = "month_label" not in data
    header_title = f"{data['year']} Year in Review" if is_yearly else f"{data['month_label']} Recap"

    lines: list[str] = [
        f"🏆 *{header_title} — {data['athlete_name']}*",
        "",
        f"`Active Days : {data['active_days']}   Rest Days : {data['rest_days']}`",
    ]

    for sport in data["sports"]:
        lines.append("")
        lines.extend(_sport_lines(sport))

    if data["highlights"]:
        lines.append("")
        lines.append("✨ *Highlights*")
        for h in data["highlights"]:
            lines.append(f"`• {h}`")

    return "\n".join(lines)


def build_recap_message(data: dict, first_name: str, upcoming_period_label: str) -> str:
    """Build the full Telegram message: the monospace stats block, a fun
    facts section (or a "quiet period" note if nothing was logged), and the
    goal-setting prompt for the upcoming month/year."""
    is_yearly = "month_label" not in data
    period_word = "year" if is_yearly else "month"

    sections = [render_recap_text(data)]

    if data["total_activities"] == 0:
        period_name = str(data["year"]) if is_yearly else data["month_label"].split()[0]
        sections.append(f"👀 {period_name} was a quiet one, {first_name} — fresh start, let's get moving.")
    else:
        fun_lines = build_fun_facts_lines(data, period_word, is_yearly)
        if fun_lines:
            adjective = "yearly" if is_yearly else "monthly"
            sections.append(f"Your {adjective} metrics till now compare to:")
            sections.extend(fun_lines)

    sections.append(f"Want to set a goal for {upcoming_period_label}?")

    return "\n\n".join(sections)
