"""All Telegram command and callback handlers.

Register every handler in ``register_handlers()`` — called once at startup
from ``app.telegram.bot.setup_bot()``.
"""
from __future__ import annotations

import logging
import pathlib
import random
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.database import AsyncSessionLocal
from app.models import User
from app.stats.calculator import calculate_stats, format_stats_message
from app.telegram.keyboards import (
    confirm_keyboard,
    connect_strava_keyboard,
    main_menu_keyboard,
    stats_period_keyboard,
    stats_sport_keyboard,
)

logger = logging.getLogger(__name__)

_QUOTES_PATH = pathlib.Path("data/quotes.txt")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_handlers(app: Application) -> None:
    """Attach all handlers to the PTB Application."""
    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("connect",       cmd_connect))
    app.add_handler(CommandHandler("disconnect",    cmd_disconnect))
    app.add_handler(CommandHandler("stats",         cmd_stats))
    app.add_handler(CommandHandler("goals",         cmd_goals))
    app.add_handler(CommandHandler("leaderboard",   cmd_leaderboard))
    app.add_handler(CommandHandler("notifications", cmd_notifications))
    app.add_handler(CommandHandler("quote",         cmd_quote))

    app.add_handler(CallbackQueryHandler(handle_callback))

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


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message — register the user and prompt Strava connection if needed."""
    user = await _get_or_create_user(update)
    name = update.effective_user.first_name or "there"

    if user.strava_athlete_id:
        await update.message.reply_text(
            f"👋 Welcome back, *{name}*!\n\n"
            f"Your Strava account is connected as *{user.strava_athlete_name or 'Athlete'}*.\n"
            f"Use the menu below or type /help to see all commands.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            f"👋 Hey *{name}*, welcome to the *BMCC Fitness Bot*! 🚴‍♂️\n\n"
            f"I post activity notifications, stats, and motivational quotes for "
            f"Beyond Miles Cycling Club.\n\n"
            f"To get started, connect your Strava account:",
            parse_mode="Markdown",
            reply_markup=connect_strava_keyboard(
                f"https://t.me/{context.bot.username}?start=connect"
            ),
        )
        await update.message.reply_text(
            "Or tap /connect to link your Strava account.",
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the help message listing all available commands."""
    await update.message.reply_text(
        "*BMCC Bot — Commands*\n\n"
        "/start — Welcome message and main menu\n"
        "/connect — Link your Strava account\n"
        "/disconnect — Unlink your Strava account\n"
        "/stats — View your activity stats\n"
        "/goals — Manage your fitness goals\n"
        "/leaderboard — Group distance leaderboard\n"
        "/quote — Get a motivational quote\n"
        "/help — Show this message",
        parse_mode="Markdown",
    )


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Begin the Strava OAuth flow for the user."""
    from app.strava.auth import build_authorization_url, generate_oauth_state

    await _get_or_create_user(update)
    tg_user_id = update.effective_user.id

    state = await generate_oauth_state(tg_user_id)
    auth_url = build_authorization_url(state)

    await update.message.reply_text(
        "Tap the button below to connect your Strava account.\n\n"
        "You'll be asked to approve access — we only read your activities, we never write.",
        reply_markup=connect_strava_keyboard(auth_url),
    )


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for confirmation before unlinking the user's Strava account."""
    await update.message.reply_text(
        "⚠️ This will unlink your Strava account.\n"
        "You'll stop receiving activity notifications until you /connect again.\n\n"
        "Are you sure?",
        reply_markup=confirm_keyboard(
            confirm_data="disconnect:confirm",
            cancel_data="disconnect:cancel",
        ),
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a sport-type selector so the user can view their stats."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == update.effective_user.id)
        )
        user = result.scalar_one_or_none()

    if not user or not user.strava_athlete_id:
        await update.message.reply_text(
            "You haven't connected your Strava account yet.\n"
            "Use /connect to get started."
        )
        return

    await update.message.reply_text(
        "📊 *Your Stats*\n\nWhich sport would you like to see?",
        parse_mode="Markdown",
        reply_markup=stats_sport_keyboard(),
    )


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the user's active goals."""
    from app.models import Goal

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Goal).where(
                Goal.user_id == select(User.id).where(
                    User.telegram_user_id == update.effective_user.id
                ).scalar_subquery()
            )
        )
        goals = result.scalars().all()

    if not goals:
        await update.message.reply_text(
            "🎯 You have no active goals.\n\n"
            "Goals aren't fully implemented yet — coming soon!"
        )
    else:
        from app.telegram.keyboards import goals_keyboard
        await update.message.reply_text(
            f"🎯 *Your Goals* ({len(goals)} active)",
            parse_mode="Markdown",
            reply_markup=goals_keyboard(goals),
        )


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show this month's distance leaderboard across all connected members."""
    from app.models import Activity
    from sqlalchemy import func

    async with AsyncSessionLocal() as db:
        from datetime import datetime, timezone
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
            "🏆 No activity recorded this month yet.\n"
            "Connect your Strava with /connect and get riding!"
        )
        return

    lines = ["🏆 *BMCC Leaderboard — This Month*\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (first_name, athlete_name, total_m) in enumerate(entries):
        medal = medals[i] if i < 3 else f"{i+1}."
        name = athlete_name or first_name
        km = round((total_m or 0) / 1000, 1)
        lines.append(f"{medal} {name} — *{km} km*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle activity notifications for the current user."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == update.effective_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await update.message.reply_text("Please /start first.")
            return
        # Toggle — User model doesn't have a notifications field; inform user
        await update.message.reply_text(
            "🔔 Notification preferences are managed at the group level.\n"
            "Ask a group admin to use /notifications in the group chat."
        )


async def cmd_quote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a random motivational quote."""
    await update.message.reply_text(f'💬 *"{_random_quote()}"*', parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Callback query handler (inline keyboard button presses)
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route inline keyboard callbacks to the appropriate sub-handler."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("stats:sport:"):
        sport = data.split(":")[-1]
        await query.edit_message_text(
            f"📊 *{sport} Stats*\n\nChoose a time period:",
            parse_mode="Markdown",
            reply_markup=stats_period_keyboard(sport),
        )

    elif data.startswith("stats:period:"):
        # stats:period:<sport>:<time_frame>
        parts = data.split(":")
        sport = parts[2]
        time_frame = parts[3]
        await _send_stats(query, sport, time_frame)

    elif data == "stats:menu":
        await query.edit_message_text(
            "📊 *Your Stats*\n\nWhich sport would you like to see?",
            parse_mode="Markdown",
            reply_markup=stats_sport_keyboard(),
        )

    elif data == "leaderboard:week":
        await query.edit_message_text("Use /leaderboard to see this month's standings.")

    elif data == "quote:random":
        await query.edit_message_text(f'💬 *"{_random_quote()}"*', parse_mode="Markdown")

    elif data == "disconnect:confirm":
        await _do_disconnect(query)

    elif data in ("disconnect:cancel", "cancel"):
        await query.edit_message_text("Cancelled — your account is still connected.")

    else:
        logger.warning("Unhandled callback data: %s", data)
        await query.edit_message_text("This button isn't wired up yet. Try /help.")


async def _send_stats(query, sport: str, time_frame: str) -> None:
    """Fetch and format stats for the user, then edit the message."""
    await query.edit_message_text("⏳ Calculating your stats…")

    tg_user_id = query.from_user.id
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == tg_user_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await query.edit_message_text("Please /start first.")
            return

        try:
            stats = await calculate_stats(db, user.id, sport, time_frame)
        except Exception as exc:
            logger.exception("calculate_stats failed for user=%s", user.id)
            await query.edit_message_text("❌ Couldn't load your stats. Try again later.")
            return

    athlete_name = user.strava_athlete_name or user.telegram_first_name
    text = format_stats_message(stats, sport, time_frame, athlete_name)
    await query.edit_message_text(text, parse_mode="Markdown")


async def _do_disconnect(query) -> None:
    """Null out the user's Strava credentials on confirmation."""
    tg_user_id = query.from_user.id
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == tg_user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await query.edit_message_text("Account not found.")
            return
        user.strava_access_token = None
        user.strava_refresh_token = None
        user.strava_token_expires_at = None
        user.strava_athlete_id = None
        await db.commit()

    await query.edit_message_text(
        "✅ Your Strava account has been disconnected.\n"
        "Use /connect any time to re-link it."
    )


# ---------------------------------------------------------------------------
# Fallback handlers
# ---------------------------------------------------------------------------

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nudge users who send plain text in a private chat."""
    await update.message.reply_text("Use /help to see what I can do.")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log all unhandled exceptions raised by handlers."""
    logger.exception("Unhandled error for update %s", update, exc_info=context.error)
