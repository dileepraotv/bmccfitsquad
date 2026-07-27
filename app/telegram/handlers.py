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

from sqlalchemy import and_, case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.database import AsyncSessionLocal
from app.models import Activity, Goal, GroupChat, User
from app.stats.calculator import calculate_stats, format_stats_message
from app.telegram.keyboards import (
    NAV_GOALS,
    NAV_HELP,
    NAV_STATS,
    _padded as _pad,
    activity_edit_description_keyboard,
    confirm_keyboard,
    connect_strava_keyboard,
    nav_keyboard,
    recap_goal_prompt_keyboard,
    stats_nav_keyboard,
    stats_other_sport_keyboard,
    stats_period_keyboard,
    stats_sport_keyboard,
)
from app.utils import DURATION_BASED_SPORTS as _DURATION_BASED_SPORTS
from app.utils import OTHER_ACTIVITY_SPORTS as _OTHER_ACTIVITY_SPORTS
from app.utils import SPORT_ACTIVITY_TYPES as _SPORT_ACTIVITY_TYPES
from app.utils import format_kv_lines as _format_kv_lines
from app.utils import SEPARATOR as _SEPARATOR

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
    app.add_handler(CommandHandler("recap",         cmd_recap,         filters=_priv))
    app.add_handler(CommandHandler("yearrecap",     cmd_yearrecap,     filters=_priv))

    app.add_handler(CallbackQueryHandler(handle_callback))

    # Auto-register group chats when the bot is added to a group
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

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
        # Returning connected user — single message, bottom nav only
        athlete_name = user.strava_athlete_name or name
        await update.message.reply_text(
            f"👋 Welcome back, *{_escape_md(name)}*\\!\n\n"
            f"Connected as *{_escape_md(athlete_name)}*\\.\n"
            f"Use the buttons below or /help for all commands\\.",
            parse_mode="MarkdownV2",
            reply_markup=nav_keyboard(),
        )
    else:
        # New user — compact welcome + quick-reference guide + Connect button
        state = await generate_oauth_state(update.effective_user.id)
        auth_url = build_authorization_url(state)
        await update.message.reply_text(
            "*Welcome to BMCC FitSquad\\!* 🚴🏃🏊🚶\n"
            "_\"It's the Ride That Matters\"_\n\n"
            "*Connect once, and I'll take it from there:* automatic activity "
            "notifications, always\\-current stats, and live goal progress — "
            "every time you log a ride, run, swim, or walk on Strava\\.\n\n"
            "Tap *Connect Strava* below to get started\\.\n\n"
            "*Once you're connected, here's what's available:*\n"
            "📊 /stats — Activity stats by sport and period\n"
            "🎯 /goals — Set and track distance goals\n"
            "🏆 /leaderboard — Monthly points leaderboard\n"
            "🔄 /sync — Pull latest activities from Strava\n"
            "💬 /help — Full command reference\n\n"
            "🌐 [www\\.beyondmiles\\.cc](http://www.beyondmiles.cc) \\| 📸 @beyondmilescc",
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
        "/goals — Set, delete or check your fitness goals\n"
        "/recap — Your most recently completed month, recapped\n"
        "/yearrecap — Preview your year in review so far\n\n"
        "🏆 *Group*\n"
        "/leaderboard — Monthly points leaderboard \\(multi\\-sport bonus included\\)\n\n"
        "💬 *Other*\n"
        "/quote — Random motivational quote\n"
        "/notifications — How activity notifications are managed\n"
        "/cancel — Cancel any in\\-progress action\n"
        "/skip — Skip the current step in an in\\-progress action\n"
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
        user = await _get_or_create_user(update)
    except Exception:
        logger.exception("cmd_connect: DB error")
        await update.message.reply_text(
            "Sorry, I couldn't reach the database right now. Please try again in a moment."
        )
        return

    # Already connected — don't repeat first-time setup copy, and don't
    # silently hand out a second OAuth link with no context.
    if user.strava_athlete_id:
        athlete_name = user.strava_athlete_name or "your Strava account"
        await update.message.reply_text(
            f"You're already connected as *{_escape_md(athlete_name)}*\\.\n\n"
            f"Use /disconnect first if you want to link a different Strava account\\.",
            parse_mode="MarkdownV2",
        )
        return

    try:
        state = await generate_oauth_state(update.effective_user.id)
        auth_url = build_authorization_url(state)
    except Exception:
        logger.exception("cmd_connect: Redis error")
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

    from app.tasks import fire_and_forget, sync_user_activities
    fire_and_forget(sync_user_activities(
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

    from app.tasks import fire_and_forget, sync_user_activities
    fire_and_forget(sync_user_activities(
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


# Point-based multiplier system: each sport's kilometre (or, for the
# duration-based "Other Activities" sports, each 30-minute block) is worth a
# different number of points — roughly proportional to average energy
# expenditure, with Run as the 1x baseline — plus a bonus for members who
# train across more sports rather than just one. The bonus caps out at the
# 4-sport tier: training a 5th+ sport keeps the max +15% rather than losing it.
_LEADERBOARD_SPORTS = [
    "Ride", "Run", "Walk", "Swim", "Hiking",
    "Yoga", "RacketSports", "StrengthTraining",
]
_POINTS_PER_KM = {"Run": 10, "Swim": 40, "Walk": 6, "Ride": 3, "Hiking": 8}
_POINTS_PER_30MIN = {"Yoga": 5, "RacketSports": 15, "StrengthTraining": 12}
_MULTI_SPORT_BONUS_PCT = {1: 0, 2: 5, 3: 10, 4: 15}
_LEADERBOARD_ICONS = {
    "Ride": "🚴", "Run": "🏃", "Walk": "🚶", "Swim": "🏊", "Hiking": "🥾",
    "Yoga": "🧘", "RacketSports": "🏸", "StrengthTraining": "🏋️",
}


def _leaderboard_metric_display(sport: str, value: float) -> str:
    """Format a per-sport leaderboard metric — km for distance sports,
    hours for duration-based sports (value is stored in 30-min blocks)."""
    if sport in _DURATION_BASED_SPORTS:
        return f"{value * 0.5:.1f}h"
    return f"{value:.0f} km"


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

        sport_sums = [
            func.sum(
                case(
                    (
                        Activity.activity_type.in_(_SPORT_ACTIVITY_TYPES[sport]),
                        Activity.moving_time_seconds if sport in _DURATION_BASED_SPORTS
                        else Activity.distance_meters,
                    ),
                    else_=0.0,
                )
            ).label(f"{sport.lower()}_v")
            for sport in _LEADERBOARD_SPORTS
        ]

        rows = await db.execute(
            select(User.telegram_first_name, User.strava_athlete_name, *sport_sums)
            .join(Activity, Activity.user_id == User.id)
            .where(Activity.activity_date >= month_start)
            .group_by(User.id, User.telegram_first_name, User.strava_athlete_name)
        )
        entries = rows.all()

    if not entries:
        await update.message.reply_text(
            "🏆 No activity recorded this month yet\\.\nConnect Strava with /connect and get riding\\!",
            parse_mode="MarkdownV2",
        )
        return

    board = []
    for first_name, athlete_name, *raw_values in entries:
        raw = dict(zip(_LEADERBOARD_SPORTS, raw_values))
        metrics = {}
        for sport in _LEADERBOARD_SPORTS:
            v = raw[sport] or 0
            # Duration sports: seconds → 30-minute blocks. Distance sports: metres → km.
            metrics[sport] = (v / 1800.0) if sport in _DURATION_BASED_SPORTS else (v / 1000.0)

        base_points = sum(
            metrics[s] * (_POINTS_PER_30MIN[s] if s in _DURATION_BASED_SPORTS else _POINTS_PER_KM[s])
            for s in _LEADERBOARD_SPORTS
        )
        sports_active = sum(1 for s in _LEADERBOARD_SPORTS if metrics[s] > 0)
        bonus_pct = _MULTI_SPORT_BONUS_PCT.get(min(sports_active, 4), 0)
        total_points = base_points * (1 + bonus_pct / 100)
        if total_points <= 0:
            continue
        board.append({
            "name": athlete_name or first_name,
            "metrics": metrics,
            "bonus_pct": bonus_pct,
            "total_points": total_points,
        })

    if not board:
        await update.message.reply_text(
            "🏆 No activity recorded this month yet\\.\nConnect Strava with /connect and get riding\\!",
            parse_mode="MarkdownV2",
        )
        return

    board.sort(key=lambda e: e["total_points"], reverse=True)
    board = board[:10]

    # A fixed-width monospace table overflows on narrow phone screens once
    # names or enough sports are involved — a stacked card per member reads
    # naturally at any screen width instead of forcing horizontal scroll.
    # The points/breakdown line is wrapped in a single `code` span so the
    # numbers render in the same monospace font as /stats and activity
    # notifications — consistent styling across the bot. Note: content
    # inside a code span only needs backtick/backslash escaped (not the
    # full MarkdownV2 set _escape_md applies), and everything here is
    # app-generated (emoji, digits, units) so no escaping is needed at all.
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 *BMCC Leaderboard — This Month*\n"]
    for i, e in enumerate(board):
        rank = medals[i] if i < 3 else f"{i + 1}\\."
        metrics = e["metrics"]
        breakdown = "  ·  ".join(
            f"{_LEADERBOARD_ICONS[s]} {_leaderboard_metric_display(s, metrics[s])}"
            for s in _LEADERBOARD_SPORTS if metrics[s] > 0
        )
        bonus_note = f"  (+{e['bonus_pct']}%)" if e["bonus_pct"] else ""
        lines.append(
            f"{rank} *{_escape_md(e['name'])}*\n"
            f"`{e['total_points']:.0f} pts{bonus_note}  ·  {breakdown}`\n"
        )

    # A clearly demarcated, monospace-aligned legend (one sport per line,
    # same `label : value` style as /stats and notifications) reads far
    # easier than the previous run-on sentence crammed into two lines.
    points_lines = _format_kv_lines([
        ("Run", "10 pts/km"),
        ("Swim", "40 pts/km"),
        ("Hiking", "8 pts/km"),
        ("Walk", "6 pts/km"),
        ("Ride", "3 pts/km"),
        ("Racket Sports", "15 pts/30 min"),
        ("Strength Training", "12 pts/30 min"),
        ("Yoga", "5 pts/30 min"),
    ])
    lines.append(
        f"{_SEPARATOR}\n\n"
        f"*Point Values*\n"
        f"{points_lines}\n\n"
        "_Multi\\-sport bonus: 2 sports \\+5% · 3 sports \\+10% · 4\\+ sports \\+15%_"
    )
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the monthly recap card for the most recently
    completed calendar month (the scheduled version fires automatically
    at 20:00 IST on the last day of each month for every connected user)."""
    from app.stats.recap import get_or_build_recap, most_recently_completed_month

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

        await update.message.reply_text("⏳ Building your recap...")

        year, month = most_recently_completed_month()
        try:
            image_bytes, caption = await get_or_build_recap(db, user, year, month)
        except Exception:
            logger.exception("cmd_recap failed for telegram_id=%s", update.effective_user.id)
            await update.message.reply_text(
                "Sorry, I couldn't put your recap together just now. Please try again shortly."
            )
            return

    await update.message.reply_photo(photo=image_bytes)
    await update.message.reply_text(caption, reply_markup=recap_goal_prompt_keyboard())


async def cmd_yearrecap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually preview the yearly recap card for the current (in-progress)
    year — useful to check the design before the scheduled version fires
    automatically at 20:00 IST on 31 December with the full year's data."""
    from app.stats.recap import get_or_build_yearly_recap

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

        await update.message.reply_text("⏳ Building your year in review...")

        year = datetime.now(timezone.utc).year
        try:
            image_bytes, caption = await get_or_build_yearly_recap(db, user, year)
        except Exception:
            logger.exception("cmd_yearrecap failed for telegram_id=%s", update.effective_user.id)
            await update.message.reply_text(
                "Sorry, I couldn't put your year in review together just now. Please try again shortly."
            )
            return

    await update.message.reply_photo(photo=image_bytes)
    await update.message.reply_text(caption, reply_markup=recap_goal_prompt_keyboard())


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
    await _push_activity_update(
        telegram_user_id=tg_id,
        reply_message=update.message,
        activity_id=draft["activity_id"],
        name=draft["name"],
        description="",
    )


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

_SPORT_TYPE_MAP = {
    "Ride Endurance":    "RideEndurance",
    "Racket Sports":     "RacketSports",
    "Strength Training": "StrengthTraining",
}
_SPORT_TYPE_MAP_REVERSE = {v: k for k, v in _SPORT_TYPE_MAP.items()}


def _sport_display_label(activity_type: str) -> str:
    """Reverse-map an internal sport key (e.g. "RacketSports") back to its
    space-cased display label (e.g. "Racket Sports") for goal messages."""
    return _SPORT_TYPE_MAP_REVERSE.get(activity_type, activity_type)

# Sport → Strava activity_type mapping now lives in app.utils.SPORT_ACTIVITY_TYPES
# (imported above as _SPORT_ACTIVITY_TYPES) so goal progress always counts the
# same activities as /stats and the notification goal-progress footer.

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


def _parse_duration_threshold_s(category: str) -> float:
    """Convert a stored duration category string like "30 min" to seconds.

    Used for the "Other Activities" sports (Yoga, Racket Sports, Strength
    Training) which have no meaningful GPS distance. Falls back to 0 if
    unparseable so all activities of that type are counted.
    """
    try:
        val = float(category.strip().split()[0].replace(",", "."))
        return val * 60
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
    "Ride":              "km",
    "Ride Endurance":    "km",
    "Run":               "km",
    "Walk":              "km",
    "Swim":              "m",
    "Hiking":            "km",
    "Yoga":              "min",
    "Racket Sports":     "min",
    "Strength Training": "min",
}


def _sport_unit(sport: str) -> str:
    return _SPORT_UNITS.get(sport, "km")


def _goals_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad("Add Goal", 21),    callback_data="goal:add"),
         InlineKeyboardButton(_pad("Delete Goal", 21), callback_data="goal:delete_menu")],
        [InlineKeyboardButton(_pad("Goal Status", 21), callback_data="goal:status"),
         InlineKeyboardButton(_pad("Exit", 21),        callback_data="goal:exit")],
    ])


