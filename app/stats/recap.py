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

import calendar
import io
import pathlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, User
from app.utils import SPORT_ACTIVITY_TYPES as _SPORT_ACTIVITY_TYPES

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
    """Render the recap dict into a PNG card. Returns raw PNG bytes."""
    from PIL import Image, ImageDraw

    W, H = 1080, 1440
    img = Image.new("RGB", (W, H), (0, 0, 0))

    # --- Faint BMCC crest watermark, centered ------------------------------
    try:
        logo = Image.open(_LOGO_PATH).convert("RGB")
        target = int(W * 0.8)
        logo = logo.resize((target, target))
        logo = logo.point(lambda p: int(p * 0.10))  # dim to ~10% brightness
        img.paste(logo, ((W - target) // 2, (H - target) // 2 - 40))
    except (OSError, FileNotFoundError):
        pass

    draw = ImageDraw.Draw(img)

    f_title    = _font(_FONT_BOLD, 42)
    f_subtitle = _font(_FONT_REG, 32)
    f_sport    = _font(_FONT_BOLD, 26)
    f_value    = _font(_FONT_BOLD, 68)
    f_unit     = _font(_FONT_REG, 30)
    f_count    = _font(_FONT_REG, 24)
    f_trend    = _font(_FONT_BOLD, 22)
    f_highlight = _font(_FONT_BOLD, 26)
    f_tagline  = _font(_FONT_REG, 26)

    margin = 80

    # --- Header --------------------------------------------------------------
    _draw_centered_text(
        draw, (W / 2, 70), data["month_label"].upper() + " RECAP",
        f_title, _WHITE, letter_spacing=6,
    )
    _draw_centered_text(
        draw, (W / 2, 130), _sanitize_for_font(data["athlete_name"]), f_subtitle, _GRAY,
    )

    # --- Sport rows ------------------------------------------------------------
    row_top = 240
    row_height = 230
    badge_d = 90

    for sport in data["sports"]:
        cy = row_top + badge_d // 2

        # Accent circle badge with sport initial
        color = tuple(sport["color"])
        draw.ellipse(
            [margin, row_top, margin + badge_d, row_top + badge_d],
            outline=color, width=3,
        )
        initial = sport["key"][0] if sport["key"] != "Run" else "Ru"
        iw = draw.textlength(initial, font=f_sport)
        draw.text(
            (margin + badge_d / 2 - iw / 2, cy - 15),
            initial, font=f_sport, fill=color,
        )

        text_x = margin + badge_d + 40

        draw.text((text_x, row_top - 6), sport["label"], font=f_sport, fill=color)

        value_y = row_top + 34
        draw.text((text_x, value_y), sport["value_text"], font=f_value, fill=_WHITE)
        value_w = draw.textlength(sport["value_text"], font=f_value)
        draw.text((text_x + value_w + 12, value_y + 30), sport["unit"], font=f_unit, fill=_GRAY)

        count_label = "activity" if sport["count"] == 1 else "activities"
        draw.text(
            (text_x, row_top + 130), f"{sport['count']} {count_label}",
            font=f_count, fill=_GRAY,
        )

        # Trend pill, right-aligned
        trend_color = _trend_rgb(sport["trend_color"])
        arrow = {"up": "\u2191", "down": "\u2193", "flat": "\u2013", "new": "\u2013"}[sport["trend_color"]]
        pill_text = f"{arrow} {sport['trend_label']}"
        pill_w = draw.textlength(pill_text, font=f_trend) + 40
        pill_h = 46
        pill_x1 = W - margin
        pill_x0 = pill_x1 - pill_w
        pill_y0 = row_top + 40
        pill_y1 = pill_y0 + pill_h
        draw.rounded_rectangle(
            [pill_x0, pill_y0, pill_x1, pill_y1], radius=pill_h / 2,
            fill=(24, 24, 24), outline=(50, 50, 50), width=1,
        )
        draw.text(
            (pill_x0 + 20, pill_y0 + (pill_h - 22) / 2), pill_text,
            font=f_trend, fill=trend_color,
        )

        row_bottom = row_top + row_height
        if sport is not data["sports"][-1]:
            draw.line([(margin, row_bottom), (W - margin, row_bottom)], fill=_DIM_GRAY, width=1)
        row_top = row_bottom

    # --- Highlights ------------------------------------------------------------
    if data["highlights"]:
        draw.line([(margin, row_top), (W - margin, row_top)], fill=_DIM_GRAY, width=1)
        hy = row_top + 30
        for line in data["highlights"]:
            draw.rounded_rectangle([margin, hy + 6, margin + 8, hy + 34], radius=4, fill=_GOLD)
            draw.text((margin + 26, hy), line, font=f_highlight, fill=_GOLD)
            hy += 46
        row_top = hy + 10
    else:
        row_top += 30

    # --- Tagline -----------------------------------------------------------
    _draw_centered_text(draw, (W / 2, H - 70), "Beyond Miles - Beyond Limits", f_tagline, _GOLD)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
