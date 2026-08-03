"""All Telegram command and callback handlers.

Goals flow design (Flexible Goal Engine — Phase 1)
---------------------------------------------------
Goals use a purely callback-driven flow (no ConversationHandler) so state is
never lost when the web dyno restarts. Unlike the old single-string
callback_data encoding, every answer collected along the way is written to
the Redis goal draft (goal_draft:{tg_id}, see _save_draft/_load_draft) —
callback_data itself only ever carries the *current* step's choice, kept
short and well under Telegram's 64-byte callback_data limit even as the
flow grew from 3 steps to 6:

  goal:sport:<sport>        → sport chosen. Sports with only one valid
                               metric (Yoga/Racket Sports/Strength Training
                               are duration-only) auto-skip step 2.
  goal:metric:<metric>      → distance | elevation | duration
  goal:mode:<mode>          → cumulative (total target) | frequency (per-
                               session threshold x count)
  (free text)               → target value, then — frequency mode only —
                               how many times
  goal:multiday:<yes|no>    → count every activity separately, or collapse
                               same-day activities to the day's best one
  goal:period:<period>      → period chosen → reads the full draft → saves
                               to DB

See app.utils.GOAL_SPORT_METRICS for the sport/metric compatibility matrix
and app.tasks.get_goal_progress for how (metric, aggregation,
allow_multiple_daily) combine at progress-calculation time.
"""
from __future__ import annotations

import logging
import pathlib
import random
import uuid as _uuid_mod
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
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
    post_dismiss_keyboard,
    recap_goal_prompt_keyboard,
    stats_nav_keyboard,
    stats_other_sport_keyboard,
    stats_period_keyboard,
    stats_sport_keyboard,
)
from app.utils import DURATION_BASED_SPORTS as _DURATION_BASED_SPORTS
from app.utils import GOAL_SPORT_METRICS as _GOAL_SPORT_METRICS
from app.utils import OTHER_ACTIVITY_SPORTS as _OTHER_ACTIVITY_SPORTS
from app.utils import SPORT_ACTIVITY_TYPES as _SPORT_ACTIVITY_TYPES
from app.utils import format_goal_number as _format_goal_number
from app.utils import format_kv_lines as _format_kv_lines
from app.utils import goal_metric_unit as _goal_metric_unit
from app.utils import goal_value_to_canonical as _goal_value_to_canonical
from app.utils import SEPARATOR as _SEPARATOR
from app.utils import escape_markdown_v2 as _escape_md

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
    app.add_handler(CommandHandler("duplicates",    cmd_duplicates,    filters=_priv))
    app.add_handler(CommandHandler("stats",         cmd_stats,         filters=_priv))
    app.add_handler(CommandHandler("goals",         cmd_goals,         filters=_priv))
    app.add_handler(CommandHandler("cancel",        cmd_cancel,        filters=_priv))
    app.add_handler(CommandHandler("skip",          cmd_skip,          filters=_priv))
    app.add_handler(CommandHandler("leaderboard",   cmd_leaderboard,   filters=_priv))
    app.add_handler(CommandHandler("notifications", cmd_notifications, filters=_priv))
    app.add_handler(CommandHandler("roastmode",     cmd_roastmode,     filters=_priv))
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


# Goal-flow-specific quotes (curated, on-topic for setting/pursuing goals) —
# shown on the goals menu, the Add Goal intro, and the Goal Status header,
# instead of one hardcoded line repeated every time.
_GOAL_QUOTES: list[tuple[str, str]] = [
    ("Setting goals is the first step in turning the invisible into the visible.", "Tony Robbins"),
    ("Our goals can only be reached through a vehicle of a plan, in which we must "
     "fervently believe, and upon which we must vigorously act. There is no other "
     "route to success.", "Pablo Picasso"),
    ("The secret of getting ahead is getting started.", "Mark Twain"),
    ("You are never too old to set another goal or to dream a new dream.", "C.S. Lewis"),
    ("Shoot for the moon. Even if you miss, you'll land among the stars.", "Les Brown"),
    ("If you set your goals ridiculously high and it's a failure, you will fail "
     "above everyone else's success.", "James Cameron"),
    ("A goal is a dream with a deadline.", "Napoleon Hill"),
    ("If you want to live a happy life, tie it to a goal, not to people or things.", "Albert Einstein"),
    ("Obstacles are those frightful things you see when you take your eyes off "
     "your goal.", "Henry Ford"),
    ("What you get by achieving your goals is not as important as what you "
     "become by achieving your goals.", "Zig Ziglar"),
    ("It always seems impossible until it's done.", "Nelson Mandela"),
    ("Disciplined execution of a well-defined goal always trumps brilliant "
     "intentions.", "Unknown"),
]


def _random_goal_quote() -> str:
    quote, author = random.choice(_GOAL_QUOTES)
    return f'*"{quote}" — {author}*'


def _inline_code_or_plain(text: str) -> str:
    """Wrap free-text user content (activity name/description) in a
    single-line inline code span for monospace display. Telegram inline
    code spans can't contain a literal newline, so multi-line descriptions
    fall back to plain text rather than risk a broken/rejected message.
    """
    if not text or "\n" in text:
        return text
    return f"`{text.replace('`', chr(39))}`"


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