def _goal_sport_keyboard() -> InlineKeyboardMarkup:
    """Sport selector — mirrors the stats sport keyboard layout."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad("Ride", 21),           callback_data="goal:sport:Ride"),
         InlineKeyboardButton(_pad("Ride Endurance", 21), callback_data="goal:sport:Ride Endurance")],
        [InlineKeyboardButton(_pad("Run"),                callback_data="goal:sport:Run"),
         InlineKeyboardButton(_pad("Swim"),                callback_data="goal:sport:Swim"),
         InlineKeyboardButton(_pad("Walk"),                callback_data="goal:sport:Walk")],
        [InlineKeyboardButton(_pad("Other Activities", 42), callback_data="goal:other")],
        [InlineKeyboardButton(_pad("Back", 21),           callback_data="goal:back"),
         InlineKeyboardButton(_pad("Exit", 21),           callback_data="goal:exit")],
    ])


def _goal_other_sport_keyboard() -> InlineKeyboardMarkup:
    """Secondary sport menu for goal-setting on the non-core sports."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad("Yoga", 21),          callback_data="goal:sport:Yoga"),
         InlineKeyboardButton(_pad("Racket Sports", 21), callback_data="goal:sport:Racket Sports")],
        [InlineKeyboardButton(_pad("Hiking", 21),        callback_data="goal:sport:Hiking"),
         InlineKeyboardButton(_pad("Strength Training", 21), callback_data="goal:sport:Strength Training")],
        [InlineKeyboardButton(_pad("Back", 21),          callback_data="goal:sport_menu"),
         InlineKeyboardButton(_pad("Exit", 21),          callback_data="goal:exit")],
    ])


