"""All Telegram command and callback handlers.

Goals flow design
-----------------
Goals use a purely callback-driven flow (no ConversationHandler) so state is
never lost when the Railway web dyno restarts.  State is passed forward in
callback_data using a compact encoding:

  goal:sport:<sport>                  → sport chosen
  goal:cat:<sport>|<category>         → category chosen
  goal:count:<sport>|<category>|<n>   → count confirmed (inline keyboard buttons 1-12)
  goal:period:<sport>|<cat>|<n>|<per> → period chosen → save to DB

The only text-input step (entering a count) was replaced with a count picker
keyboard (1-12 buttons) to avoid needing a ConversationHandler for text input.
"""
from __future__ import annotations

import logging
import pathlib
import random
import uuid as _uuid_mod
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.database import AsyncSessionLocal
from app.models import Activity, Goal, User
from app.stats.calculator import calculate_stats, format_stats_message
from app.telegram.keyboards import (
    NAV_GOALS,
    NAV_HELP,
    NAV_STATS,
    confirm_keyboard,
    connect_strava_keyboard,
    main_menu_keyboard,
    nav_keyboard,
    stats_nav_keyboard,
    stats_period_keyboard,
    stats_sport_keyboard,
)

logger = logging.getLogger(__name__)

_QUOTES_PATH = pathlib.Path("data/quotes.txt")

# ---------------------------------------------------------------------------
# In-process draft registry — avoids Redis round-trips on every message
# ---------------------------------------------------------------------------
# When a goal draft OR activity-edit draft is created, we record the
# telegram_user_id here.  handle_unknown skips both Redis GETs unless the
# user is in this set.  The set is process-local so it resets on restart,
# but that is fine: after a restart the draft in Redis has also expired or
# the user is starting fresh.  The cost of one extra GET after a restart is
# trivial compared to eliminating GETs for every unrelated message.
_users_with_draft: set[int] = set()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_handlers(app: Application) -> None:
    """Attach all handlers to the PTB Application.

    All command handlers are restricted to private chats so the bot does not
    respond to /stats, /goals etc. when added to a group conversation.
    """
    _priv = filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start",         cmd_start,         filters=_priv))
    app.add_handler(CommandHandler("help",          cmd_help,          filters=_priv))
    app.add_handler(CommandHandler("connect",       cmd_connect,       filters=_priv))
    app.add_handler(CommandHandler("disconnect",    cmd_disconnect,    filters=_priv))
    app.add_handler(CommandHandler("sync",          cmd_sync,          filters=_priv))
    app.add_handler(CommandHandler("fullsync",      cmd_fullsync,      filters=_priv))
    app.add_handler(CommandHandler("stats",         cmd_stats,         filters=_priv))
    app.add_handler(CommandHandler("goals",         cmd_goals,         filters=_priv))
    app.add_handler(CommandHandler("cancel",        cmd_cancel,        filters=_priv))
    app.add_handler(CommandHandler("skip",          cmd_skip,          filters=_priv))
    app.add_handler(CommandHandler("leaderboard",   cmd_leaderboard,   filters=_priv))
    app.add_handler(CommandHandler("notifications", cmd_notifications, filters=_priv))
    app.add_handler(CommandHandler("quote",         cmd_quote,         filters=_priv))

    app.add_handler(CallbackQueryHandler(handle_callback))

    # Persistent nav bar — registered in group 0 BEFORE handle_unknown.
    # Within a single group PTB stops at the first matching handler, so these
    # will consume the nav button messages and handle_unknown never sees them.
    _priv_text = filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(_priv_text & filters.Regex(f"^{NAV_STATS}$"), cmd_stats))
    app.add_handler(MessageHandler(_priv_text & filters.Regex(f"^{NAV_GOALS}$"), cmd_goals))
    app.add_handler(MessageHandler(_priv_text & filters.Regex(f"^{NAV_HELP}$"),  cmd_help))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_unknown)
    )
    app.add_error_handler(handle_error)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_or_create_user(update: Update) -> User:
    """Upsert the Telegram user into the DB and return the ORM object."""
    tg_user = update.effective_user
    async with AsyncSessionLocal() as db:
        stmt = (
            pg_insert(User)
            .values(
                telegram_user_id=tg_user.id,
                telegram_username=tg_user.username,
                telegram_first_name=tg_user.first_name or "Friend",
            )
            .on_conflict_do_update(
                index_elements=["telegram_user_id"],
                set_={
                    "telegram_username":   tg_user.username,
                    "telegram_first_name": tg_user.first_name or "Friend",
                },
            )
            .returning(User)
        )
        result = await db.execute(stmt)
        await db.commit()
        return result.fetchone()[0]


def _random_quote() -> str:
    try:
        lines = [l.strip() for l in _QUOTES_PATH.read_text().splitlines() if l.strip()]
        return random.choice(lines) if lines else "Keep moving forward."
    except FileNotFoundError:
        return "Every kilometre counts."