_HELP_TEXT = (
    "*BMCC FitSquad — All Commands*\n\n"
    "🔗 *Strava*\n"
    "/connect — Link your Strava account\n"
    "/disconnect — Unlink your Strava account\n"
    "/sync — Fetch latest activities \\(fast, day\\-to\\-day use\\)\n"
    "/fullsync — Rebuild your full history \\(use only if stats look wrong\\)\n"
    "/duplicates — Check your history for possible duplicate uploads\n\n"
    "📊 *Stats \\& Goals*\n"
    "/stats — View activity stats by sport and time period\n"
    "/goals — Set, delete or check your fitness goals\n"
    "/recap — This month's stats so far, recapped\n"
    "/yearrecap — Preview your year in review so far\n\n"
    "🏆 *Group*\n"
    "/leaderboard — Monthly points leaderboard \\(multi\\-sport bonus included\\)\n\n"
    "💬 *Other*\n"
    "/quote — Random motivational quote\n"
    "/notifications — How activity notifications are managed\n"
    "/roastmode — Toggle roast/kudos activity notifications \\(on by default\\)\n"
    "/cancel — Cancel any in\\-progress action\n"
    "/skip — Skip the current step in an in\\-progress action\n"
    "/start — Welcome message and main menu\n"
    "/help — Show this list\n\n"
    "💡 *Tip:* New activities sync automatically when you save them on Strava\\. "
    "Use /sync only if a recent activity is missing\\.\n\n"
    "🌐 [www\\.beyondmiles\\.cc](http://www.beyondmiles.cc) \\| 📸 @beyondmilescc"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        _HELP_TEXT,
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


async def cmd_duplicates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scan the caller's own activity history for possible duplicate uploads.

    Matches the same fingerprint as the automatic live/backfill checks (same
    sport, start time within 2 min, duration within 10%), but user-triggered
    and scoped to just this athlete. Cheap — a single indexed query.
    """
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(User.telegram_user_id == update.effective_user.id)
            )
            user = result.scalar_one_or_none()
    except Exception:
        logger.exception("cmd_duplicates: DB error")
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

    await update.message.reply_text("🔍 Checking your activity history for duplicates...")

    from app.tasks import check_duplicates_for_user
    try:
        clusters = await check_duplicates_for_user(str(user.id))
    except Exception:
        logger.exception("cmd_duplicates: scan failed for user_id=%s", user.id)
        await update.message.reply_text(
            "Sorry, something went wrong while checking. Please try again in a moment."
        )
        return

    if not clusters:
        await update.message.reply_text(
            "✅ No possible duplicates found — your activity history looks clean!"
        )
        return

    n = len(clusters)
    noun = "issue" if n == 1 else "issues"
    await update.message.reply_text(
        f"⚠️ Found {n} possible duplicate {noun} in your history — details above 👆"
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
    """/leaderboard — a shared, month-wide aggregation across every member,
    so (unlike per-user data such as /stats or /recap) the exact same
    rendered text is correct for whoever asks. Cached for a few minutes:
    high payoff for negligible staleness, since several members often check
    it in quick succession after group activity."""
    now = datetime.now(timezone.utc)
    month_key = f"{now.year}-{now.month:02d}"

    from app.redis_client import _LEADERBOARD_CACHE_TTL_SECONDS, get_redis, key_leaderboard

    redis = await get_redis()
    cache_key = key_leaderboard(month_key)
    try:
        cached = await redis.get(cache_key)
    except Exception:
        cached = None
    if cached:
        await update.message.reply_text(cached, parse_mode="MarkdownV2")
        return

    text = await _build_leaderboard_text(now)

    try:
        await redis.set(cache_key, text, ex=_LEADERBOARD_CACHE_TTL_SECONDS)
    except Exception:
        logger.warning("leaderboard cache: failed to write cache for %s", cache_key)

    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def _build_leaderboard_text(now: datetime) -> str:
    """Compute the full /leaderboard message text for the calendar month
    containing *now*. Pulled out of cmd_leaderboard so the cache-miss path
    only touches the DB, never the Telegram Update object."""
    async with AsyncSessionLocal() as db:
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
        return "🏆 No activity recorded this month yet\\.\nConnect Strava with /connect and get riding\\!"

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
        return "🏆 No activity recorded this month yet\\.\nConnect Strava with /connect and get riding\\!"

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

    # A clearly demarcated, monospace-aligned legend. Telegram has no way to
    # shrink font size, so instead of one long "pts/30 min" suffix repeated
    # on every line (which wrapped on narrow phones), the unit is stated
    # once per group header and each line is just "Label : points" — much
    # shorter, no wrapping, still lines up in a straight column.
    per_km_lines = _format_kv_lines([
        ("Run", "10"), ("Swim", "40"), ("Hiking", "8"),
        ("Walk", "6"), ("Ride", "3"),
    ])
    per_30min_lines = _format_kv_lines([
        ("Racket Sports", "15"), ("Strength Training", "12"), ("Yoga", "5"),
    ])
    lines.append(
        f"{_SEPARATOR}\n\n"
        f"*Point Values \\(per km\\)*\n"
        f"{per_km_lines}\n\n"
        f"*Point Values \\(per 30 min\\)*\n"
        f"{per_30min_lines}\n\n"
        "_Multi\\-sport bonus: 2 sports \\+5% · 3 sports \\+10% · 4\\+ sports \\+15%_"
    )
    return "\n".join(lines)


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually preview the monthly recap for the current (in-progress)
    month, point-in-time — same model as /yearrecap. The scheduled version
    fires automatically at 21:00 IST on the last day of each month for
    every connected user, by which point "current month" and "completed
    month" are the same thing."""
    from app.stats.recap import get_or_build_recap
    from app.telegram.notifications import send_recap_message

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

        now = datetime.now(timezone.utc)
        try:
            text = await get_or_build_recap(db, user, now.year, now.month)
        except Exception:
            logger.exception("cmd_recap failed for telegram_id=%s", update.effective_user.id)
            await update.message.reply_text(
                "Sorry, I couldn't put your recap together just now. Please try again shortly."
            )
            return

    await send_recap_message(
        context.bot, update.effective_chat.id, text, reply_markup=recap_goal_prompt_keyboard(),
    )


async def cmd_yearrecap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually preview the yearly recap for the current (in-progress) year
    — useful to check the numbers before the scheduled version fires
    automatically at 21:00 IST on 31 December with the full year's data."""
    from app.stats.recap import get_or_build_yearly_recap
    from app.telegram.notifications import send_recap_message

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
            text = await get_or_build_yearly_recap(db, user, year)
        except Exception:
            logger.exception("cmd_yearrecap failed for telegram_id=%s", update.effective_user.id)
            await update.message.reply_text(
                "Sorry, I couldn't put your year in review together just now. Please try again shortly."
            )
            return

    await send_recap_message(
        context.bot, update.effective_chat.id, text, reply_markup=recap_goal_prompt_keyboard(),
    )


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔔 Notification preferences are managed at the group level\\.\n"
        "Ask a group admin to configure notifications in the group chat\\.",
        parse_mode="MarkdownV2",
    )