def _goal_period_keyboard(sport: str, category: str, count: str) -> InlineKeyboardMarkup:
    p = _GOAL_PERIODS
    enc = lambda period: f"goal:period:{sport}|{category}|{count}|{period}"  # noqa: E731
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad(p[0], 21), callback_data=enc(p[0])),
         InlineKeyboardButton(_pad(p[1], 21), callback_data=enc(p[1]))],
        [InlineKeyboardButton(_pad(p[2], 21), callback_data=enc(p[2])),
         InlineKeyboardButton(_pad(p[5], 21), callback_data=enc(p[5]))],
        [InlineKeyboardButton(_pad(p[3], 42), callback_data=enc(p[3]))],
        [InlineKeyboardButton(_pad(p[4], 42), callback_data=enc(p[4]))],
        [InlineKeyboardButton(_pad("Cancel", 42),   callback_data="goal:exit")],
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

    if data == "goal:back":
        await _clear_draft(tg_id)
        await _send_goals_menu(query, tg_id)
        return

    if data == "goal:other":
        await query.edit_message_text(
            "Choose an activity:",
            reply_markup=_goal_other_sport_keyboard(),
        )
        return

    if data == "goal:sport_menu":
        await query.edit_message_text(
            '_"Setting goals is the first step in turning the invisible into the visible."_\n\n'
            "Choose a Sport:",
            parse_mode="Markdown",
            reply_markup=_goal_sport_keyboard(),
        )
        return

    if data == "goal:delete_menu":
        await _show_delete_menu(query)
        return

    if data == "goal:status":
        await _show_goal_status(query)
        return

    if data == "goal:exit":
        await _clear_draft(tg_id)
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
            "Run":               "`5`, `10`, `21.1`, `42.2`",
            "Walk":              "`2`, `5`, `10`, `21.1`",
            "Ride":              "`50`, `100`, `200`",
            "Ride Endurance":    "`200`, `300`, `600`",
            "Swim":              "`500`, `1000`, `1500`, `3800`",
            "Hiking":            "`2`, `5`, `10`, `21.1`",
            "Yoga":              "`30`, `45`, `60`",
            "Racket Sports":     "`30`, `45`, `60`",
            "Strength Training": "`30`, `45`, `60`",
        }
        eg = examples.get(sport, "`100`")
        goal_noun = "session length" if unit == "min" else "distance"
        await query.message.reply_text(
            f"✏️ *What is your goal {goal_noun} for {sport}?*\n\n"
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
                InlineKeyboardButton(_pad("My Goals", 42), callback_data="goal:menu"),
            ]]),
        )
        return

    # ── User tapped a goal in the delete list → show a confirmation screen
    #    before touching the database. Tapping the list previously deleted
    #    the goal immediately with no way back.
    if data.startswith("goal:delete_pick:"):
        goal_id = data[len("goal:delete_pick:"):]
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Goal).where(Goal.id == _uuid_mod.UUID(goal_id))
            )
            goal = result.scalar_one_or_none()

        if not goal:
            await query.edit_message_text("Goal not found.")
            return

        sport_label = _sport_display_label(goal.activity_type)
        target_word = "time" if goal.target_count == 1 else "times"
        await query.edit_message_text(
            f"Delete this goal?\n\n"
            f"Sport: *{sport_label}*\n"
            f"Goal: *{goal.category}*\n"
            f"Target: *{goal.target_count} {target_word}*\n"
            f"Window: {goal.start_date} → {goal.end_date}\n\n"
            f"This can't be undone.",
            parse_mode="Markdown",
            reply_markup=confirm_keyboard(
                confirm_data=f"goal:confirm_delete:{goal_id}",
                cancel_data="goal:delete_menu",
            ),
        )
        return

    # ── Confirmed — actually delete ─────────────────────────────────────────
    if data.startswith("goal:confirm_delete:"):
        goal_id = data[len("goal:confirm_delete:"):]
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Goal).where(Goal.id == _uuid_mod.UUID(goal_id))
            )
            goal = result.scalar_one_or_none()
            if goal:
                sport_label = _sport_display_label(goal.activity_type)
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
                        InlineKeyboardButton(_pad("My Goals", 42), callback_data="goal:menu"),
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
            _pad(
                f"{_sport_display_label(g.activity_type)}"
                f" — {g.category} x{g.target_count} ({g.start_date} to {g.end_date})",
                42,
            ),
            callback_data=f"goal:delete_pick:{g.id}",
        )]
        for g in goals
    ]
    rows.append([InlineKeyboardButton(_pad("Back", 42), callback_data="goal:back")])
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

            # Duration-based sports (Yoga, Racket Sports, Strength Training) have
            # no meaningful GPS distance — compare moving time instead of km/m.
            if g.activity_type in _DURATION_BASED_SPORTS:
                threshold_s = _parse_duration_threshold_s(g.category)
                metric_filter = Activity.moving_time_seconds >= threshold_s
            else:
                # Parse threshold from stored category string, e.g. "100 km" → 100_000 m
                threshold_m = _parse_category_threshold(g.category)
                metric_filter = Activity.distance_meters >= threshold_m

            count_result = await db.execute(
                select(func.count(Activity.id))
                .where(
                    and_(
                        Activity.user_id == user.id,
                        Activity.activity_type.in_(act_types),
                        Activity.activity_date >= start_dt,
                        Activity.activity_date < end_dt,
                        metric_filter,
                    )
                )
            )
            achieved = count_result.scalar_one() or 0
            pct = min(100, round(achieved / g.target_count * 100))

            # Compact progress bar — 10 segments
            filled_segs = round(pct / 10)
            bar = "█" * filled_segs + "░" * (10 - filled_segs)

            sport_label = _sport_display_label(g.activity_type)
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

    # Activity edit — description step (must be before activity:edit:<id>)
    if data == "activity:desc_skip":
        await _handle_activity_desc_skip(query)
        return
    if data == "activity:desc_cancel":
        await _handle_activity_desc_cancel(query)
        return
    if data == "activity:dismiss":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if data.startswith("activity:edit:"):
        await _handle_activity_edit_start(query, data)
        return

    if data == "recap:dismiss":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
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
            "Hiking": "Hiking", "Yoga": "Yoga",
            "RacketSports": "Racket Sports", "StrengthTraining": "Strength Training",
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

    elif data == "stats:other":
        await query.edit_message_text(
            "📊 *Other Activities*\n\nSelect the activity behind your progress:",
            parse_mode="Markdown",
            reply_markup=stats_other_sport_keyboard(),
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
        # No inline menu here — the persistent nav bar (Stats / Goals / Help)
        # is already on screen and is the one top-level destination menu.
        await query.edit_message_text("Cancelled — your account is still connected.")

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
            from app.tasks import fire_and_forget, sync_user_activities
            fire_and_forget(sync_user_activities(user_id=str(user.id), full=True))
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

    from app.strava.auth import build_authorization_url, generate_oauth_state
    from app.telegram.keyboards import connect_strava_keyboard
    try:
        state = await generate_oauth_state(query.from_user.id)
        auth_url = build_authorization_url(state)
        await query.edit_message_text(
            "✅ Strava disconnected\\.\n\n"
            "Ready to reconnect whenever you are\\.",
            parse_mode="MarkdownV2",
            reply_markup=connect_strava_keyboard(auth_url),
        )
    except Exception:
        await query.edit_message_text(
            "✅ Your Strava account has been disconnected.\n"
            "Use /connect any time to re-link it."
        )


# ---------------------------------------------------------------------------
# Activity edit flow
# ---------------------------------------------------------------------------

_ACTIVITY_EDIT_TTL = 600  # 10 minutes


async def _handle_activity_desc_skip(query) -> None:
    """Inline 'Skip description' — same as /skip during description step."""
    from app.redis_client import get_redis, key_activity_edit

    tg_id = query.from_user.id
    r = await get_redis()
    raw = await r.get(key_activity_edit(tg_id))
    if not raw:
        await query.message.reply_text("Nothing to skip.")
        return
    draft = _json.loads(raw)
    if draft.get("step") != "description":
        await query.message.reply_text("Nothing to skip at this step.")
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await r.delete(key_activity_edit(tg_id))
    _users_with_draft.discard(tg_id)
    await _push_activity_update(
        telegram_user_id=tg_id,
        reply_message=query.message,
        activity_id=draft["activity_id"],
        name=draft["name"],
        description="",
    )


async def _handle_activity_desc_cancel(query) -> None:
    """Inline 'Cancel' during activity edit (any step with a draft)."""
    from app.redis_client import get_redis, key_activity_edit

    tg_id = query.from_user.id
    r = await get_redis()
    if not await r.get(key_activity_edit(tg_id)):
        await query.message.reply_text("Nothing to cancel.")
        return
    await r.delete(key_activity_edit(tg_id))
    _users_with_draft.discard(tg_id)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text("Activity update cancelled.")


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
            "Use the buttons below, or type /skip or /cancel.",
            parse_mode="Markdown",
            reply_markup=activity_edit_description_keyboard(),
        )
        return True

    if step == "description":
        description = text
        await r.delete(key_activity_edit(tg_id))
        _users_with_draft.discard(tg_id)
        await _push_activity_update(
            telegram_user_id=tg_id,
            reply_message=update.message,
            activity_id=draft["activity_id"],
            name=draft["name"],
            description=description,
        )
        return True

    return False