def _escape_md(text: str) -> str:
    """Escape text for Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.strava.auth import build_authorization_url, generate_oauth_state

    try:
        user = await _get_or_create_user(update)
    except Exception:
        logger.exception("cmd_start: DB error")
        await update.message.reply_text(
            "Sorry, I couldn't reach the database right now. Please try again in a moment."
        )
        return
    name = update.effective_user.first_name or "there"

    if user.strava_athlete_id:
        athlete_name = user.strava_athlete_name or name
        await update.message.reply_text(
            f"👋 Welcome back, *{_escape_md(name)}*\\!\n\n"
            f"Connected as *{_escape_md(athlete_name)}*\\.\n\n"
            f"Use the menu below or type /help to see all commands\\.",
            parse_mode="MarkdownV2",
            reply_markup=nav_keyboard(),
        )
        await update.message.reply_text(
            "What would you like to do?",
            reply_markup=main_menu_keyboard(),
        )
    else:
        state = await generate_oauth_state(update.effective_user.id)
        auth_url = build_authorization_url(state)
        await update.message.reply_text(
            "*Welcome to BMCC FitSquad\\!* 🚴🏃🏊🚶\n\n"
            "_\"It's the Ride That Matters\"_\n\n"
            "I help you track your Strava cycling, running, swimming, and walking activities, "
            "along with your statistics and fitness goals\\.\n\n"
            "To get started, connect your Strava account using the *Connect Strava* button below\\.\n\n"
            "You can also use /help anytime to see all available commands and features\\.\n\n"
            "Stay connected with BMCC:\n"
            "🌐 [www\\.beyondmiles\\.cc](http://www.beyondmiles.cc)\n"
            "📸 Instagram: @beyondmilescc",
            parse_mode="MarkdownV2",
            reply_markup=connect_strava_keyboard(auth_url),
            disable_web_page_preview=True,
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*BMCC FitSquad — All Commands*\n\n"
        "🔗 *Strava*\n"
        "/connect — Link your Strava account\n"
        "/disconnect — Unlink your Strava account\n"
        "/sync — Fetch latest activities \\(fast, day\\-to\\-day use\\)\n"
        "/fullsync — Rebuild your full history \\(use only if stats look wrong\\)\n\n"
        "📊 *Stats \\& Goals*\n"
        "/stats — View activity stats by sport and time period\n"
        "/goals — Set, delete or check your fitness goals\n\n"
        "🏆 *Group*\n"
        "/leaderboard — Monthly distance leaderboard\n\n"
        "💬 *Other*\n"
        "/quote — Random motivational quote\n"
        "/cancel — Cancel any in\\-progress action\n"
        "/start — Welcome message and main menu\n"
        "/help — Show this list\n\n"
        "💡 *Tip:* New activities sync automatically when you save them on Strava\\. "
        "Use /sync only if a recent activity is missing\\.\n\n"
        "🌐 [www\\.beyondmiles\\.cc](http://www.beyondmiles.cc) \\| 📸 @beyondmilescc",
        parse_mode="MarkdownV2",
        disable_web_page_preview=True,
    )


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.strava.auth import build_authorization_url, generate_oauth_state

    try:
        await _get_or_create_user(update)
        state = await generate_oauth_state(update.effective_user.id)
        auth_url = build_authorization_url(state)
    except Exception:
        logger.exception("cmd_connect: DB or Redis error")
        await update.message.reply_text(
            "Sorry, I couldn't generate your Strava link right now. "
            "Please try again in a moment."
        )
        return

    await update.message.reply_text(
        "Tap *Connect Strava* below to link your account\\.\n\n"
        "We request access to read all your activities \\(including private ones\\) "
        "so your stats and notifications are complete\\.",
        parse_mode="MarkdownV2",
        reply_markup=connect_strava_keyboard(auth_url),
    )


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚠️ This will unlink your Strava account\\.\n"
        "You'll stop receiving activity notifications until you /connect again\\.\n\n"
        "Are you sure?",
        parse_mode="MarkdownV2",
        reply_markup=confirm_keyboard(
            confirm_data="disconnect:confirm",
            cancel_data="disconnect:cancel",
        ),
    )


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Incremental sync — fetches only new activities since the last stored one."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(User.telegram_user_id == update.effective_user.id)
            )
            user = result.scalar_one_or_none()
    except Exception:
        logger.exception("cmd_sync: DB error")
        await update.message.reply_text(
            "Sorry, I couldn't reach the database right now. Please try again in a moment."
        )
        return

    if not user or not user.strava_athlete_id:
        await update.message.reply_text(
            "You haven't connected your Strava account yet\\. Use /connect to get started\\.",
            parse_mode="MarkdownV2",
        )
        return

    import asyncio
    from app.tasks import sync_user_activities
    asyncio.ensure_future(sync_user_activities(
        user_id=str(user.id),
        notify_telegram_id=update.effective_user.id,
    ))

    await update.message.reply_text(
        "⏳ *Sync started\\!*\n\n"
        "Fetching your latest Strava activities\\. I'll message you when it's done\\.\n\n"
        "_If your stats still look off after syncing, use /fullsync to rebuild your full history\\._",
        parse_mode="MarkdownV2",
    )


async def cmd_fullsync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force a complete re-fetch of the entire Strava activity history."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(User.telegram_user_id == update.effective_user.id)
            )
            user = result.scalar_one_or_none()
    except Exception:
        logger.exception("cmd_fullsync: DB error")
        await update.message.reply_text(
            "Sorry, I couldn't reach the database right now. Please try again in a moment."
        )
        return

    if not user or not user.strava_athlete_id:
        await update.message.reply_text(
            "You haven't connected your Strava account yet\\. Use /connect to get started\\.",
            parse_mode="MarkdownV2",
        )
        return

    import asyncio
    from app.tasks import sync_user_activities
    asyncio.ensure_future(sync_user_activities(
        user_id=str(user.id),
        full=True,
        notify_telegram_id=update.effective_user.id,
    ))

    await update.message.reply_text(
        "🔄 *Full sync started\\!*\n\n"
        "Re\\-fetching your *entire* Strava history and removing any activities "
        "you've deleted on Strava\\.\n\n"
        "This may take a minute or two for large accounts\\. "
        "I'll message you when it's done\\.",
        parse_mode="MarkdownV2",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == update.effective_user.id)
        )
        user = result.scalar_one_or_none()

    if not user or not user.strava_athlete_id:
        await update.message.reply_text(
            "You haven't connected your Strava account yet\\.\nUse /connect to get started\\.",
            parse_mode="MarkdownV2",
        )
        return

    await update.message.reply_text(
        "📊 *Stats*\n\nSelect the activity behind your progress:",
        parse_mode="Markdown",
        reply_markup=stats_sport_keyboard(),
    )


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _get_or_create_user(update)
    if not user.strava_athlete_id:
        await update.message.reply_text(
            "Connect your Strava account first with /connect\\.",
            parse_mode="MarkdownV2",
        )
        return
    await _send_goals_menu(update.message, update.effective_user.id)


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

        rows = await db.execute(
            select(
                User.telegram_first_name,
                User.strava_athlete_name,
                func.sum(Activity.distance_meters).label("total_m"),
            )
            .join(Activity, Activity.user_id == User.id)
            .where(Activity.activity_date >= month_start)
            .group_by(User.id, User.telegram_first_name, User.strava_athlete_name)
            .order_by(func.sum(Activity.distance_meters).desc())
            .limit(10)
        )
        entries = rows.all()

    if not entries:
        await update.message.reply_text(
            "🏆 No activity recorded this month yet\\.\nConnect Strava with /connect and get riding\\!",
            parse_mode="MarkdownV2",
        )
        return

    lines = ["🏆 *BMCC Leaderboard — This Month*\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (first_name, athlete_name, total_m) in enumerate(entries):
        medal = medals[i] if i < 3 else f"{i + 1}."
        name = athlete_name or first_name
        km = round((total_m or 0) / 1000, 1)
        lines.append(f"{medal} {name} — *{km} km*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔔 Notification preferences are managed at the group level\\.\n"
        "Ask a group admin to configure notifications in the group chat\\.",
        parse_mode="MarkdownV2",
    )


async def cmd_quote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f'💬 *"{_random_quote()}"*', parse_mode="Markdown")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel any in-progress goal entry or activity edit."""
    from app.redis_client import get_redis, key_activity_edit
    r = await get_redis()
    tg_id = update.effective_user.id
    # Try to cancel activity edit first
    if await r.delete(key_activity_edit(tg_id)):
        _users_with_draft.discard(tg_id)
        await update.message.reply_text("Activity update cancelled.")
        return
    # Then try goal draft
    if await r.delete(_draft_key(tg_id)):
        _users_with_draft.discard(tg_id)
        await update.message.reply_text("Goal entry cancelled. Use /goals anytime.")
        return
    await update.message.reply_text("Nothing to cancel.")


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Skip the description step in an activity edit."""
    from app.redis_client import get_redis, key_activity_edit
    r = await get_redis()
    tg_id = update.effective_user.id
    raw = await r.get(key_activity_edit(tg_id))
    if not raw:
        await update.message.reply_text("Nothing to skip.")
        return
    draft = _json.loads(raw)
    if draft.get("step") != "description":
        await update.message.reply_text("Nothing to skip at this step.")
        return
    await r.delete(key_activity_edit(tg_id))
    _users_with_draft.discard(tg_id)
    await _push_activity_update(update, draft["activity_id"], draft["name"], "")


# ---------------------------------------------------------------------------
# Goals — callback-driven sport selection + Redis-backed free-text entry
# ---------------------------------------------------------------------------
# Flow:
#   /goals  →  main menu keyboard
#   ➕ Add Goal  →  sport keyboard (stats-style layout)
#   sport chosen  →  bot sends NEW message asking for goal description (free text)
#                    draft stored in Redis: goal_draft:{tg_id} = JSON{sport, step}
#   user types goal (e.g. "100 km")  →  bot asks for count (e.g. "4")
#   user types count  →  bot asks for period (keyboard)
#   period chosen  →  saved, confirmation shown
# ---------------------------------------------------------------------------

import json as _json

_SPORT_TYPE_MAP = {"Ride Endurance": "RideEndurance"}

_SPORT_ACTIVITY_TYPES: dict[str, list[str]] = {
    "Ride":          ["Ride", "VirtualRide"],
    "RideEndurance": ["Ride", "VirtualRide"],
    "Run":           ["Run", "VirtualRun"],
    "Walk":          ["Walk", "Hike"],
    "Swim":          ["Swim", "OpenWaterSwim"],
}

def _parse_category_threshold(category: str) -> float:
    """Convert a stored category string to minimum metres for activity counting.

    Examples:
        "100 km"  → 100_000.0
        "1500 m"  → 1_500.0
        "21.1 km" → 21_100.0
    Falls back to 0 if unparseable so all activities of that type are counted.
    """
    try:
        parts = category.strip().split()
        val = float(parts[0].replace(",", "."))
        unit = parts[1].lower() if len(parts) > 1 else "km"
        return val * 1_000 if unit == "km" else val
    except (IndexError, ValueError):
        return 0.0

_GOAL_PERIODS = [
    "This Month",
    "This Quarter",
    "This Year",
    "First Half of Year",
    "Second Half of Year",
    "This Week",
]

_GOAL_DRAFT_TTL = 600  # seconds — draft expires after 10 min of inactivity

_SPORT_UNITS: dict[str, str] = {
    "Ride":           "km",
    "Ride Endurance": "km",
    "Run":            "km",
    "Walk":           "km",
    "Swim":           "m",
}


def _sport_unit(sport: str) -> str:
    return _SPORT_UNITS.get(sport, "km")


def _goals_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add Goal",    callback_data="goal:add"),
         InlineKeyboardButton("Delete Goal", callback_data="goal:delete_menu")],
        [InlineKeyboardButton("Goal Status", callback_data="goal:status"),
         InlineKeyboardButton("Exit",        callback_data="goal:exit")],
    ])


def _goal_sport_keyboard() -> InlineKeyboardMarkup:
    """Sport selector — mirrors the stats sport keyboard layout."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ride",           callback_data="goal:sport:Ride"),
         InlineKeyboardButton("Ride Endurance", callback_data="goal:sport:Ride Endurance")],
        [InlineKeyboardButton("Run",            callback_data="goal:sport:Run"),
         InlineKeyboardButton("Swim",           callback_data="goal:sport:Swim"),
         InlineKeyboardButton("Walk",           callback_data="goal:sport:Walk")],
        [InlineKeyboardButton("Back",           callback_data="goal:back"),
         InlineKeyboardButton("Exit",          callback_data="goal:exit")],
    ])