async def cmd_roastmode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle Roast Mode — on by default, swaps the activity notification's
    greeting for a contextual roast (short effort) or kudos (solid effort)
    line, for Ride/Run/Swim/Walk. Usage: /roastmode, /roastmode on, /roastmode off.
    """
    arg = (context.args[0].lower() if context.args else "").strip()

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(User.telegram_user_id == update.effective_user.id)
            )
            user = result.scalar_one_or_none()
            if user is None:
                await update.message.reply_text(
                    "You haven't connected your Strava account yet\\. Use /connect to get started\\.",
                    parse_mode="MarkdownV2",
                )
                return

            if arg in ("on", "enable"):
                user.roast_mode_enabled = True
            elif arg in ("off", "disable"):
                user.roast_mode_enabled = False
            elif arg:
                await update.message.reply_text("Usage: /roastmode on | off")
                return
            else:
                user.roast_mode_enabled = not user.roast_mode_enabled

            new_state = user.roast_mode_enabled
            await db.commit()
    except Exception:
        logger.exception("cmd_roastmode: DB error")
        await update.message.reply_text(
            "Sorry, I couldn't reach the database right now. Please try again in a moment."
        )
        return

    if new_state:
        await update.message.reply_text(
            "😏 Roast Mode is now *ON*\\.\n"
            "Fall short of a sport's threshold \\(Ride < 50 km, Run < 10 km, "
            "Swim < 1500 m, Walk < 5 km\\) and your next activity notification "
            "will roast you a little\\. Clear it and you'll get a kudos line instead\\.\n"
            "Turn it off anytime with /roastmode off\\.",
            parse_mode="MarkdownV2",
        )
    else:
        await update.message.reply_text(
            "🙂 Roast Mode is now *OFF*\\. Activity notifications will go back "
            "to the plain \"Nice one / Kudos to you / Amazing\\.\\.\\.\" greeting\\.\n"
            "Turn it back on anytime with /roastmode on\\.",
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
        await update.message.reply_text(
            "Activity update cancelled.", reply_markup=post_dismiss_keyboard(),
        )
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
#
# Progress querying lives once in app.tasks.get_goal_progress, metric- and
# aggregation-mode-aware — see its use in _show_goal_status below.

_GOAL_PERIODS = [
    "This Month",
    "This Quarter",
    "This Year",
    "First Half of Year",
    "Second Half of Year",
    "This Week",
]

_GOAL_DRAFT_TTL = 600  # seconds — draft expires after 10 min of inactivity

# Plain text — no emoji. This dict is looked up rather than typed as a
# literal next to InlineKeyboardButton(...), so a naive "grep for emoji next
# to InlineKeyboardButton" audit will miss it; keep it plain on purpose.
_METRIC_LABELS: dict[str, str] = {
    "distance": "Distance",
    "elevation": "Elevation",
    "duration": "Duration",
}

# Spelled-out unit words for free-text prompts — "kilometers (km)" reads far
# more clearly to a first-time user than a bare "(km)".
_UNIT_WORDS: dict[str, str] = {
    "km": "kilometers",
    "m": "meters",
    "hrs": "hours",
    "min": "minutes",
}

# The four sports only reachable via "Other Activities" — used to route a
# mid-flow Back tap to the right sport menu (core vs. other).
_OTHER_MENU_SPORTS = {"Yoga", "Racket Sports", "Hiking", "Strength Training"}

_SPORT_EMOJI: dict[str, str] = {
    "Ride": "🚴", "RideEndurance": "🚴",
    "Run": "🏃", "Walk": "🚶", "Swim": "🏊",
    "Hiking": "🥾", "Yoga": "🧘",
    "RacketSports": "🏸", "StrengthTraining": "🏋️",
}


# Standard padding widths for the whole goals flow — sized so every button
# fills its row instead of looking cramped around short labels. A 2-per-row
# button gets roughly double a 3-per-row button's share of the row, and a
# full-width (1-per-row) button gets the most breathing room of all.
_PAD_FULL  = 42  # 1 button spanning the whole row
_PAD_2COL  = 32  # 2 buttons sharing a row
_PAD_3COL  = 20  # 3 buttons sharing a row


# ---------------------------------------------------------------------------
# Step-progress ("Step X of Y") — computed per-draft, not hardcoded, since
# the metric step is skipped for duration-only sports and the count step is
# skipped in cumulative mode. "rectype" (One-time Period vs Repeating Goal)
# and "final" (the period or recurrence picker, depending on that choice)
# are always present, so the total never depends on which of those two is
# picked — only on what's skippable earlier in the flow.
# ---------------------------------------------------------------------------
_GOAL_STEP_ORDER = ["sport", "metric", "mode", "value", "count", "daily", "rectype", "final"]


def _goal_step_progress(draft: dict, step_key: str) -> str:
    sport = draft.get("sport")
    metrics = _GOAL_SPORT_METRICS.get(sport, ["distance", "elevation", "duration"]) if sport else ["distance", "elevation", "duration"]
    skip_metric = len(metrics) <= 1
    skip_count = draft.get("aggregation") == "cumulative"

    applicable = [
        s for s in _GOAL_STEP_ORDER
        if not (s == "metric" and skip_metric) and not (s == "count" and skip_count)
    ]
    total = len(applicable)
    current = applicable.index(step_key) + 1 if step_key in applicable else total
    return f"Step {current} of {total}"


def _goal_prev_step(draft: dict, current_step: str) -> str | None:
    """The step immediately before *current_step* in this draft's actual
    path through the flow (accounting for the same metric/count skips as
    _goal_step_progress), or None if current_step is the first step."""
    sport = draft.get("sport")
    metrics = _GOAL_SPORT_METRICS.get(sport, ["distance", "elevation", "duration"]) if sport else ["distance", "elevation", "duration"]
    skip_metric = len(metrics) <= 1
    skip_count = draft.get("aggregation") == "cumulative"

    applicable = [
        s for s in _GOAL_STEP_ORDER
        if not (s == "metric" and skip_metric) and not (s == "count" and skip_count)
    ]
    if current_step not in applicable:
        return None
    idx = applicable.index(current_step)
    return applicable[idx - 1] if idx > 0 else None


def _goals_main_keyboard() -> InlineKeyboardMarkup:
    """Grouped by how often each action is used, not a uniform grid: Add
    Goal and Goal Status are the two things most people tap regularly, so
    each gets its own full-width row; Delete Goal (rare) and Exit
    (non-action) are secondary and share the bottom row."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad("Add Goal", _PAD_FULL),    callback_data="goal:add")],
        [InlineKeyboardButton(_pad("Goal Status", _PAD_FULL), callback_data="goal:status")],
        [InlineKeyboardButton(_pad("Delete Goal", _PAD_2COL), callback_data="goal:delete_menu"),
         InlineKeyboardButton(_pad("Exit", _PAD_2COL),        callback_data="goal:exit")],
    ])


def _goal_sport_keyboard() -> InlineKeyboardMarkup:
    """Sport selector — mirrors the stats sport keyboard layout."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad("Ride", _PAD_2COL),           callback_data="goal:sport:Ride"),
         InlineKeyboardButton(_pad("Ride Endurance", _PAD_2COL), callback_data="goal:sport:Ride Endurance")],
        [InlineKeyboardButton(_pad("Run", _PAD_3COL),            callback_data="goal:sport:Run"),
         InlineKeyboardButton(_pad("Swim", _PAD_3COL),           callback_data="goal:sport:Swim"),
         InlineKeyboardButton(_pad("Walk", _PAD_3COL),           callback_data="goal:sport:Walk")],
        [InlineKeyboardButton(_pad("Other Activities", _PAD_FULL), callback_data="goal:other")],
        [InlineKeyboardButton(_pad("Back", _PAD_2COL),           callback_data="goal:back"),
         InlineKeyboardButton(_pad("Exit", _PAD_2COL),           callback_data="goal:exit")],
    ])


def _goal_other_sport_keyboard() -> InlineKeyboardMarkup:
    """Secondary sport menu for goal-setting on the non-core sports."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad("Yoga", _PAD_2COL),          callback_data="goal:sport:Yoga"),
         InlineKeyboardButton(_pad("Racket Sports", _PAD_2COL), callback_data="goal:sport:Racket Sports")],
        [InlineKeyboardButton(_pad("Hiking", _PAD_2COL),        callback_data="goal:sport:Hiking"),
         InlineKeyboardButton(_pad("Strength Training", _PAD_2COL), callback_data="goal:sport:Strength Training")],
        [InlineKeyboardButton(_pad("Back", _PAD_2COL),          callback_data="goal:sport_menu"),
         InlineKeyboardButton(_pad("Exit", _PAD_2COL),          callback_data="goal:exit")],
    ])


