"""Monthly recap: data aggregation + rendered card image.

Public API
----------
  compute_monthly_recap(db, user, year, month) -> dict
      Aggregates Ride/Run/Walk/Swim distance + count for the given calendar
      month, trend vs the previous month, any new personal records set that
      month, and the athlete's current activity streak as of month end.

  render_recap_card(data: dict) -> bytes
      Renders the recap dict into a PNG card (dark background, BMCC crest
      watermark, one row per sport, a highlights strip for PRs/streaks).

  month_bounds(year, month) -> tuple[datetime, datetime]
      UTC (start, end-exclusive) bounds for a calendar month.

  most_recently_completed_month(reference=None) -> tuple[int, int]
      (year, month) of the last full calendar month relative to `reference`
      (defaults to now). Used as the default target for /recap.
"""
from __future__ import annotations

import base64
import calendar
import io
import logging
import pathlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, User
from app.utils import DURATION_BASED_SPORTS as _DURATION_BASED_SPORTS
from app.utils import SPORT_ACTIVITY_TYPES as _SPORT_ACTIVITY_TYPES

logger = logging.getLogger(__name__)

# A completed month's data never changes, so once rendered it's cached for
# the rest of that month (and a bit beyond, to comfortably cover the whole
# window during which /recap could reasonably still target it).
_RECAP_CACHE_TTL_SECONDS = 60 * 86_400

# A preview of the *current, still in-progress* year (via /yearrecap before
# 31 Dec) must expire quickly — long enough to avoid re-rendering on rapid
# repeat taps, but nowhere near long enough to still be sitting in the
# cache by the time the real, final send happens at year end.
_YEARLY_PREVIEW_CACHE_TTL_SECONDS = 5 * 60

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Base disciplines shown on the card — "RideEndurance" is a filtered subset
# of "Ride" (200km+ rides) used only for goals/stats, not a separate sport.
_RECAP_SPORTS: list[str] = ["Ride", "Run", "Walk", "Swim"]

# "Other Activities" sports — these only earn a row on the card for a given
# period if the athlete actually logged one, so the card stays clean for the
# majority of members who only train the four core sports above.
_RECAP_OTHER_SPORTS: list[str] = ["Hiking", "Yoga", "RacketSports", "StrengthTraining"]

_SPORT_LABELS: dict[str, str] = {
    "Ride": "RIDE", "Run": "RUN", "Walk": "WALK", "Swim": "SWIM",
    "Hiking": "HIKE", "Yoga": "YOGA", "RacketSports": "RACKET", "StrengthTraining": "STRENGTH",
}

# (accent RGB, unit) per sport — Ride/Run/Walk/Hiking in km, Swim in metres,
# the duration-based "Other Activities" sports in hours.
# Muted palette to match the approved reference design (icons carry the
# accent color; the sport label text stays a single uniform gold — see
# _LABEL_COLOR — rather than repeating each accent on the text itself).
_SPORT_STYLE: dict[str, dict] = {
    "Ride": {"color": (196, 156, 92),  "unit": "km"},   # muted gold
    "Run":  {"color": (196, 122, 76),  "unit": "km"},   # muted terracotta
    "Walk": {"color": (116, 148, 92),  "unit": "km"},   # muted sage
    "Swim": {"color": (76, 110, 158),  "unit": "m"},    # muted steel blue
    "Hiking":           {"color": (150, 120, 80),  "unit": "km"},   # muted umber
    "Yoga":             {"color": (150, 120, 170), "unit": "hrs"},  # muted lavender
    "RacketSports":     {"color": (90, 150, 150),  "unit": "hrs"},  # muted teal
    "StrengthTraining": {"color": (170, 90, 90),   "unit": "hrs"},  # muted brick
}

_LABEL_COLOR = (204, 175, 133)  # uniform warm tan/gold for all sport labels

_MIN_STREAK_TO_SHOW = 3  # days — shorter streaks aren't worth calling out