def _goal_period_keyboard(sport: str, category: str, count: str) -> InlineKeyboardMarkup:
    p = _GOAL_PERIODS
    enc = lambda period: f"goal:period:{sport}|{category}|{count}|{period}"  # noqa: E731
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(p[0], callback_data=enc(p[0])),
         InlineKeyboardButton(p[1], callback_data=enc(p[1]))],
        [InlineKeyboardButton(p[2], callback_data=enc(p[2])),
         InlineKeyboardButton(p[5], callback_data=enc(p[5]))],
        [InlineKeyboardButton(p[3], callback_data=enc(p[3]))],
        [InlineKeyboardButton(p[4], callback_data=enc(p[4]))],
        [InlineKeyboardButton("Cancel",   callback_data="goal:exit")],
    ])


def _goal_period_dates(period: str):
    """Return (start_date, end_date_inclusive) for display and DB queries.

    The end date returned is the *last day* of the period (inclusive) so it
    displays as e.g. 2026-01-01 → 2026-12-31.  The DB query uses
    ``activity_date < end_dt + 1 day`` (exclusive upper bound) to stay correct.
    """
    now = datetime.now(timezone.utc)
    y = now.year

    if period == "This Month":
        start = datetime(y, now.month, 1, tzinfo=timezone.utc)
        # First day of next month minus 1 day = last day of this month
        next_month = datetime(y + 1, 1, 1, tzinfo=timezone.utc) if now.month == 12 \
                     else datetime(y, now.month + 1, 1, tzinfo=timezone.utc)
        end = next_month - timedelta(days=1)

    elif period == "This Quarter":
        q_start_month = ((now.month - 1) // 3) * 3 + 1
        start = datetime(y, q_start_month, 1, tzinfo=timezone.utc)
        q_end_month = q_start_month + 3
        next_q = datetime(y + 1, 1, 1, tzinfo=timezone.utc) if q_end_month > 12 \
                 else datetime(y, q_end_month, 1, tzinfo=timezone.utc)
        end = next_q - timedelta(days=1)

    elif period == "This Year":
        start = datetime(y, 1, 1, tzinfo=timezone.utc)
        end   = datetime(y, 12, 31, tzinfo=timezone.utc)

    elif period == "First Half of Year":
        start = datetime(y, 1, 1, tzinfo=timezone.utc)
        end   = datetime(y, 6, 30, tzinfo=timezone.utc)

    elif period == "Second Half of Year":
        start = datetime(y, 7, 1, tzinfo=timezone.utc)
        end   = datetime(y, 12, 31, tzinfo=timezone.utc)

    else:  # This Week (Mon–Sun)
        start = (datetime(y, now.month, now.day, tzinfo=timezone.utc)
                 - timedelta(days=now.weekday()))
        end = start + timedelta(days=6)

    return start.date(), end.date()


def _format_goal_summary(sport_display: str, category: str, count: int,
                          period: str, start, end) -> str:
    lines = [
        "✅ *Goal saved!*\n",
        f"Sport:    *{sport_display}*",
        f"Goal:     *{category}*",
        f"Target:   *{count} time{'s' if count != 1 else ''}*",
        f"Period:   *{period}*",
        f"Window:   {start}  →  {end}",
    ]
    return "\n".join(lines)


# Redis draft helpers

def _draft_key(tg_id: int) -> str:
    return f"goal_draft:{tg_id}"


async def _save_draft(tg_id: int, data: dict) -> None:
    from app.redis_client import get_redis
    r = await get_redis()
    await r.set(_draft_key(tg_id), _json.dumps(data), ex=_GOAL_DRAFT_TTL)
    _users_with_draft.add(tg_id)


async def _load_draft(tg_id: int) -> dict | None:
    from app.redis_client import get_redis
    r = await get_redis()
    raw = await r.get(_draft_key(tg_id))
    if raw is None:
        _users_with_draft.discard(tg_id)
    return _json.loads(raw) if raw else None


async def _clear_draft(tg_id: int) -> None:
    from app.redis_client import get_redis
    r = await get_redis()
    await r.delete(_draft_key(tg_id))
    _users_with_draft.discard(tg_id)


async def _send_goals_menu(target, user_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.telegram_user_id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return
        goals_res = await db.execute(
            select(Goal).where(Goal.user_id == user.id, Goal.is_active == True)  # noqa: E712
        )
        count = len(goals_res.scalars().all())

    n = f"{count} Active Goal{'s' if count != 1 else ''}"
    text = f'🎯 _"A goal is a dream with a deadline."_\n\nYou have *{n}*.'

    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode="Markdown", reply_markup=_goals_main_keyboard())
    else:
        await target.reply_text(text, parse_mode="Markdown", reply_markup=_goals_main_keyboard())


async def _handle_goal_callbacks(query, data: str) -> None:
    """Route all goal: callback data."""
    tg_id = query.from_user.id

    if data == "goal:add":
        await _clear_draft(tg_id)
        await query.edit_message_text(
            '_"Setting goals is the first step in turning the invisible into the visible."_\n\n'
            "Choose a Sport:",
            parse_mode="Markdown",
            reply_markup=_goal_sport_keyboard(),
        )
        return

    if data in ("goal:back", "goal:exit"):
        await _clear_draft(tg_id)
        await _send_goals_menu(query, tg_id)
        return

    if data == "goal:delete_menu":
        await _show_delete_menu(query)
        return

    if data == "goal:status":
        await _show_goal_status(query)
        return

    if data == "goal:exit":
        await query.edit_message_text("Goals closed. Tap /goals anytime to return.")
        return

    # ── Sport chosen → ask for goal target as a number ─────────────────────
    if data.startswith("goal:sport:"):
        sport = data[len("goal:sport:"):]
        await _save_draft(tg_id, {"sport": sport, "step": "category"})
        await query.edit_message_text(
            f"Sport: *{sport}*",
            parse_mode="Markdown",
        )
        unit = _sport_unit(sport)
        examples = {
            "Run":            "`5`, `10`, `21.1`, `42.2`",
            "Walk":           "`2`, `5`, `10`, `21.1`",
            "Ride":           "`50`, `100`, `200`",
            "Ride Endurance": "`200`, `300`, `600`",
            "Swim":           "`500`, `1000`, `1500`, `3800`",
        }
        eg = examples.get(sport, "`100`")
        await query.message.reply_text(
            f"✏️ *What is your goal distance for {sport}?*\n\n"
            f"Enter a number in {unit} — e.g. {eg}\n\n"
            f"Type /cancel to abort.",
            parse_mode="Markdown",
        )
        return

    # ── Period chosen → save goal ──────────────────────────────────────────
    if data.startswith("goal:period:"):
        payload = data[len("goal:period:"):]
        parts = payload.split("|")
        if len(parts) < 4:
            await query.edit_message_text("Invalid goal data. Please try /goals again.")
            return

        sport_display = parts[0]
        category      = parts[1]
        count         = int(parts[2])
        period        = parts[3]
        sport_db      = _SPORT_TYPE_MAP.get(sport_display, sport_display)
        start, end    = _goal_period_dates(period)

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(User.telegram_user_id == tg_id)
            )
            user = result.scalar_one_or_none()
            if not user:
                await query.edit_message_text("User not found. Try /start first.")
                return

            goal = Goal(
                user_id=user.id,
                activity_type=sport_db,
                category=category,
                target_count=count,
                start_date=start,
                end_date=end,
            )
            db.add(goal)
            await db.commit()

        await _clear_draft(tg_id)
        await query.edit_message_text(
            _format_goal_summary(sport_display, category, count, period, start, end),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("My Goals", callback_data="goal:menu"),
            ]]),
        )
        return

    # ── Confirm delete ─────────────────────────────────────────────────────
    if data.startswith("goal:confirm_delete:"):
        goal_id = data[len("goal:confirm_delete:"):]
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Goal).where(Goal.id == _uuid_mod.UUID(goal_id))
            )
            goal = result.scalar_one_or_none()
            if goal:
                sport_label = ("Ride Endurance"
                               if goal.activity_type == "RideEndurance"
                               else goal.activity_type)
                goal.is_active = False
                await db.commit()
                await query.edit_message_text(
                    f"✅ *Goal deleted*\n\n"
                    f"Sport: *{sport_label}*\n"
                    f"Goal: *{goal.category}*\n"
                    f"Target: *{goal.target_count} times*\n\n"
                    f"Use /goals to manage your goals.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("My Goals", callback_data="goal:menu"),
                    ]]),
                )
            else:
                await query.edit_message_text("Goal not found.")
        return