def _goal_metric_keyboard(sport: str) -> InlineKeyboardMarkup:
    """Metric selector, filtered to the sport's valid metrics (see
    GOAL_SPORT_METRICS). Only shown for sports with 2+ valid metrics —
    duration-only sports (Yoga/Racket Sports/Strength Training) never see
    this step at all (auto-skipped in the goal:sport: handler)."""
    metrics = _GOAL_SPORT_METRICS.get(sport, ["distance"])
    # 2 metrics -> 2-column width, 3 metrics -> 3-column width, so the row
    # is always filled regardless of how many valid metrics this sport has.
    width = _PAD_2COL if len(metrics) <= 2 else _PAD_3COL
    row = [
        InlineKeyboardButton(_pad(_METRIC_LABELS[m], width), callback_data=f"goal:metric:{m}")
        for m in metrics
    ]
    return InlineKeyboardMarkup([
        row,
        [InlineKeyboardButton(_pad("Back", _PAD_2COL), callback_data="goal:prev:sport"),
         InlineKeyboardButton(_pad("Exit", _PAD_2COL), callback_data="goal:exit")],
    ])


_GOAL_MODE_PROMPT = (
    "How should this goal be tracked?\n"
    "• *Cumulative Total* — add up every activity toward one big number "
    "(e.g. 1,000 km total)\n"
    "• *Session Count* — hit a per-activity target a set number of "
    "sessions (e.g. a 10 km run, 4 sessions)"
)


def _goal_mode_keyboard(draft: dict) -> InlineKeyboardMarkup:
    """Aggregation-mode selector: sum-over-period vs. per-session count."""
    back_target = _goal_prev_step(draft, "mode") or "sport"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad("Cumulative Total", _PAD_2COL), callback_data="goal:mode:cumulative"),
         InlineKeyboardButton(_pad("Session Count", _PAD_2COL), callback_data="goal:mode:frequency")],
        [InlineKeyboardButton(_pad("Back", _PAD_2COL), callback_data=f"goal:prev:{back_target}"),
         InlineKeyboardButton(_pad("Exit", _PAD_2COL), callback_data="goal:exit")],
    ])


def _goal_daily_keyboard(draft: dict) -> InlineKeyboardMarkup:
    """Daily multi-instance question — "Count All Activities" (the product
    default) vs. "Only Best Each Day" (collapse same-day activities to the
    day's best one)."""
    back_target = _goal_prev_step(draft, "daily") or "mode"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad("Count All Activities", _PAD_2COL), callback_data="goal:multiday:yes"),
         InlineKeyboardButton(_pad("Only Best Each Day", _PAD_2COL), callback_data="goal:multiday:no")],
        [InlineKeyboardButton(_pad("Back", _PAD_2COL), callback_data=f"goal:prev:{back_target}"),
         InlineKeyboardButton(_pad("Exit", _PAD_2COL), callback_data="goal:exit")],
    ])


def _goal_rectype_keyboard() -> InlineKeyboardMarkup:
    """Top-level choice between a one-off goal for a specific period and a
    goal that repeats independently every month/quarter across the year —
    asked before the period picker so recurrence reads as its own concept
    rather than a hidden sub-branch that only appears after picking "This
    Year" specifically."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad("One-time Period", _PAD_2COL), callback_data="goal:rectype:onetime"),
         InlineKeyboardButton(_pad("Repeating Goal", _PAD_2COL), callback_data="goal:rectype:repeating")],
        [InlineKeyboardButton(_pad("Back", _PAD_2COL), callback_data="goal:prev:daily"),
         InlineKeyboardButton(_pad("Exit", _PAD_2COL), callback_data="goal:exit")],
    ])


def _goal_period_keyboard() -> InlineKeyboardMarkup:
    """Period selector for a one-time goal. All prior answers (sport,
    metric, aggregation, value(s), allow_multiple_daily) already live in
    the Redis draft, so callback_data only needs to carry the period name
    itself. First/Second Half of Year share one row with shortened labels
    so neither wraps or overflows its half of the row."""
    p = _GOAL_PERIODS
    enc = lambda period: f"goal:period:{period}"  # noqa: E731
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad(p[0], _PAD_2COL), callback_data=enc(p[0])),
         InlineKeyboardButton(_pad(p[1], _PAD_2COL), callback_data=enc(p[1]))],
        [InlineKeyboardButton(_pad(p[2], _PAD_2COL), callback_data=enc(p[2])),
         InlineKeyboardButton(_pad(p[5], _PAD_2COL), callback_data=enc(p[5]))],
        [InlineKeyboardButton(_pad("First Half", _PAD_2COL), callback_data=enc(p[3])),
         InlineKeyboardButton(_pad("Second Half", _PAD_2COL), callback_data=enc(p[4]))],
        [InlineKeyboardButton(_pad("Back", _PAD_2COL), callback_data="goal:prev:rectype"),
         InlineKeyboardButton(_pad("Exit", _PAD_2COL), callback_data="goal:exit")],
    ])


def _goal_recurrence_keyboard() -> InlineKeyboardMarkup:
    """Shown for a "Repeating Goal" — the target always spans the full
    year (period is implicitly "This Year"), so this only asks how often
    it repeats within it. A one-off yearly total instead is reached via
    "One-time Period" → "This Year" in _goal_period_keyboard, not here."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_pad("Every Month", _PAD_2COL), callback_data="goal:recurrence:monthly"),
         InlineKeyboardButton(_pad("Every Quarter", _PAD_2COL), callback_data="goal:recurrence:quarterly")],
        [InlineKeyboardButton(_pad("Back", _PAD_2COL), callback_data="goal:prev:rectype"),
         InlineKeyboardButton(_pad("Exit", _PAD_2COL), callback_data="goal:exit")],
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


def _format_goal_date_range(start, end) -> str:
    """Compact human-readable window — "Aug 1 – Aug 31" (year appended only
    once, and only if either end falls outside the current year) instead
    of the verbose ISO "2026-08-01 → 2026-08-31"."""
    cur_year = datetime.now(timezone.utc).year
    start_str = start.strftime("%b %-d")
    end_str = end.strftime("%b %-d")
    if start.year != cur_year or end.year != cur_year:
        if start.year == end.year:
            return f"{start_str} – {end_str}, {end.year}"
        return f"{start_str}, {start.year} – {end_str}, {end.year}"
    return f"{start_str} – {end_str}"