async def _push_activity_update(
    *,
    telegram_user_id: int,
    reply_message: Message,
    activity_id: int,
    name: str,
    description: str,
) -> None:
    """PUT the updated name+description to Strava and confirm to user."""
    from app.strava.auth import get_valid_access_token
    from app.strava.client import update_activity

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        user = result.scalar_one_or_none()
        if not user or not user.strava_athlete_id:
            await reply_message.reply_text(
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
            await reply_message.reply_text(
                f"❌ Could not update the activity on Strava.{detail or ' Please try again later.'}"
            )
            return

    desc_display = description if description else "_(unchanged)_"
    await reply_message.reply_text(
        f"✅ *Activity updated on Strava!*\n\n"
        f"Name: *{name}*\n"
        f"Description: {desc_display}",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Group chat registration
# ---------------------------------------------------------------------------

async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-register or deactivate group chats as the bot is added/removed.

    Fires whenever the bot's own membership status changes in any chat.
    - Added to a group → upsert row in group_chats with notifications_enabled=True
    - Removed from a group → set notifications_enabled=False
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    member = update.my_chat_member
    if not member:
        return

    chat = member.chat
    # Only care about group and supergroup chats
    if chat.type not in ("group", "supergroup"):
        return

    new_status = member.new_chat_member.status  # "member", "administrator", "left", "kicked"
    is_active = new_status in ("member", "administrator")

    async with AsyncSessionLocal() as db:
        if is_active:
            stmt = (
                pg_insert(GroupChat)
                .values(
                    id=chat.id,
                    title=chat.title,
                    notifications_enabled=True,
                )
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={"title": chat.title, "notifications_enabled": True},
                )
            )
            await db.execute(stmt)
            await db.commit()
            logger.info("Group chat registered: chat_id=%s title=%r", chat.id, chat.title)
        else:
            result = await db.execute(
                select(GroupChat).where(GroupChat.id == chat.id)
            )
            group = result.scalar_one_or_none()
            if group:
                group.notifications_enabled = False
                await db.commit()
            logger.info("Group chat deactivated: chat_id=%s", chat.id)


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


# Small in-memory ring buffer of recent unhandled errors, surfaced via
# /ops/recent-errors — Telegram always sees 200 OK from our webhook (see
# telegram_webhook() in bot.py) even when a handler blows up and this fires,
# so without this buffer the failure is invisible outside Render's own log
# dashboard, which we don't have API access to from here.
_recent_errors: list[dict] = []
_RECENT_ERRORS_MAX = 25


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    import traceback

    logger.exception("Unhandled error for update %s", update, exc_info=context.error)
    _recent_errors.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "update": str(update)[:500],
        "error": "".join(
            traceback.format_exception(type(context.error), context.error, context.error.__traceback__)
        )[-3000:] if context.error else None,
    })
    del _recent_errors[:-_RECENT_ERRORS_MAX]

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


def get_recent_errors() -> list[dict]:
    """Public accessor for the /ops/recent-errors diagnostic endpoint."""
    return list(_recent_errors)