# ── Free-text handler: receives goal description and count ─────────────────

async def _handle_goal_text_input(update: Update) -> bool:
    """Handle free-text input for the in-progress goal draft.

    Returns True if the message was consumed by the goal flow.
    """
    tg_id = update.effective_user.id
    text  = update.message.text.strip()

    if text.lower() == "/cancel":
        await _clear_draft(tg_id)
        await update.message.reply_text("Goal entry cancelled. Use /goals anytime.")
        return True

    draft = await _load_draft(tg_id)
    if not draft:
        return False

    step = draft.get("step")

    if step == "category":
        sport = draft.get("sport", "")
        unit  = _sport_unit(sport)
        try:
            val = float(text.replace(",", "."))
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"Please enter a positive number ({unit}) — e.g. *100* or *21.1*:",
                parse_mode="Markdown",
            )
            return True
        # Normalise: drop trailing .0 for whole numbers so "100.0 km" → "100 km"
        display_val = int(val) if val == int(val) else val
        category = f"{display_val} {unit}"
        draft["category"] = category
        draft["step"]     = "count"
        await _save_draft(tg_id, draft)
        await update.message.reply_text(
            f"Goal: *{category}*\n\n"
            f"How many times do you want to achieve this?\n"
            f"Enter a number — e.g. *4*\n\n"
            f"Type /cancel to abort.",
            parse_mode="Markdown",
        )
        return True

    if step == "count":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text(
                "Please enter a positive whole number — e.g. *4*:",
                parse_mode="Markdown",
            )
            return True

        draft["count"] = int(text)
        draft["step"]  = "period"
        await _save_draft(tg_id, draft)

        sport    = draft["sport"]
        category = draft["category"]
        count    = draft["count"]

        await update.message.reply_text(
            f"Sport: *{sport}*\n"
            f"Goal: *{category}*\n"
            f"Target: *{count} time{'s' if count != 1 else ''}*\n\n"
            f"Choose the time period:",
            parse_mode="Markdown",
            reply_markup=_goal_period_keyboard(sport, category, str(count)),
        )
        return True

    return False