_DATA_DIR   = pathlib.Path("data")
_LOGO_PATH  = _DATA_DIR / "bmcc_logo.jpg"
_CREST_PATH = _DATA_DIR / "bmcc_crest.png"
_ICON_DIR   = _DATA_DIR / "icons"
_ICON_PATHS = {
    "Ride": _ICON_DIR / "ride.png",
    "Run":  _ICON_DIR / "run.png",
    "Walk": _ICON_DIR / "walk.png",
    "Swim": _ICON_DIR / "swim.png",
}
# "Other Activities" sports have no bundled line-art PNG — they fall back to
# a simple drawn monogram badge (see _paste_fallback_icon) in their accent color.
_FALLBACK_MONOGRAM = {
    "Hiking": "H", "Yoga": "Y", "RacketSports": "R", "StrengthTraining": "S",
}
_FONT_DIR   = _DATA_DIR / "fonts"
_FONT_BOLD  = _FONT_DIR / "DejaVuSans-Bold.ttf"
_FONT_REG   = _FONT_DIR / "DejaVuSans.ttf"

_WHITE = (255, 255, 255)
_GRAY  = (140, 140, 140)
_DIM_GRAY = (70, 70, 70)
_GREEN = (110, 200, 110)
_RED   = (215, 95, 95)
_GOLD  = (212, 175, 55)
_BG_BLACK = (0, 0, 0)
_BG_NAVY  = (8, 14, 33)  # yearly recap card background — everything else unchanged


# ---------------------------------------------------------------------------
# Month arithmetic
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


def most_recently_completed_month(reference: datetime | None = None) -> tuple[int, int]:
    """(year, month) of the last full calendar month relative to `reference`.

    Always the previous month — the current month, even on its last day,
    isn't "completed" until it ends.
    """
    now = reference or datetime.now(timezone.utc)
    return _previous_month(now.year, now.month)


def next_month_name(year: int, month: int) -> str:
    """Full month name of the month immediately after (year, month)."""
    if month == 12:
        return calendar.month_name[1]
    return calendar.month_name[month + 1]


# ---------------------------------------------------------------------------
# Caption text
# ---------------------------------------------------------------------------

def build_recap_caption(data: dict, first_name: str) -> str:
    """Build the DM text sent alongside the recap card, ending with the
    goal-setting prompt for the upcoming month."""
    month_name = data["month_label"].split()[0]
    total = data["total_activities"]

    if total == 0:
        opener = f"👀 {month_name} was a quiet one, {first_name}."
        body = "Fresh month, fresh start — let's get moving."
    else:
        opener = f"🏆 {month_name} wrapped up strong, {first_name}!"
        top = data.get("top_sport_label")
        lead = ""
        if top:
            top_stat = next(s for s in data["sports"] if s["key"] == top)
            lead = f", led by {top_stat['value_text']} {top_stat['unit']} on the {top.lower()}"
        plural = "activity" if total == 1 else "activities"
        body = f"{total} {plural} this month{lead}."

    lines = [opener, "", body]
    if data["highlights"]:
        lines.append("")
        lines.extend(f"• {h}" for h in data["highlights"])

    upcoming = next_month_name(data["year"], data["month"])
    lines += ["", f"Want to set a goal for {upcoming}?"]
    return "\n".join(lines)


