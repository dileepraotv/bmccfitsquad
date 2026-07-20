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
from app.utils import SPORT_ACTIVITY_TYPES as _SPORT_ACTIVITY_TYPES

logger = logging.getLogger(__name__)

# A completed month's data never changes, so once rendered it's cached for
# the rest of that month (and a bit beyond, to comfortably cover the whole
# window during which /recap could reasonably still target it).
_RECAP_CACHE_TTL_SECONDS = 60 * 86_400

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Base disciplines shown on the card — "RideEndurance" is a filtered subset
# of "Ride" (200km+ rides) used only for goals/stats, not a separate sport.
_RECAP_SPORTS: list[str] = ["Ride", "Run", "Walk", "Swim"]

_SPORT_LABELS: dict[str, str] = {"Ride": "RIDE", "Run": "RUN", "Walk": "WALK", "Swim": "SWIM"}

# (accent RGB, unit) per sport — Ride/Run/Walk in km, Swim in metres.
_SPORT_STYLE: dict[str, dict] = {
    "Ride": {"color": (212, 175, 55),  "unit": "km"},   # gold
    "Run":  {"color": (255, 140, 66),  "unit": "km"},   # orange
    "Walk": {"color": (124, 197, 118), "unit": "km"},   # green
    "Swim": {"color": (79, 168, 232),  "unit": "m"},    # blue
}

_MIN_STREAK_TO_SHOW = 3  # days — shorter streaks aren't worth calling out

_DATA_DIR   = pathlib.Path("data")
_LOGO_PATH  = _DATA_DIR / "bmcc_logo.jpg"
_FONT_DIR   = _DATA_DIR / "fonts"
_FONT_BOLD  = _FONT_DIR / "DejaVuSans-Bold.ttf"
_FONT_REG   = _FONT_DIR / "DejaVuSans.ttf"

_WHITE = (255, 255, 255)
_GRAY  = (140, 140, 140)
_DIM_GRAY = (70, 70, 70)
_GREEN = (110, 200, 110)
_RED   = (215, 95, 95)
_GOLD  = (212, 175, 55)


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


# ---------------------------------------------------------------------------
# Data aggregation
# ---------------------------------------------------------------------------

async def _sport_month_stats(
    db: AsyncSession, user_id, sport: str, start: datetime, end: datetime,
) -> tuple[float, int, float]:
    """Return (total_distance_m, activity_count, max_single_activity_m)."""
    types = _SPORT_ACTIVITY_TYPES[sport]
    result = await db.execute(
        select(
            func.coalesce(func.sum(Activity.distance_meters), 0.0),
            func.count(Activity.id),
            func.coalesce(func.max(Activity.distance_meters), 0.0),
        ).where(
            Activity.user_id == user_id,
            Activity.activity_type.in_(types),
            Activity.activity_date >= start,
            Activity.activity_date < end,
        )
    )
    total_m, count, max_m = result.one()
    return float(total_m or 0.0), int(count or 0), float(max_m or 0.0)