async def _show_delete_menu(query) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == query.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await query.edit_message_text("User not found.")
            return

        goals_res = await db.execute(
            select(Goal).where(Goal.user_id == user.id, Goal.is_active == True)  # noqa: E712
        )
        goals = goals_res.scalars().all()

    if not goals:
        await query.edit_message_text(
            "You have no active goals to delete.",
            reply_markup=_goals_main_keyboard(),
        )
        return

    rows = [
        [InlineKeyboardButton(
            f"{'Ride Endurance' if g.activity_type == 'RideEndurance' else g.activity_type}"
            f" — {g.category} x{g.target_count} ({g.start_date} to {g.end_date})",
            callback_data=f"goal:confirm_delete:{g.id}",
        )]
        for g in goals
    ]
    rows.append([InlineKeyboardButton("Back", callback_data="goal:back")])
    await query.edit_message_text(
        "Tap a goal to delete it:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _show_goal_status(query) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == query.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await query.edit_message_text("User not found.")
            return

        goals_res = await db.execute(
            select(Goal).where(Goal.user_id == user.id, Goal.is_active == True)  # noqa: E712
        )
        goals = goals_res.scalars().all()

        if not goals:
            await query.edit_message_text(
                "You have no active goals. Use ➕ Add Goal to create one.",
                reply_markup=_goals_main_keyboard(),
            )
            return

        athlete_name = user.strava_athlete_name or user.telegram_first_name or "You"
        divider = "─" * 24
        lines = [
            f"*Goal Status for: {athlete_name}*",
            "",
            f'*"{_random_quote()}"*',
            "",
        ]

        for g in goals:
            start_dt = datetime(
                g.start_date.year, g.start_date.month, g.start_date.day, tzinfo=timezone.utc
            )
            # end_date is inclusive (last day of period); add 1 day for exclusive SQL upper bound
            end_dt = datetime(
                g.end_date.year, g.end_date.month, g.end_date.day, tzinfo=timezone.utc
            ) + timedelta(days=1)
            act_types = _SPORT_ACTIVITY_TYPES.get(g.activity_type, [g.activity_type])

            # Parse threshold from stored category string, e.g. "100 km" → 100_000 m
            threshold_m = _parse_category_threshold(g.category)

            count_result = await db.execute(
                select(func.count(Activity.id))
                .where(
                    and_(
                        Activity.user_id == user.id,
                        Activity.activity_type.in_(act_types),
                        Activity.activity_date >= start_dt,
                        Activity.activity_date < end_dt,
                        Activity.distance_meters >= threshold_m,
                    )
                )
            )
            achieved = count_result.scalar_one() or 0
            pct = min(100, round(achieved / g.target_count * 100))

            # Compact progress bar — 10 segments
            filled_segs = round(pct / 10)
            bar = "█" * filled_segs + "░" * (10 - filled_segs)

            sport_label = "Ride Endurance" if g.activity_type == "RideEndurance" else g.activity_type
            target_word = "time" if g.target_count == 1 else "times"
            lines.append(
                f"*{sport_label}* — {g.category}\n"
                f"🎯 {achieved}/{g.target_count} {target_word}\n"
                f"`{bar}` {pct}%\n"
                f"_{g.start_date} → {g.end_date}_"
            )
            lines.append(divider)

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_goals_main_keyboard(),
    )