def _format_goal_summary(sport_display: str, category: str, aggregation: str,
                          count: int, period: str, start, end,
                          recurrence: str = "none") -> str:
    lines = [
        "✅ *Goal saved!*\n",
        f"Sport:    *{sport_display}*",
        f"Goal:     *{category}*",
    ]
    if aggregation == "frequency":
        lines.append(f"Target:   *{count} session{'s' if count != 1 else ''}*")
    period_label = {
        "monthly": f"{period} (repeats every month)",
        "quarterly": f"{period} (repeats every quarter)",
    }.get(recurrence, period)
    lines += [
        f"Period:   *{period_label}*",
        f"Window:   {_format_goal_date_range(start, end)}",
    ]
    return "\n".join(lines)


def _format_goal_category(sport: str, metric: str, aggregation: str,
                           value: float, unit: str, recurrence: str = "none") -> str:
    """Build the display-cache "category" string from structured goal
    fields — e.g. "100 km" / "30 min" (frequency, unchanged from the
    pre-Phase-1 format) or "Total 5,000 km" / "Total 100,000 m elevation"
    (non-recurring cumulative) or "1,000 km/mo" (Phase 2 recurring
    cumulative — target_value is interpreted per sub-period, so "Total"
    would be misleading here). Recurring frequency goals keep the plain
    threshold string ("21.1 km") since the recurrence itself is already
    conveyed by the Period line in _format_goal_summary and the /goals
    status screen's per-sub-period breakdown."""
    display_val = _format_goal_number(value)
    suffix = " elevation" if metric == "elevation" else ""
    if aggregation == "cumulative":
        rec_suffix = {"monthly": "/mo", "quarterly": "/qtr"}.get(recurrence)
        if rec_suffix:
            return f"{display_val} {unit}{suffix}{rec_suffix}"
        return f"Total {display_val} {unit}{suffix}"
    return f"{display_val} {unit}{suffix}"


def _goal_value_examples(metric: str, aggregation: str, unit: str) -> str:
    """Example numbers shown alongside the free-text value prompt, tuned to
    the metric/aggregation combination so a cumulative yearly elevation
    target (tens of thousands of metres) doesn't show the same examples as
    a per-ride elevation threshold (hundreds of metres)."""
    if metric == "duration":
        return "`5`, `10`, `20`" if aggregation == "cumulative" else "`30`, `45`, `60`"
    if metric == "elevation":
        return "`10,000`, `50,000`, `100,000`" if aggregation == "cumulative" else "`500`, `1,000`"
    if unit == "m":  # Swim distance
        return "`10,000`, `50,000`" if aggregation == "cumulative" else "`500`, `1,000`, `1,500`, `3,800`"
    return "`150`, `1,000`, `5,000`" if aggregation == "cumulative" else "`50`, `100`, `200`"