def build_yearly_recap_caption(data: dict, first_name: str) -> str:
    """Build the DM text sent alongside the yearly recap card, ending with
    the goal-setting prompt for the upcoming year."""
    year = data["year"]
    total = data["total_activities"]

    if total == 0:
        opener = f"👀 {year} was a quiet one, {first_name}."
        body = "New year, fresh start — let's get moving."
    else:
        opener = f"🏆 {year} wrapped up strong, {first_name}!"
        top = data.get("top_sport_label")
        lead = ""
        if top:
            top_stat = next(s for s in data["sports"] if s["key"] == top)
            lead = f", led by {top_stat['value_text']} {top_stat['unit']} on the {top.lower()}"
        plural = "activity" if total == 1 else "activities"
        body = f"{total} {plural} this year{lead}."

    lines = [opener, "", body]
    if data["highlights"]:
        lines.append("")
        lines.extend(f"• {h}" for h in data["highlights"])

    lines += ["", f"Want to set a goal for {year + 1}?"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cached fetch — a completed month's card never changes once generated, so
# repeat requests (manual /recap re-runs, the scheduled send picking up a
# month a user already peeked at) reuse the same render instead of paying
# the DB + Pillow cost again.
# ---------------------------------------------------------------------------

async def get_or_build_recap(db: AsyncSession, user: User, year: int, month: int) -> tuple[bytes, str]:
    """Return (image_png_bytes, caption_text) for this user + month, using
    the Redis cache when available and populating it otherwise."""
    from app.redis_client import get_redis, key_recap_caption, key_recap_image

    redis = await get_redis()
    img_key, cap_key = key_recap_image(user.id, year, month), key_recap_caption(user.id, year, month)

    cached_img_b64, cached_caption = await redis.get(img_key), await redis.get(cap_key)
    if cached_img_b64 and cached_caption:
        try:
            return base64.b64decode(cached_img_b64), cached_caption
        except Exception:
            logger.warning("recap cache: corrupt cached image for %s — regenerating", img_key)

    data = await compute_monthly_recap(db, user, year, month)
    image_bytes = render_recap_card(data)
    caption = build_recap_caption(data, user.telegram_first_name or "there")

    try:
        await redis.set(img_key, base64.b64encode(image_bytes).decode("ascii"), ex=_RECAP_CACHE_TTL_SECONDS)
        await redis.set(cap_key, caption, ex=_RECAP_CACHE_TTL_SECONDS)
    except Exception:
        logger.warning("recap cache: failed to write cache for %s", img_key)

    return image_bytes, caption


async def get_or_build_yearly_recap(db: AsyncSession, user: User, year: int) -> tuple[bytes, str]:
    """Return (image_png_bytes, caption_text) for this user + year, using
    the Redis cache when available and populating it otherwise.

    Unlike a calendar month, a year isn't "done changing" until it actually
    ends — /yearrecap lets someone preview the current, still-in-progress
    year, so that preview must not be cached with the same long TTL used
    for a genuinely completed year. Otherwise an earlier manual preview
    (e.g. in November) could still be sitting in the cache on 31 December
    and get served — with stale, incomplete data — to the real scheduled
    send that evening."""
    from app.redis_client import get_redis, key_yearly_recap_caption, key_yearly_recap_image

    redis = await get_redis()
    img_key, cap_key = key_yearly_recap_image(user.id, year), key_yearly_recap_caption(user.id, year)

    cached_img_b64, cached_caption = await redis.get(img_key), await redis.get(cap_key)
    if cached_img_b64 and cached_caption:
        try:
            return base64.b64decode(cached_img_b64), cached_caption
        except Exception:
            logger.warning("recap cache: corrupt cached yearly image for %s — regenerating", img_key)

    data = await compute_yearly_recap(db, user, year)
    image_bytes = render_recap_card(data, bg_color=_BG_NAVY)
    caption = build_yearly_recap_caption(data, user.telegram_first_name or "there")

    year_is_over = datetime.now(timezone.utc) >= datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    ttl = _RECAP_CACHE_TTL_SECONDS if year_is_over else _YEARLY_PREVIEW_CACHE_TTL_SECONDS

    try:
        await redis.set(img_key, base64.b64encode(image_bytes).decode("ascii"), ex=ttl)
        await redis.set(cap_key, caption, ex=ttl)
    except Exception:
        logger.warning("recap cache: failed to write cache for %s", img_key)

    return image_bytes, caption


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
) -> tuple[float, int, float]:
    """Return (total_metric, activity_count, max_single_activity_metric) —
    metric is metres for distance sports, seconds for duration sports."""
    types = _SPORT_ACTIVITY_TYPES[sport]
    metric_col = _sport_metric_column(sport)
    result = await db.execute(
        select(
            func.coalesce(func.sum(metric_col), 0.0),
            func.count(Activity.id),
            func.coalesce(func.max(metric_col), 0.0),
        ).where(
            Activity.user_id == user_id,
            Activity.activity_type.in_(types),
            Activity.activity_date >= start,
            Activity.activity_date < end,
        )
    )
    total, count, mx = result.one()
    return float(total or 0.0), int(count or 0), float(mx or 0.0)


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
    into the display string shown as the card's big number."""
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