# ---------------------------------------------------------------------------
# General callback query handler
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # Activity edit
    if data.startswith("activity:edit:"):
        await _handle_activity_edit_start(query, data)
        return

    # Goals
    if data == "goal:menu":
        await _send_goals_menu(query, query.from_user.id)
        return

    if data.startswith("goal:"):
        await _handle_goal_callbacks(query, data)
        return

    # Stats
    if data.startswith("stats:sport:"):
        sport = data.split(":")[-1]
        sport_labels = {
            "Ride": "Ride", "RideEndurance": "Ride Endurance",
            "Run": "Run", "Swim": "Swim", "Walk": "Walk",
        }
        label = sport_labels.get(sport, sport)
        await query.edit_message_text(
            f"📊 *{label} Stats*\n\nChoose a time period:",
            parse_mode="Markdown",
            reply_markup=stats_period_keyboard(sport),
        )

    elif data.startswith("stats:period:"):
        parts = data.split(":")
        sport = parts[2]
        time_frame = parts[3]
        await _send_stats(query, sport, time_frame)

    elif data == "stats:menu":
        await query.edit_message_text(
            "📊 *Stats*\n\nSelect the activity behind your progress:",
            parse_mode="Markdown",
            reply_markup=stats_sport_keyboard(),
        )

    elif data == "stats:exit":
        await query.edit_message_text("Stats closed. Use /stats anytime to check your numbers.")

    elif data == "quote:random":
        await query.edit_message_text(f'💬 *"{_random_quote()}"*', parse_mode="Markdown")

    elif data == "reconnect:strava":
        from app.strava.auth import build_authorization_url, generate_oauth_state
        state = await generate_oauth_state(query.from_user.id)
        auth_url = build_authorization_url(state)
        await query.edit_message_text(
            "Tap below to reconnect your Strava account:",
            reply_markup=connect_strava_keyboard(auth_url),
        )

    elif data == "disconnect:confirm":
        await _do_disconnect(query)

    elif data in ("disconnect:cancel", "cancel"):
        await query.edit_message_text(
            "Cancelled — your account is still connected.",
            reply_markup=main_menu_keyboard(),
        )

    else:
        logger.warning("Unhandled callback data: %s", data)