async def _sport_best_before(db: AsyncSession, user_id, sport: str, before: datetime) -> float:
    """Best (longest) single activity of `sport` strictly before `before`."""
    types = _SPORT_ACTIVITY_TYPES[sport]
    result = await db.execute(
        select(func.coalesce(func.max(Activity.distance_meters), 0.0))
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


async def compute_monthly_recap(db: AsyncSession, user: User, year: int, month: int) -> dict:
    """Aggregate this athlete's Ride/Run/Walk/Swim recap for a calendar month."""
    start, end = month_bounds(year, month)
    prev_year, prev_month = _previous_month(year, month)
    prev_start, prev_end = month_bounds(prev_year, prev_month)

    sports: list[dict] = []
    highlights: list[str] = []
    total_activities = 0
    top_sport_label = None
    top_sport_distance = 0.0

    for sport in _RECAP_SPORTS:
        style = _SPORT_STYLE[sport]
        total_m, count, max_m = await _sport_month_stats(db, user.id, sport, start, end)
        prev_total_m, _prev_count, _prev_max = await _sport_month_stats(
            db, user.id, sport, prev_start, prev_end
        )
        trend_label, trend_color = _trend(total_m, prev_total_m)

        if style["unit"] == "m":
            value_text = f"{int(round(total_m)):,}"
        elif total_m <= 0:
            value_text = "0"
        elif total_m >= 1000:
            value_text = f"{total_m / 1000:.0f}"
        else:
            value_text = f"{total_m / 1000:.1f}"

        sports.append({
            "key":         sport,
            "label":       _SPORT_LABELS[sport],
            "color":       style["color"],
            "value_text":  value_text,
            "unit":        style["unit"],
            "count":       count,
            "trend_label": trend_label,
            "trend_color": trend_color,
        })

        total_activities += count
        if total_m > top_sport_distance:
            top_sport_distance = total_m
            top_sport_label = sport

        # New PR — longest single activity of this sport, ever.
        if max_m > 0:
            best_before = await _sport_best_before(db, user.id, sport, start)
            if max_m > best_before:
                unit = style["unit"]
                pr_value = f"{int(round(max_m)):,} m" if unit == "m" else f"{max_m / 1000:.1f} km"
                highlights.append(f"New PR: Longest {sport} — {pr_value}")

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
# Sport icons — simple single-stroke line art, drawn directly (no external
# icon assets / emoji font dependency needed).
# ---------------------------------------------------------------------------

def _icon_bike(draw, box, color, w=5):
    x0, y0, x1, y1 = box
    h, wd = y1 - y0, x1 - x0
    cy = y0 + h * 0.68
    r = h * 0.20
    x1c, x2c = x0 + wd * 0.22, x1 - wd * 0.22
    bb = (x0 + wd * 0.48, cy)
    seat = (x0 + wd * 0.36, y0 + h * 0.20)
    bar = (x0 + wd * 0.78, y0 + h * 0.34)
    draw.ellipse([x1c - r, cy - r, x1c + r, cy + r], outline=color, width=w)
    draw.ellipse([x2c - r, cy - r, x2c + r, cy + r], outline=color, width=w)
    draw.line([bb, seat], fill=color, width=w)
    draw.line([bb, bar], fill=color, width=w)
    draw.line([(x1c, cy), seat], fill=color, width=w)
    draw.line([(x1c, cy), bb], fill=color, width=w)
    draw.line([bar, (x2c, cy)], fill=color, width=w)
    draw.line([(seat[0] - r * 0.5, seat[1]), (seat[0] + r * 0.5, seat[1])], fill=color, width=w)


def _icon_shoe(draw, box, color, w=5):
    x0, y0, x1, y1 = box
    h, wd = y1 - y0, x1 - x0
    sole_y = y0 + h * 0.78
    pts = [
        (x0 + wd * 0.08, sole_y),
        (x0 + wd * 0.05, y0 + h * 0.55),
        (x0 + wd * 0.20, y0 + h * 0.35),
        (x0 + wd * 0.45, y0 + h * 0.32),
        (x0 + wd * 0.55, y0 + h * 0.42),
        (x1 - wd * 0.10, y0 + h * 0.50),
        (x1 - wd * 0.03, y0 + h * 0.62),
        (x1 - wd * 0.05, sole_y),
    ]
    draw.line(pts + [pts[0]], fill=color, width=w, joint="curve")
    draw.line([(x0 + wd * 0.30, y0 + h * 0.34), (x0 + wd * 0.40, y0 + h * 0.50)], fill=color, width=max(2, w - 2))
    draw.line([(x0 + wd * 0.42, y0 + h * 0.34), (x0 + wd * 0.52, y0 + h * 0.50)], fill=color, width=max(2, w - 2))


def _icon_walker(draw, box, color, w=5):
    x0, y0, x1, y1 = box
    h, wd = y1 - y0, x1 - x0
    cx = x0 + wd * 0.5
    head_r = h * 0.10
    head_c = (cx, y0 + h * 0.16)
    draw.ellipse([head_c[0] - head_r, head_c[1] - head_r, head_c[0] + head_r, head_c[1] + head_r], outline=color, width=w)
    neck = (cx, y0 + h * 0.26)
    hip = (cx - wd * 0.03, y0 + h * 0.55)
    draw.line([neck, hip], fill=color, width=w)
    draw.line([hip, (x0 + wd * 0.20, y1 - h * 0.02)], fill=color, width=w)
    draw.line([hip, (x1 - wd * 0.15, y0 + h * 0.72)], fill=color, width=w)
    draw.line([(neck[0], neck[1] + h * 0.08), (x0 + wd * 0.15, y0 + h * 0.42)], fill=color, width=w)
    draw.line([(neck[0], neck[1] + h * 0.08), (x1 - wd * 0.10, y0 + h * 0.55)], fill=color, width=w)


def _icon_swimmer(draw, box, color, w=5):
    x0, y0, x1, y1 = box
    h, wd = y1 - y0, x1 - x0
    cy = y0 + h * 0.35
    head_r = h * 0.11
    hx = x1 - wd * 0.30
    draw.ellipse([hx - head_r, cy - head_r, hx + head_r, cy + head_r], outline=color, width=w)
    draw.line([(hx - head_r, cy + head_r * 0.3), (x0 + wd * 0.12, cy + h * 0.14)], fill=color, width=w, joint="curve")
    draw.line([(x0 + wd * 0.12, cy + h * 0.14), (x0 + wd * 0.0, cy - h * 0.02)], fill=color, width=w)
    draw.line([(hx + head_r * 0.6, cy - h * 0.06), (x1 - wd * 0.0, cy - h * 0.22)], fill=color, width=w)
    wave_y = y0 + h * 0.72
    for row in range(2):
        yy = wave_y + row * h * 0.14
        for i in range(3):
            xx = x0 + wd * 0.05 + i * wd * 0.32
            bbox = [xx, yy - h * 0.045, xx + wd * 0.30, yy + h * 0.045]
            start, end = (200, 340) if i % 2 == 0 else (20, 160)
            draw.arc(bbox, start, end, fill=color, width=max(3, w - 2))


_SPORT_ICONS = {"Ride": _icon_bike, "Run": _icon_shoe, "Walk": _icon_walker, "Swim": _icon_swimmer}


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


def render_recap_card(data: dict) -> bytes:
    """Render the recap dict into a PNG card. Returns raw PNG bytes.

    Drawn onto a generously tall canvas, then cropped to the actual content
    height at the end — so the card is the same height regardless of how
    many highlight lines there are, with no leftover empty space at the
    bottom (and no risk of clipping when there are several highlights).
    """
    from PIL import Image, ImageDraw

    W, MAX_H = 1080, 2000
    img = Image.new("RGB", (W, MAX_H), (0, 0, 0))

    # --- Faint BMCC crest watermark, centered ------------------------------
    try:
        logo = Image.open(_LOGO_PATH).convert("RGB")
        target = int(W * 0.8)
        logo = logo.resize((target, target))
        logo = logo.point(lambda p: int(p * 0.25))  # dim to ~25% brightness
        img.paste(logo, ((W - target) // 2, 220))
    except (OSError, FileNotFoundError):
        pass

    draw = ImageDraw.Draw(img)

    f_title     = _font(_FONT_BOLD, 42)
    f_subtitle  = _font(_FONT_REG, 32)
    f_daysline  = _font(_FONT_REG, 26)
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
    _draw_centered_text(
        draw, (W / 2, y), data["month_label"].upper() + " RECAP",
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
    icon_size = 96

    for sport in data["sports"]:
        row_top = y
        color = tuple(sport["color"])
        icon_box = (margin, row_top, margin + icon_size, row_top + icon_size)
        _SPORT_ICONS[sport["key"]](draw, icon_box, color)

        text_x = margin + icon_size + 34

        draw.text((text_x, row_top - 6), sport["label"], font=f_sport, fill=color)

        value_y = row_top + 36
        draw.text((text_x, value_y), sport["value_text"], font=f_value, fill=_WHITE)
        value_w = draw.textlength(sport["value_text"], font=f_value)
        draw.text((text_x + value_w + 12, value_y + 30), sport["unit"], font=f_unit, fill=_GRAY)

        count_label = "activity" if sport["count"] == 1 else "activities"
        draw.text(
            (text_x, row_top + 132), f"{sport['count']} {count_label}",
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
        pill_y0 = row_top + (icon_size - pill_h) / 2 + 4
        pill_y1 = pill_y0 + pill_h
        draw.rounded_rectangle(
            [pill_x0, pill_y0, pill_x1, pill_y1], radius=pill_h / 2,
            fill=(24, 24, 24), outline=(55, 55, 55), width=1,
        )
        draw.text(
            (pill_x0 + 24, pill_y0 + (pill_h - 30) / 2 - 2), pill_text,
            font=f_trend, fill=trend_color,
        )

        row_bottom = row_top + row_height
        if sport is not data["sports"][-1]:
            draw.line([(margin, row_bottom), (W - margin, row_bottom)], fill=_DIM_GRAY, width=1)
        y = row_bottom

    # --- Highlights ------------------------------------------------------------
    if data["highlights"]:
        draw.line([(margin, y), (W - margin, y)], fill=_DIM_GRAY, width=1)
        y += 40
        for line in data["highlights"]:
            draw.rounded_rectangle([margin, y + 6, margin + 8, y + 34], radius=4, fill=_GOLD)
            draw.text((margin + 26, y), line, font=f_highlight, fill=_GOLD)
            y += 48
        y += 10
    else:
        y += 30

    # --- Tagline -----------------------------------------------------------
    y += 40
    _draw_centered_text(draw, (W / 2, y), "Beyond Miles - Beyond Limits", f_tagline, _GOLD)
    y += 60

    img = img.crop((0, 0, W, min(y, MAX_H)))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