async def _build_sport_row(
    db: AsyncSession, user: User, sport: str, start: datetime, end: datetime,
    prev_start: datetime, prev_end: datetime,
) -> tuple[dict, float, str | None]:
    """Return (row_dict, total_metric, pr_highlight_or_None) for one sport
    over one period. Shared by the monthly and yearly recap aggregators."""
    style = _SPORT_STYLE[sport]
    unit = style["unit"]
    total, count, mx = await _sport_month_stats(db, user.id, sport, start, end)
    prev_total, _prev_count, _prev_max = await _sport_month_stats(db, user.id, sport, prev_start, prev_end)
    trend_label, trend_color = _trend(total, prev_total)

    row = {
        "key":         sport,
        "label":       _SPORT_LABELS[sport],
        "color":       style["color"],
        "value_text":  _format_metric_value(total, unit),
        "unit":        unit,
        "count":       count,
        "trend_label": trend_label,
        "trend_color": trend_color,
    }

    highlight = None
    if mx > 0:
        best_before = await _sport_best_before(db, user.id, sport, start)
        if mx > best_before:
            noun = "session" if sport in _DURATION_BASED_SPORTS else sport
            highlight = f"New PR: Longest {noun} — {_format_pr_value(mx, unit)}"

    return row, total, highlight


async def compute_monthly_recap(db: AsyncSession, user: User, year: int, month: int) -> dict:
    """Aggregate this athlete's recap for a calendar month — the four core
    sports always appear; "Other Activities" sports only appear if the
    athlete actually logged one that month."""
    start, end = month_bounds(year, month)
    prev_year, prev_month = _previous_month(year, month)
    prev_start, prev_end = month_bounds(prev_year, prev_month)

    sports: list[dict] = []
    highlights: list[str] = []
    total_activities = 0
    top_sport_label = None
    top_sport_distance = 0.0

    for sport in _RECAP_SPORTS:
        row, total, highlight = await _build_sport_row(db, user, sport, start, end, prev_start, prev_end)
        sports.append(row)
        total_activities += row["count"]
        if total > top_sport_distance:
            top_sport_distance = total
            top_sport_label = sport
        if highlight:
            highlights.append(highlight)

    for sport in _RECAP_OTHER_SPORTS:
        row, _total, highlight = await _build_sport_row(db, user, sport, start, end, prev_start, prev_end)
        if row["count"] <= 0:
            continue
        sports.append(row)
        total_activities += row["count"]
        if highlight:
            highlights.append(highlight)

    streak = await _current_streak_days(db, user.id, end)
    if streak >= _MIN_STREAK_TO_SHOW:
        highlights.append(f"{streak}-day activity streak")

    days_in_month = (end - start).days
    active_days = await _distinct_active_days(db, user.id, start, end)
    rest_days = max(0, days_in_month - active_days)

    month_label = f"{calendar.month_name[month]} {year}"
    prev_month_abbr = calendar.month_abbr[prev_month]
    # Fill in "vs <month>" only for badges that carry a real percentage —
    # not "New!" / "None" / "—", which already stand on their own.
    for s in sports:
        if s["trend_label"].endswith("%"):
            s["trend_label"] = f"{s['trend_label']} vs {prev_month_abbr}"

    return {
        "year": year,
        "month": month,
        "month_label": month_label,
        "athlete_name": user.strava_athlete_name or user.telegram_first_name,
        "sports": sports,
        "highlights": highlights,
        "total_activities": total_activities,
        "top_sport_label": top_sport_label,
        "active_days": active_days,
        "rest_days": rest_days,
    }