async def _send_stats(query, sport: str, time_frame: str) -> None:
    await query.edit_message_text("⏳ Calculating your stats...")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == query.from_user.id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await query.edit_message_text("Please /start first.")
            return

        # Auto-sync if the user has no activities in the DB at all.
        # This recovers gracefully when the DB was wiped, a new service was
        # deployed, or the user connected Strava but never ran /sync.
        activity_count_result = await db.execute(
            select(func.count(Activity.id)).where(Activity.user_id == user.id)
        )
        total_activities = activity_count_result.scalar_one() or 0

        if total_activities == 0 and user.strava_athlete_id:
            import asyncio
            from app.tasks import sync_user_activities
            asyncio.ensure_future(sync_user_activities(user_id=str(user.id), full=True))
            await query.edit_message_text(
                "⏳ No activity data found — syncing your Strava history now\\.\n\n"
                "This may take a minute\\. Please use /stats again in a moment\\.",
                parse_mode="MarkdownV2",
            )
            return

        try:
            stats = await calculate_stats(db, user.id, sport, time_frame)
        except Exception:
            logger.exception("calculate_stats failed for user=%s", user.id)
            await query.edit_message_text("Could not load your stats. Try again later.")
            return

    athlete_name = user.strava_athlete_name or user.telegram_first_name
    text = format_stats_message(stats, sport, time_frame, athlete_name)
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=stats_nav_keyboard(sport),
    )


async def _do_disconnect(query) -> None:
    from app.strava.auth import deauthorize
    from app.crypto import decrypt

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == query.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await query.edit_message_text("Account not found.")
            return

        # Revoke Strava access before nulling local tokens
        if user.strava_access_token:
            try:
                plaintext_token = decrypt(user.strava_access_token)
                await deauthorize(plaintext_token)
            except Exception as exc:
                logger.warning("Strava deauthorize failed (continuing): %s", exc)

        user.strava_access_token    = None
        user.strava_refresh_token   = None
        user.strava_token_expires_at = None
        user.strava_athlete_id      = None
        await db.commit()

    await query.edit_message_text(
        "✅ Your Strava account has been disconnected.\n"
        "Use /connect any time to re-link it."
    )


# ---------------------------------------------------------------------------
# Activity edit flow
# ---------------------------------------------------------------------------

_ACTIVITY_EDIT_TTL = 600  # 10 minutes


