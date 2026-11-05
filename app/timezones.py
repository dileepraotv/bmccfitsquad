"""Per-user timezone resolution — shared by stats, goals, recap, and the
scheduled-recap/checkpoint triggers.

Why this exists
----------------
Activity.activity_date is always stored as a UTC instant. Every calendar
boundary ("this month", "this week", "today") computed against it used to
be anchored to a single hardcoded IST offset, on the assumption that every
BMCC member is in India. That's no longer true — members can be anywhere
in the world, so each user's *own* IANA timezone (stored on User.timezone)
must be used instead wherever a boundary is personal (stats, goals, recap,
streaks). Group-wide, shared-across-everyone data (the /leaderboard) is the
one deliberate exception — see its own module for why it stays pinned to
one reference zone instead of going per-user.

zoneinfo.ZoneInfo (not a fixed timedelta offset) is used throughout,
because unlike IST, most non-Indian timezones observe DST — a fixed offset
would silently be wrong for half the year.
"""
from __future__ import annotations

import logging
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# Fallback for any user who hasn't set/detected a timezone yet — BMCC's
# original, still-largest user base.
DEFAULT_TIMEZONE = "Asia/Kolkata"

# Strava's athlete.timezone field looks like "(GMT+05:30) Asia/Calcutta" —
# the IANA name is everything after the ") ".
_STRAVA_TZ_RE = re.compile(r"\)\s*(.+)$")


def resolve_tz(tz_name: str | None) -> ZoneInfo:
    """Return a ZoneInfo for *tz_name*, falling back to DEFAULT_TIMEZONE for
    anything missing, empty, or no longer a valid IANA name (tzdata is
    occasionally reorganised; a stale stored name should never crash a
    stats/goals/recap call)."""
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("resolve_tz: invalid/unknown timezone %r — falling back to default", tz_name)
    return ZoneInfo(DEFAULT_TIMEZONE)


def user_tz(user) -> ZoneInfo:
    """Convenience wrapper — resolve straight from a User ORM object."""
    return resolve_tz(getattr(user, "timezone", None))


def is_valid_tz(tz_name: str) -> bool:
    try:
        ZoneInfo(tz_name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def parse_strava_timezone(raw: str | None) -> str | None:
    """Extract the IANA name from Strava's athlete.timezone field, e.g.
    "(GMT+05:30) Asia/Calcutta" -> "Asia/Calcutta". Returns None if *raw*
    is empty or doesn't look like Strava's documented format, or if the
    extracted name isn't a tzdata name we can actually resolve."""
    if not raw:
        return None
    match = _STRAVA_TZ_RE.search(raw)
    candidate = match.group(1).strip() if match else raw.strip()
    return candidate if is_valid_tz(candidate) else None