def _draft_summary_text(draft: dict) -> str:
    """Short recap of the answers collected so far, shown above the final
    two steps (daily-instance question, period picker) so the user can
    double check what they're about to save."""
    sport = draft["sport"]
    metric = draft.get("metric", "distance")
    aggregation = draft.get("aggregation", "frequency")
    unit = _goal_metric_unit(sport, metric, aggregation)
    val_str = _format_goal_number(draft.get("value", 0))
    lines = [f"Sport: *{sport}*", f"Metric: *{metric.capitalize()}*"]
    if aggregation == "cumulative":
        lines.append(f"Target: *Total {val_str} {unit}*")
    else:
        count = draft.get("count", 1)
        lines.append(f"Per-session: *{val_str} {unit}*")
        lines.append(f"Target: *{count} session{'s' if count != 1 else ''}*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt builders — one function per step, each a pure function of the
# draft so far. Shared by the forward flow (_handle_goal_callbacks /
# _handle_goal_text_input) and the Back handler (goal:prev:<step>), so
# stepping back and re-answering a step always looks identical to reaching
# it the first time.
# ---------------------------------------------------------------------------

def _sport_intro_text() -> str:
    return f"{_random_goal_quote()}\n\nChoose a Sport:"


def _metric_prompt_text(draft: dict) -> str:
    return (
        f"*{_goal_step_progress(draft, 'metric')}*\n\n"
        f"Sport: *{draft['sport']}*\n\n"
        f"What do you want to measure?"
    )


def _mode_prompt_text(draft: dict) -> str:
    lines = [f"*{_goal_step_progress(draft, 'mode')}*", ""]
    lines.append(f"Sport: *{draft['sport']}*")
    if draft.get("metric"):
        lines.append(f"Metric: *{draft['metric'].capitalize()}*")
    lines.append("")
    lines.append(_GOAL_MODE_PROMPT)
    return "\n".join(lines)


def _value_prompt_text(draft: dict) -> str:
    sport, metric, mode = draft["sport"], draft["metric"], draft["aggregation"]
    unit = _goal_metric_unit(sport, metric, mode)
    unit_word = _UNIT_WORDS.get(unit, unit)
    if mode == "cumulative":
        prompt = f"✏️ *What's your total {metric} target for {sport} this period?*"
    else:
        noun = "session length" if metric == "duration" else metric
        prompt = f"✏️ *What is your per-session {noun} goal for {sport}?*"
    eg = _goal_value_examples(metric, mode, unit)
    return (
        f"*{_goal_step_progress(draft, 'value')}*\n\n"
        f"{prompt}\n\n"
        f"Enter a number in {unit_word} ({unit}) — e.g. {eg}\n\n"
        f"Type /back to go to the previous step, or /cancel to exit."
    )


def _count_prompt_text(draft: dict) -> str:
    val_str = _format_goal_number(draft.get("value", 0))
    unit = _goal_metric_unit(draft["sport"], draft["metric"], draft["aggregation"])
    return (
        f"*{_goal_step_progress(draft, 'count')}*\n\n"
        f"Per-session goal: *{val_str} {unit}*\n\n"
        f"How many sessions do you want to achieve this in?\n"
        f"Enter a whole number — e.g. *4*\n\n"
        f"Type /back to go to the previous step, or /cancel to exit."
    )


def _daily_prompt_text(draft: dict) -> str:
    sport = draft.get("sport", "this sport")
    return (
        f"{_draft_summary_text(draft)}\n\n"
        f"*{_goal_step_progress(draft, 'daily')}*\n\n"
        f"If you log more than one {sport} activity on the same day, should "
        f"they all count, or just the best one?\n"
        f"_(This only affects multiple {sport} activities on the same day — "
        f"any Runs, Walks, Rides, or other sports you also log still count "
        f"toward their own separate goals as usual.)_"
    )


def _rectype_prompt_text(draft: dict) -> str:
    return (
        f"{_draft_summary_text(draft)}\n\n"
        f"*{_goal_step_progress(draft, 'rectype')}*\n\n"
        "Is this a one-time goal for a specific period, or should it repeat "
        "independently every month or quarter throughout the year?"
    )


def _period_prompt_text(draft: dict) -> str:
    return (
        f"{_draft_summary_text(draft)}\n\n"
        f"*{_goal_step_progress(draft, 'final')}*\n\n"
        "Choose the time period:"
    )


def _recurrence_prompt_text(draft: dict) -> str:
    return (
        f"{_draft_summary_text(draft)}\n\n"
        f"*{_goal_step_progress(draft, 'final')}*\n\n"
        "Repeat this target every month, or every quarter?"
    )


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


async def _nearest_deadline_line(db, user: "User", goals: list) -> str | None:
    """One-line summary of whichever active goal's deadline is soonest —
    for a recurring goal that means the sub-period currently in progress
    (e.g. this month), not the year-long window as a whole, since that's
    the deadline that's actually approaching."""
    from app.tasks import get_recurring_goal_progress

    today = datetime.now(timezone.utc).date()
    best_deadline = None
    best_goal = None
    for g in goals:
        if g.recurrence in ("monthly", "quarterly"):
            recurring = await get_recurring_goal_progress(db, user, g)
            current_sp = next((sp for sp in recurring.sub_periods if sp.status == "in_progress"), None)
            deadline = current_sp.end if current_sp else g.end_date
        else:
            deadline = g.end_date
        if deadline < today:
            continue
        if best_deadline is None or deadline < best_deadline:
            best_deadline, best_goal = deadline, g

    if best_goal is None:
        return None

    sport_label = _sport_display_label(best_goal.activity_type)
    emoji = _SPORT_EMOJI.get(best_goal.activity_type, "🏅")
    days_left = (best_deadline - today).days
    when = "today" if days_left == 0 else ("tomorrow" if days_left == 1 else f"in {days_left} days")
    return f"⏰ Closest deadline: {emoji} *{sport_label}* {best_goal.category} — ends {when} ({best_deadline})"


async def _send_goals_menu(target, user_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.telegram_user_id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return
        goals_res = await db.execute(
            select(Goal).where(Goal.user_id == user.id, Goal.is_active == True)  # noqa: E712
        )
        goals = goals_res.scalars().all()
        deadline_line = await _nearest_deadline_line(db, user, goals) if goals else None

    count = len(goals)
    quote = _random_goal_quote()
    if count == 0:
        text = f"🎯 {quote}\n\nYou have no active goals yet — tap *Add Goal* to set one."
    else:
        n = f"{count} active goal{'s' if count != 1 else ''}"
        text = f"🎯 {quote}\n\nYou have *{n}*."
        if deadline_line:
            text += f"\n\n{deadline_line}"

    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode="Markdown", reply_markup=_goals_main_keyboard())
    else:
        await target.reply_text(text, parse_mode="Markdown", reply_markup=_goals_main_keyboard())


async def _save_goal_and_confirm(query, tg_id: int, draft: dict, period: str, recurrence: str) -> None:
    """Shared save-to-DB + confirmation-message logic for both goal-creation
    paths that actually reach a save: the direct path (any period other
    than "This Year", recurrence="none") and the Phase 2 recurrence-picker
    path (This Year, recurrence in {"none", "monthly", "quarterly"})."""
    sport_display = draft["sport"]
    metric        = draft["metric"]
    aggregation   = draft["aggregation"]
    value         = draft["value"]
    count         = draft.get("count", 1)
    allow_multi   = draft.get("allow_multiple_daily", True)
    sport_db      = _SPORT_TYPE_MAP.get(sport_display, sport_display)
    start, end    = _goal_period_dates(period)

    unit = _goal_metric_unit(sport_display, metric, aggregation)
    target_value = _goal_value_to_canonical(value, unit)
    category = _format_goal_category(sport_display, metric, aggregation, value, unit, recurrence)

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
            metric=metric,
            aggregation=aggregation,
            target_value=target_value,
            target_count=count,
            allow_multiple_daily=allow_multi,
            recurrence=recurrence,
            start_date=start,
            end_date=end,
        )
        db.add(goal)
        await db.commit()

    await _clear_draft(tg_id)
    await query.edit_message_text(
        _format_goal_summary(sport_display, category, aggregation, count, period, start, end, recurrence),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(_pad("My Goals", 42), callback_data="goal:menu"),
        ]]),
    )