async def compute_yearly_recap(db: AsyncSession, user: User, year: int) -> dict:
    """Aggregate this athlete's recap for a full calendar year — same shape
    as compute_monthly_recap, trended against the prior year instead of the
    prior month."""
    start, end = year_bounds(year)
    prev_start, prev_end = year_bounds(year - 1)

    sports: list[dict] = []
    highlights: list[str] = []
    total_activities = 0
    top_sport_label = None
    top_sport_distance = 0.0

    for sport in _RECAP_SPORTS:
        row, total, highlight = await _build_sport_row(db, user, sport, start, end, prev_start, prev_end)
        sports.append(row)
        total_activities += row["count"]
        if total > top_sport_distance:
            top_sport_distance = total
            top_sport_label = sport
        if highlight:
            highlights.append(highlight)

    for sport in _RECAP_OTHER_SPORTS:
        row, _total, highlight = await _build_sport_row(db, user, sport, start, end, prev_start, prev_end)
        if row["count"] <= 0:
            continue
        sports.append(row)
        total_activities += row["count"]
        if highlight:
            highlights.append(highlight)

    streak = await _current_streak_days(db, user.id, end)
    if streak >= _MIN_STREAK_TO_SHOW:
        highlights.append(f"{streak}-day activity streak")

    days_in_year = (end - start).days
    active_days = await _distinct_active_days(db, user.id, start, end)
    rest_days = max(0, days_in_year - active_days)

    for s in sports:
        if s["trend_label"].endswith("%"):
            s["trend_label"] = f"{s['trend_label']} vs {year - 1}"

    return {
        "year": year,
        "title": f"{year} YEAR IN REVIEW",
        "athlete_name": user.strava_athlete_name or user.telegram_first_name,
        "sports": sports,
        "highlights": highlights,
        "total_activities": total_activities,
        "top_sport_label": top_sport_label,
        "active_days": active_days,
        "rest_days": rest_days,
    }


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------

def _font(path: pathlib.Path, size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default(size=size)


def _sanitize_for_font(text: str) -> str:
    """Strip characters the bundled DejaVu Sans font can't render (emoji,
    dingbats, etc.) so they don't show up as empty tofu boxes on the card.
    Keeps all Latin script (including accents) — only drops codepoints
    above the Latin Extended block, which is exclusively symbols/emoji."""
    return "".join(ch for ch in text if ord(ch) < 0x2000).strip() or text[:1]


def _trend_rgb(color_key: str) -> tuple[int, int, int]:
    return {"up": _GREEN, "down": _RED, "flat": _GRAY, "new": _GRAY}.get(color_key, _GRAY)


# ---------------------------------------------------------------------------
# Sport icons — bundled line-art PNGs (approved reference assets), keyed
# onto a transparent alpha so they composite cleanly over the dark card
# regardless of their own flat near-black/charcoal backdrop.
# ---------------------------------------------------------------------------

def _load_art_rgba(path: pathlib.Path, max_size: int, midpoint: int = 40, ramp: int = 20):
    """Load a flat-background line-art PNG and turn its backdrop transparent,
    using luminosity thresholding (the source assets are two-tone: a dark
    flat backdrop + bright line art, so a midpoint cut with a short
    anti-aliasing ramp reproduces a clean cutout without any fringing)."""
    from PIL import Image

    img = Image.open(path).convert("RGB")
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    lo, hi = max(0, midpoint - ramp), min(255, midpoint + ramp)
    span = max(1, hi - lo)
    lut = [0 if v <= lo else 255 if v >= hi else int((v - lo) * 255 / span) for v in range(256)]
    alpha = img.convert("L").point(lut)
    rgba = img.convert("RGBA")
    rgba.putalpha(alpha)
    return rgba


def _paste_icon(img, sport_key: str, box, color: tuple[int, int, int] | None = None) -> None:
    """Paste the bundled icon for `sport_key` centered within `box`
    (x0, y0, x1, y1), preserving its native aspect ratio. Sports with no
    bundled line-art PNG (the "Other Activities" sports) fall back to a
    drawn monogram badge in their accent color instead."""
    path = _ICON_PATHS.get(sport_key)
    if path is None:
        _paste_fallback_icon(img, sport_key, box, color)
        return
    x0, y0, x1, y1 = box
    size = int(max(x1 - x0, y1 - y0))
    icon = _load_art_rgba(path, size)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    px = int(cx - icon.width / 2)
    py = int(cy - icon.height / 2)
    img.paste(icon, (px, py), icon)


def _paste_fallback_icon(img, sport_key: str, box, color: tuple[int, int, int] | None) -> None:
    """Draw a simple ringed-monogram badge for sports with no bundled icon."""
    from PIL import ImageDraw

    x0, y0, x1, y1 = box
    size = max(x1 - x0, y1 - y0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    ring_color = color or _GRAY
    draw = ImageDraw.Draw(img)
    r = size / 2 * 0.82
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ring_color, width=5)

    letter = _FALLBACK_MONOGRAM.get(sport_key, sport_key[:1])
    f = _font(_FONT_BOLD, int(size * 0.4))
    bbox = draw.textbbox((0, 0), letter, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]), letter, font=f, fill=ring_color)