async def _handle_activity_edit_start(query, data: str) -> None:
    """Callback: user tapped 'Update Activity' on a notification."""
    activity_id = int(data.split(":")[-1])
    tg_id = query.from_user.id

    from app.redis_client import get_redis, key_activity_edit
    r = await get_redis()
    await r.set(
        key_activity_edit(tg_id),
        _json.dumps({"activity_id": activity_id, "step": "name"}),
        ex=_ACTIVITY_EDIT_TTL,
    )
    _users_with_draft.add(tg_id)   # mark in-process so handle_unknown skips Redis
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        "Enter the *Activity Name* for this activity:\n\n"
        "_Example: 100 Km Ride_\n\n"
        "Type /cancel to abort.",
        parse_mode="Markdown",
    )


async def _handle_activity_edit_text(update: Update) -> bool:
    """Handle free-text input for the activity name/description edit flow.

    Returns True if the message was consumed by this flow, False otherwise.
    Only hits Redis if this user is flagged in _users_with_draft.
    """
    tg_id = update.effective_user.id
    if tg_id not in _users_with_draft:
        return False

    from app.redis_client import get_redis, key_activity_edit
    from app.strava.auth import get_valid_access_token
    from app.strava.client import update_activity

    r = await get_redis()
    raw = await r.get(key_activity_edit(tg_id))
    if not raw:
        _users_with_draft.discard(tg_id)
        return False

    draft = _json.loads(raw)
    text  = update.message.text.strip()
    step  = draft.get("step")

    if step == "name":
        draft["name"] = text
        draft["step"] = "description"
        await r.set(key_activity_edit(tg_id), _json.dumps(draft), ex=_ACTIVITY_EDIT_TTL)
        await update.message.reply_text(
            "Got it! Now enter the *Activity Description*:\n\n"
            "_Example: It was great riding the Nandi BRM from Bangalore Randonneurs_\n\n"
            "Type /skip to leave the description unchanged, or /cancel to abort.",
            parse_mode="Markdown",
        )
        return True

    if step == "description":
        description = text
        await r.delete(key_activity_edit(tg_id))
        _users_with_draft.discard(tg_id)
        await _push_activity_update(update, draft["activity_id"], draft["name"], description)
        return True

    return False


async def _push_activity_update(
    update: Update,
    activity_id: int,
    name: str,
    description: str,
) -> None:
    """PUT the updated name+description to Strava and confirm to user."""
    from app.strava.auth import get_valid_access_token
    from app.strava.client import update_activity

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == update.effective_user.id)
        )
        user = result.scalar_one_or_none()
        if not user or not user.strava_athlete_id:
            await update.message.reply_text(
                "Could not update — Strava account not connected. Use /connect."
            )
            return
        try:
            access_token = await get_valid_access_token(db, user)
            await update_activity(
                access_token,
                activity_id=activity_id,
                name=name,
                description=description,
            )
            # Update local DB name too
            act_result = await db.execute(
                select(Activity).where(Activity.strava_activity_id == activity_id)
            )
            activity = act_result.scalar_one_or_none()
            if activity:
                activity.activity_name = name
            await db.commit()
        except Exception as exc:
            import httpx
            detail = ""
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f" (HTTP {exc.response.status_code}: {exc.response.text[:200]})"
            logger.error("Failed to update Strava activity %s: %s%s", activity_id, exc, detail)
            await update.message.reply_text(
                f"❌ Could not update the activity on Strava.{detail or ' Please try again later.'}"
            )
            return

    await update.message.reply_text(
        f"✅ *Activity updated on Strava!*\n\n"
        f"Name: *{name}*\n"
        f"Description: {description}",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Fallback handlers
# ---------------------------------------------------------------------------

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    tg_id = update.effective_user.id

    # Fast path: if no draft is in-flight for this user, skip ALL Redis calls.
    # _users_with_draft is an in-process set maintained by _save_draft /
    # _handle_activity_edit_start / clear helpers.  False negatives can happen
    # after a process restart (the set is empty), but that is fine — we do one
    # extra Redis GET per user on the first message post-restart, after which
    # the set is self-healing.
    if tg_id not in _users_with_draft:
        _is_numeric = False
        try:
            float(text.replace(",", "."))
            _is_numeric = True
        except ValueError:
            pass
        if _is_numeric:
            await update.message.reply_text(
                "Were you adding a goal? Your session may have expired. "
                "Type /goals to start again."
            )
        else:
            await update.message.reply_text("Use /help to see what I can do.")
        return

    # Draft is in-flight — check both flows (order matters: activity edit first)
    if await _handle_activity_edit_text(update):
        return

    if await _handle_goal_text_input(update):
        return

    # Draft flag was set but neither flow recognised the input (shouldn't happen
    # often).  Clear the stale flag and give a helpful nudge.
    _users_with_draft.discard(tg_id)
    await update.message.reply_text("Use /help to see what I can do.")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error for update %s", update, exc_info=context.error)
    # Tell the user something went wrong instead of leaving them hanging.
    if not isinstance(update, Update):
        return
    msg = (
        update.message
        or (update.callback_query and update.callback_query.message)
    )
    if msg:
        try:
            await msg.reply_text(
                "Something went wrong on my end. Please try again in a moment.\n"
                "If it keeps happening, the bot may be experiencing a service outage."
            )
        except Exception:
            pass  # don't let error-handler itself raise