async def _handle_goal_callbacks(query, data: str) -> None:
    """Route all goal: callback data."""
    tg_id = query.from_user.id

    if data == "goal:add":
        await _clear_draft(tg_id)
        await query.edit_message_text(
            _sport_intro_text(),
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
            _sport_intro_text(),
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

    if data.startswith("goal:detail:"):
        await _show_goal_detail(query, data[len("goal:detail:"):])
        return

    if data == "goal:exit":
        await _clear_draft(tg_id)
        await query.edit_message_text("Goals closed. Tap /goals anytime to return.")
        return

    # ── True step-back: re-render the target step's own screen from the
    #    draft as it stands, rather than wiping the whole flow like
    #    goal:exit does. Picking sport again naturally resets anything
    #    downstream (see goal:sport: below), so no manual field cleanup is
    #    needed for the other targets — stale fields get overwritten as the
    #    user retraces their steps forward again. ────────────────────────
    if data.startswith("goal:prev:"):
        target = data[len("goal:prev:"):]
        draft = await _load_draft(tg_id) or {}
        draft["step"] = target
        await _save_draft(tg_id, draft)

        if target == "sport":
            if draft.get("sport_menu") == "other":
                await query.edit_message_text("Choose an activity:", reply_markup=_goal_other_sport_keyboard())
            else:
                await query.edit_message_text(_sport_intro_text(), parse_mode="Markdown", reply_markup=_goal_sport_keyboard())
        elif target == "metric":
            await query.edit_message_text(
                _metric_prompt_text(draft), parse_mode="Markdown",
                reply_markup=_goal_metric_keyboard(draft["sport"]),
            )
        elif target == "mode":
            await query.edit_message_text(
                _mode_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_mode_keyboard(draft),
            )
        elif target == "value":
            await query.edit_message_text(_value_prompt_text(draft), parse_mode="Markdown")
        elif target == "count":
            await query.edit_message_text(_count_prompt_text(draft), parse_mode="Markdown")
        elif target == "daily":
            await query.edit_message_text(
                _daily_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_daily_keyboard(draft),
            )
        elif target == "rectype":
            await query.edit_message_text(
                _rectype_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_rectype_keyboard(),
            )
        return

    # ── Sport chosen → metric picker (or auto-skip straight to mode picker
    #    for duration-only sports, which only have one valid metric) ───────
    if data.startswith("goal:sport:"):
        sport = data[len("goal:sport:"):]
        sport_menu = "other" if sport in _OTHER_MENU_SPORTS else "core"
        metrics = _GOAL_SPORT_METRICS.get(sport, ["distance"])
        if len(metrics) == 1:
            draft = {"sport": sport, "sport_menu": sport_menu, "metric": metrics[0], "step": "mode"}
            await _save_draft(tg_id, draft)
            await query.edit_message_text(
                _mode_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_mode_keyboard(draft),
            )
        else:
            draft = {"sport": sport, "sport_menu": sport_menu, "step": "metric"}
            await _save_draft(tg_id, draft)
            await query.edit_message_text(
                _metric_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_metric_keyboard(sport),
            )
        return

    # ── Metric chosen → aggregation-mode picker ─────────────────────────────
    if data.startswith("goal:metric:"):
        metric = data[len("goal:metric:"):]
        draft = await _load_draft(tg_id)
        if not draft:
            await query.edit_message_text("Session expired. Please try /goals again.")
            return
        draft["metric"] = metric
        draft["step"] = "mode"
        await _save_draft(tg_id, draft)
        await query.edit_message_text(
            _mode_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_mode_keyboard(draft),
        )
        return

    # ── Mode chosen → prompt for the target value as free text ─────────────
    if data.startswith("goal:mode:"):
        mode = data[len("goal:mode:"):]
        draft = await _load_draft(tg_id)
        if not draft:
            await query.edit_message_text("Session expired. Please try /goals again.")
            return
        draft["aggregation"] = mode
        draft["step"] = "value"
        await _save_draft(tg_id, draft)
        await query.edit_message_text(_value_prompt_text(draft), parse_mode="Markdown")
        return

    # ── Daily multi-instance answer → recurrence-type picker ────────────────
    if data.startswith("goal:multiday:"):
        answer = data[len("goal:multiday:"):]
        draft = await _load_draft(tg_id)
        if not draft:
            await query.edit_message_text("Session expired. Please try /goals again.")
            return
        draft["allow_multiple_daily"] = (answer == "yes")
        draft["step"] = "rectype"
        await _save_draft(tg_id, draft)
        await query.edit_message_text(
            _rectype_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_rectype_keyboard(),
        )
        return

    # ── One-time vs Repeating chosen — the top-level fork. One-time goes to
    #    the period picker (any period, recurrence="none"); Repeating skips
    #    the period picker entirely (always the full year) and goes straight
    #    to "every month or every quarter?". ─────────────────────────────────
    if data.startswith("goal:rectype:"):
        rectype = data[len("goal:rectype:"):]
        draft = await _load_draft(tg_id)
        if not draft:
            await query.edit_message_text("Session expired. Please try /goals again.")
            return
        draft["rectype"] = rectype
        draft["step"] = "final"
        await _save_draft(tg_id, draft)
        if rectype == "repeating":
            await query.edit_message_text(
                _recurrence_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_recurrence_keyboard(),
            )
        else:
            await query.edit_message_text(
                _period_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_period_keyboard(),
            )
        return

    # ── Period chosen (one-time path) → save goal ───────────────────────────
    if data.startswith("goal:period:"):
        period = data[len("goal:period:"):]
        draft = await _load_draft(tg_id)
        if not draft or "sport" not in draft or "value" not in draft:
            await query.edit_message_text("Session expired. Please try /goals again.")
            return
        await _save_goal_and_confirm(query, tg_id, draft, period, recurrence="none")
        return

    # ── Recurrence chosen (repeating path) → save goal, period is always
    #    the full year ─────────────────────────────────────────────────────
    if data.startswith("goal:recurrence:"):
        recurrence = data[len("goal:recurrence:"):]
        draft = await _load_draft(tg_id)
        if not draft or "sport" not in draft or "value" not in draft:
            await query.edit_message_text("Session expired. Please try /goals again.")
            return
        await _save_goal_and_confirm(query, tg_id, draft, "This Year", recurrence)
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
        target_line = "" if goal.aggregation == "cumulative" else (
            f"Target: *{goal.target_count} session{'s' if goal.target_count != 1 else ''}*\n"
        )
        await query.edit_message_text(
            f"Delete this goal?\n\n"
            f"Sport: *{sport_label}*\n"
            f"Goal: *{goal.category}*\n"
            f"{target_line}"
            f"Window: {_format_goal_date_range(goal.start_date, goal.end_date)}\n\n"
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
                target_line = "" if goal.aggregation == "cumulative" else (
                    f"Target: *{goal.target_count} session{'s' if goal.target_count != 1 else ''}*\n"
                )
                goal.is_active = False
                await db.commit()
                await query.edit_message_text(
                    f"✅ *Goal deleted*\n\n"
                    f"Sport: *{sport_label}*\n"
                    f"Goal: *{goal.category}*\n"
                    f"{target_line}\n"
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

    # ── True Back from a free-text step. Both text steps' predecessor is a
    #    button step (mode, or value itself), so this always re-sends a
    #    fresh message with the target step's own screen. ──────────────────
    if text.lower() == "/back" and step in ("value", "count"):
        prev = _goal_prev_step(draft, step) or "sport"
        draft["step"] = prev
        await _save_draft(tg_id, draft)
        if prev == "mode":
            await update.message.reply_text(
                _mode_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_mode_keyboard(draft),
            )
        elif prev == "value":
            await update.message.reply_text(_value_prompt_text(draft), parse_mode="Markdown")
        return True

    if step == "value":
        sport = draft.get("sport", "")
        metric = draft.get("metric", "distance")
        aggregation = draft.get("aggregation", "frequency")
        unit = _goal_metric_unit(sport, metric, aggregation)
        unit_word = _UNIT_WORDS.get(unit, unit)
        try:
            val = float(text.replace(",", "."))
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"Please enter a positive number in {unit_word} ({unit}) — e.g. *100* or *21.1*:",
                parse_mode="Markdown",
            )
            return True

        draft["value"] = val

        if aggregation == "cumulative":
            draft["step"] = "daily"
            await _save_draft(tg_id, draft)
            await update.message.reply_text(
                _daily_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_daily_keyboard(draft),
            )
        else:
            draft["step"] = "count"
            await _save_draft(tg_id, draft)
            await update.message.reply_text(_count_prompt_text(draft), parse_mode="Markdown")
        return True

    if step == "count":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text(
                "Please enter a positive whole number — e.g. *4*:",
                parse_mode="Markdown",
            )
            return True

        draft["count"] = int(text)
        draft["step"]  = "daily"
        await _save_draft(tg_id, draft)

        await update.message.reply_text(
            _daily_prompt_text(draft), parse_mode="Markdown", reply_markup=_goal_daily_keyboard(draft),
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

    def _delete_row_label(g: Goal) -> str:
        target = "" if g.aggregation == "cumulative" else f" x{g.target_count}"
        return (
            f"{_sport_display_label(g.activity_type)}"
            f" — {g.category}{target} ({g.start_date} to {g.end_date})"
        )

    rows = [
        [InlineKeyboardButton(
            _pad(_delete_row_label(g), 42),
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
            _random_goal_quote(),
            "",
        ]

        from app.tasks import format_goal_progress_value, get_goal_progress, get_recurring_goal_progress

        detail_rows = []

        for g in goals:
            sport_label = _sport_display_label(g.activity_type)
            emoji = _SPORT_EMOJI.get(g.activity_type, "🏅")

            if g.recurrence in ("monthly", "quarterly"):
                # Compact: one line for the tally, not a row per elapsed
                # sub-period — a year-long monthly goal would otherwise
                # print up to 12 rows here. Full month-by-month breakdown
                # moves to a "Details" tap (goal:detail:<id>) below.
                recurring = await get_recurring_goal_progress(db, user, g)

                if recurring.overall_status == "achieved":
                    banner = "🏆 Achieved!"
                elif recurring.overall_status == "failed":
                    first_missed = next(
                        (sp for sp in recurring.sub_periods if sp.status == "missed"), None
                    )
                    banner = f"💔 Failed — missed {first_missed.label.split()[0] if first_missed else '?'}"
                else:
                    banner = f"▶️ In progress — {recurring.met_count}/{recurring.elapsed_count} met"

                lines.append(
                    f"{emoji} *{sport_label} : {g.category}*\n\n"
                    f"{banner}\n"
                    f"_{_format_goal_date_range(g.start_date, g.end_date)}_"
                )
                lines.append(divider)
                detail_rows.append([InlineKeyboardButton(
                    _pad(f"Details: {sport_label} {g.category}", _PAD_FULL),
                    callback_data=f"goal:detail:{g.id}",
                )])
                continue

            progress = await get_goal_progress(db, user, g)
            pct = round(progress.pct)

            # Unicode progress bar — 10 segments. Plain characters (no code
            # span) so it renders as a bar, not a black/redacted-looking
            # monospace box.
            filled_segs = round(min(100, pct) / 10)
            bar = "▰" * filled_segs + "▱" * (10 - filled_segs)

            if progress.mode == "cumulative":
                progress_line = (
                    f"🎯 {format_goal_progress_value(g, progress.current)}"
                    f"/{format_goal_progress_value(g, progress.target)} "
                    f"{_goal_metric_unit(sport_label, g.metric, g.aggregation)} ({pct}%)"
                )
            else:
                session_word = "session" if g.target_count == 1 else "sessions"
                progress_line = f"🎯 {int(progress.current)}/{g.target_count} {session_word} ({pct}%)"
            lines.append(
                f"{emoji} *{sport_label} : {g.category}*\n\n"
                f"{progress_line}\n"
                f"{bar}\n"
                f"_{_format_goal_date_range(g.start_date, g.end_date)}_"
            )
            lines.append(divider)

    keyboard = InlineKeyboardMarkup(detail_rows + list(_goals_main_keyboard().inline_keyboard))
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def _show_goal_detail(query, goal_id: str) -> None:
    """Full month-by-month (or quarter-by-quarter) breakdown for a single
    recurring goal — the detail the compact status list intentionally
    leaves out to stay skimmable as the year progresses."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == query.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await query.edit_message_text("User not found.")
            return

        result = await db.execute(select(Goal).where(Goal.id == _uuid_mod.UUID(goal_id)))
        goal = result.scalar_one_or_none()
        if not goal or goal.user_id != user.id:
            await query.edit_message_text("Goal not found.", reply_markup=_goals_main_keyboard())
            return

        from app.tasks import get_recurring_goal_progress
        recurring = await get_recurring_goal_progress(db, user, goal)

    sport_label = _sport_display_label(goal.activity_type)
    emoji = _SPORT_EMOJI.get(goal.activity_type, "🏅")
    period_word = "month" if goal.recurrence == "monthly" else "quarter"

    sp_lines = []
    pending_count = 0
    for sp in recurring.sub_periods:
        if sp.status == "pending":
            pending_count += 1
            continue
        icon = {"met": "✅", "missed": "❌", "in_progress": "🔵"}[sp.status]
        sp_lines.append(f"{icon} {sp.label}")
    if pending_count:
        sp_lines.append(f"⏳ {pending_count} {period_word}{'s' if pending_count != 1 else ''} remaining")

    if recurring.overall_status == "achieved":
        banner = "🏆 Achieved!"
    elif recurring.overall_status == "failed":
        first_missed = next((sp for sp in recurring.sub_periods if sp.status == "missed"), None)
        banner = f"💔 Failed — missed {first_missed.label if first_missed else '?'}"
    else:
        banner = f"▶️ In progress — {recurring.met_count}/{recurring.elapsed_count} met"

    text = (
        f"{emoji} *{sport_label} : {goal.category}*\n\n"
        + "\n".join(sp_lines) + "\n\n"
        f"{banner}\n"
        f"_{_format_goal_date_range(goal.start_date, goal.end_date)}_"
    )
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(_pad("Back to Goal Status", _PAD_FULL), callback_data="goal:status")],
        ]),
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
        # Give the user an obvious next step instead of just ending the
        # interaction — swap the Update/Dismiss buttons for Stats/Goals/Help.
        try:
            await query.edit_message_reply_markup(reply_markup=post_dismiss_keyboard())
        except Exception:
            pass
        return

    if data == "postact:stats":
        await query.edit_message_text(
            "📊 *Stats*\n\nSelect the activity behind your progress:",
            parse_mode="Markdown",
            reply_markup=stats_sport_keyboard(),
        )
        return
    if data == "postact:goals":
        await _send_goals_menu(query, query.from_user.id)
        return
    if data == "postact:help":
        await query.edit_message_text(
            _HELP_TEXT, parse_mode="MarkdownV2", disable_web_page_preview=True,
        )
        return

    if data.startswith("activity:edit:"):
        await _handle_activity_edit_start(query, data)
        return

    if data == "recap:dismiss":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            "👍 No worries — here's your menu whenever you need it.",
            reply_markup=nav_keyboard(),
        )
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
    await query.message.reply_text(
        "Activity update cancelled.", reply_markup=post_dismiss_keyboard(),
    )


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

    desc_value = description if description else "(unchanged)"
    await reply_message.reply_text(
        f"✅ *Activity updated on Strava!*\n\n"
        f"Name: {_inline_code_or_plain(name)}\n"
        f"Description: {_inline_code_or_plain(desc_value)}",
        parse_mode="Markdown",
        reply_markup=post_dismiss_keyboard(),
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

    # Invalidate the in-process notification group-chat cache (app/tasks.py)
    # so the next activity notification doesn't broadcast to a stale list.
    import app.tasks as _tasks
    _tasks._group_chats_cache = None


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