def _draw_centered_text(draw, xy_center, text, font, fill, letter_spacing: int = 0):
    """Draw text horizontally centered at xy_center, optionally with extra
    letter-spacing (rendered by drawing each glyph separately)."""
    x_center, y = xy_center
    if letter_spacing <= 0:
        w = draw.textlength(text, font=font)
        draw.text((x_center - w / 2, y), text, font=font, fill=fill)
        return
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + letter_spacing * (len(text) - 1)
    x = x_center - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += w + letter_spacing


def render_recap_card(data: dict, bg_color: tuple[int, int, int] = _BG_BLACK) -> bytes:
    """Render the recap dict into a PNG card. Returns raw PNG bytes.

    Drawn onto a generously tall canvas, then cropped to the actual content
    height at the end — so the card is the same height regardless of how
    many highlight lines there are, with no leftover empty space at the
    bottom (and no risk of clipping when there are several highlights).

    `bg_color` only swaps the canvas background (e.g. navy for the yearly
    recap vs black for the monthly one) — every other visual element is
    identical between the two card types.
    """
    from PIL import Image, ImageDraw

    # MAX_H is a generous upper bound only — the canvas is cropped to actual
    # content height at the end. Raised from 2000 to comfortably fit the four
    # core sports plus all four "Other Activities" rows in the same card.
    W, MAX_H = 1080, 3200
    img = Image.new("RGB", (W, MAX_H), bg_color)
    draw = ImageDraw.Draw(img)

    f_title     = _font(_FONT_BOLD, 42)
    f_subtitle  = _font(_FONT_REG, 32)
    f_daysline  = _font(_FONT_REG, 31)
    f_sport     = _font(_FONT_BOLD, 28)
    f_value     = _font(_FONT_BOLD, 68)
    f_unit      = _font(_FONT_REG, 30)
    f_count     = _font(_FONT_REG, 24)
    f_trend     = _font(_FONT_BOLD, 28)
    f_highlight = _font(_FONT_BOLD, 26)
    f_tagline   = _font(_FONT_REG, 26)

    margin = 80

    # --- Header --------------------------------------------------------------
    y = 70
    title = data.get("title") or (data["month_label"].upper() + " RECAP")
    _draw_centered_text(
        draw, (W / 2, y), title,
        f_title, _WHITE, letter_spacing=6,
    )
    y += 60
    _draw_centered_text(
        draw, (W / 2, y), _sanitize_for_font(data["athlete_name"]), f_subtitle, _GRAY,
    )
    y += 50
    active, rest = data["active_days"], data["rest_days"]
    _draw_centered_text(
        draw, (W / 2, y),
        f"{active} active day{'s' if active != 1 else ''}  \u00b7  "
        f"{rest} rest day{'s' if rest != 1 else ''}",
        f_daysline, _GOLD,
    )
    y += 70

    # --- Sport rows ------------------------------------------------------------
    row_height = 230
    icon_size = 100
    # Gap below each divider line before the next row's content starts —
    # without this, a label sits almost flush against the line above it.
    row_top_pad = 26
    # Icon is vertically centered on the row's visual anchor (the big
    # number), not on the row's raw top edge — otherwise icons of
    # different silhouettes read as "off-center" against the text block.
    icon_anchor_offset = 54

    for sport in data["sports"]:
        row_start = y
        content_top = row_start + row_top_pad
        icon_cy = content_top + icon_anchor_offset
        icon_box = (margin, icon_cy - icon_size / 2, margin + icon_size, icon_cy + icon_size / 2)
        _paste_icon(img, sport["key"], icon_box, sport.get("color"))

        text_x = margin + icon_size + 30

        draw.text((text_x, content_top - 6), sport["label"], font=f_sport, fill=_LABEL_COLOR)

        value_y = content_top + 36
        draw.text((text_x, value_y), sport["value_text"], font=f_value, fill=_WHITE)
        value_w = draw.textlength(sport["value_text"], font=f_value)
        draw.text((text_x + value_w + 12, value_y + 30), sport["unit"], font=f_unit, fill=_GRAY)

        if sport["key"] in _DURATION_BASED_SPORTS:
            count_label = "session" if sport["count"] == 1 else "sessions"
        else:
            count_label = "activity" if sport["count"] == 1 else "activities"
        draw.text(
            (text_x, content_top + 132), f"{sport['count']} {count_label}",
            font=f_count, fill=_GRAY,
        )

        # Trend pill, right-aligned, vertically centered on the row
        trend_color = _trend_rgb(sport["trend_color"])
        arrow = {"up": "\u2191", "down": "\u2193", "flat": "\u2013", "new": "\u2013"}[sport["trend_color"]]
        pill_text = f"{arrow} {sport['trend_label']}"
        pill_w = draw.textlength(pill_text, font=f_trend) + 48
        pill_h = 60
        pill_x1 = W - margin
        pill_x0 = pill_x1 - pill_w
        pill_y0 = content_top + (icon_size - pill_h) / 2 + 4
        pill_y1 = pill_y0 + pill_h
        draw.rounded_rectangle(
            [pill_x0, pill_y0, pill_x1, pill_y1], radius=pill_h / 2,
            fill=(24, 24, 24), outline=(55, 55, 55), width=1,
        )
        draw.text(
            (pill_x0 + 24, pill_y0 + (pill_h - 30) / 2 - 2), pill_text,
            font=f_trend, fill=trend_color,
        )

        row_bottom = row_start + row_height
        if sport is not data["sports"][-1]:
            draw.line([(margin, row_bottom), (W - margin, row_bottom)], fill=_DIM_GRAY, width=1)
        y = row_bottom

    # --- Highlights ------------------------------------------------------------
    if data["highlights"]:
        draw.line([(margin, y), (W - margin, y)], fill=_DIM_GRAY, width=1)
        y += row_top_pad
        for line in data["highlights"]:
            draw.rounded_rectangle([margin, y + 6, margin + 8, y + 34], radius=4, fill=_GOLD)
            draw.text((margin + 26, y), line, font=f_highlight, fill=_GOLD)
            y += 48
        y += 6
    else:
        y += 20

    # --- BMCC crest + tagline ------------------------------------------------
    y += 20
    try:
        crest_size = 132
        crest = _load_art_rgba(_CREST_PATH, crest_size, midpoint=100, ramp=40)
        img.paste(crest, (int(W / 2 - crest.width / 2), int(y)), crest)
        y += crest_size + 20
    except (OSError, FileNotFoundError):
        pass

    _draw_centered_text(draw, (W / 2, y), "Beyond Miles - Beyond Limits", f_tagline, _GOLD)
    y += 60

    img = img.crop((0, 0, W, min(y, MAX_H)))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
