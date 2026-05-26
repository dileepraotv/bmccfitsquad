"""All Telegram command and callback handlers.

Register every handler in ``register_handlers()`` — called once at startup
from ``app.telegram.bot.setup_bot()``.

Goals conversation states
-------------------------
  GOAL_CHOOSE_ACTION  — top-level: Add / Delete / Status / Done
  GOAL_CHOOSE_SPORT   — which sport is the goal for?
  GOAL_CHOOSE_CATEGORY — which distance/achievement category?
  GOAL_ENTER_COUNT    — how many times in the period?
  GOAL_CHOOSE_PERIOD  — which time window (this month / this year / custom)?
"""
from __future__ import annotations

import logging
import pathlib
import random
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.database import AsyncSessionLocal
from app.models import Goal, User
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

# Conversation states
GOAL_CHOOSE_ACTION   = 0
GOAL_CHOOSE_SPORT    = 1
GOAL_CHOOSE_CATEGORY = 2
GOAL_ENTER_COUNT     = 3
GOAL_CHOOSE_PERIOD   = 4


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_handlers(app: Application) -> None:
    """Attach all handlers to the PTB Application."""
    # Goals conversation must be registered before the generic callback handler
    app.add_handler(_goals_conversation())

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("connect",       cmd_connect))
    app.add_handler(CommandHandler("disconnect",    cmd_disconnect))
    app.add_handler(CommandHandler("sync",          cmd_sync))
    app.add_handler(CommandHandler("stats",         cmd_stats))
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
    from app.strava.auth import build_authorization_url, generate_oauth_state

    user = await _get_or_create_user(update)
    name = update.effective_user.first_name or "there"

    if user.strava_athlete_id:
        await update.message.reply_text(
            f"👋 Welcome back, *{name}*\\!\n\n"
            f"Your Strava account is connected as *{user.strava_athlete_name or 'Athlete'}*\\.\n"
            f"Use the menu below or type /help to see all commands\\.",
            parse_mode="MarkdownV2",
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
    """Send the help message listing all available commands."""
    await update.message.reply_text(
        "*BMCC FitSquad — Available Commands*\n\n"
        "🔗 *Strava*\n"
        "/connect — Link your Strava account\n"
        "/disconnect — Unlink your Strava account\n"
        "/sync — Sync your Strava activity history\n\n"
        "📊 *Stats & Goals*\n"
        "/stats — View activity stats by sport and period\n"
        "/goals — Add, delete or check your fitness goals\n\n"
        "🏆 *Group*\n"
        "/leaderboard — Monthly distance leaderboard\n\n"
        "💬 *Other*\n"
        "/quote — Get a random motivational quote\n"
        "/start — Show the welcome message\n"
        "/help — Show this message\n\n"
        "🌐 [www\\.beyondmiles\\.cc](http://www.beyondmiles.cc) \\| 📸 @beyondmilescc",
        parse_mode="MarkdownV2",
        disable_web_page_preview=True,
    )


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Begin the Strava OAuth flow for the user."""
    from app.strava.auth import build_authorization_url, generate_oauth_state

    await _get_or_create_user(update)
    state = await generate_oauth_state(update.effective_user.id)
    auth_url = build_authorization_url(state)

    await update.message.reply_text(
        "Tap *Connect Strava* below to link your account\\.\n\n"
        "We request access to read all your activities \\(including private ones\\) "
        "so your stats and notifications are complete\\.",
        parse_mode="MarkdownV2",
        reply_markup=connect_strava_keyboard(auth_url),
    )


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for confirmation before unlinking the user's Strava account."""
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
    """Manually trigger a Strava activity history sync."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == update.effective_user.id)
        )
        user = result.scalar_one_or_none()

    if not user or not user.strava_athlete_id:
        await update.message.reply_text(
            "You haven't connected your Strava account yet\\. Use /connect to get started\\.",
            parse_mode="MarkdownV2",
        )
        return

    await update.message.reply_text("⏳ Syncing your Strava activities\\. This may take a moment\\.", parse_mode="MarkdownV2")

    import asyncio
    from app.tasks import _sync_user_activities_async
    asyncio.ensure_future(_sync_user_activities_async(user_id=str(user.id)))

    await update.message.reply_text(
        "✅ Sync started\\! Your stats will be up to date shortly\\. Use /stats to check your numbers\\.",
        parse_mode="MarkdownV2",
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
            "You haven't connected your Strava account yet\\.\nUse /connect to get started\\.",
            parse_mode="MarkdownV2",
        )
        return

    await update.message.reply_text(
        "📊 *Your Stats*\n\nChoose an activity type to view your stats:",
        parse_mode="Markdown",
        reply_markup=stats_sport_keyboard(),
    )


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show this month's distance leaderboard across all connected members."""
    from app.models import Activity
    from sqlalchemy import func

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
        medal = medals[i] if i < 3 else f"{i+1}\\."
        name = athlete_name or first_name
        km = round((total_m or 0) / 1000, 1)
        lines.append(f"{medal} {name} — *{km} km*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle activity notifications info."""
    await update.message.reply_text(
        "🔔 Notification preferences are managed at the group level\\.\n"
        "Ask a group admin to configure notifications in the group chat\\.",
        parse_mode="MarkdownV2",
    )


async def cmd_quote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a random motivational quote."""
    await update.message.reply_text(f'💬 *"{_random_quote()}"*', parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Goals ConversationHandler
# ---------------------------------------------------------------------------

_GOAL_SPORTS = ["Ride", "Ride Endurance", "Run", "Swim", "Walk"]
_GOAL_CATEGORIES: dict[str, list[str]] = {
    "Ride":           ["50 km", "100 km", "200 km"],
    "Ride Endurance": ["200 km", "300 km", "400 km", "600 km", "1000 km"],
    "Run":            ["5 km", "10 km", "Half Marathon", "Full Marathon", "Ultra"],
    "Swim":           ["500 m", "1000 m", "1500 m", "2000 m", "3800 m"],
    "Walk":           ["2 km", "5 km", "10 km"],
}
_GOAL_PERIODS = ["This Month", "This Year", "This Week"]
_SPORT_TYPE_MAP = {"Ride Endurance": "RideEndurance"}  # display → DB value


def _goals_keyboard_main() -> object:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Goal", callback_data="goal:add"),
         InlineKeyboardButton("❌ Delete Goal", callback_data="goal:delete_menu")],
        [InlineKeyboardButton("✅  Goal Status", callback_data="goal:status")],
    ])


def _sport_select_keyboard(prefix: str) -> object:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(s, callback_data=f"{prefix}:{s}")] for s in _GOAL_SPORTS]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="goal:cancel")])
    return InlineKeyboardMarkup(rows)


def _category_select_keyboard(sport: str) -> object:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    cats = _GOAL_CATEGORIES.get(sport, [])
    rows = [[InlineKeyboardButton(c, callback_data=f"goal:cat:{c}")] for c in cats]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="goal:cancel")])
    return InlineKeyboardMarkup(rows)


def _period_select_keyboard() -> object:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(p, callback_data=f"goal:period:{p}")] for p in _GOAL_PERIODS]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="goal:cancel")])
    return InlineKeyboardMarkup(rows)


def _goal_period_dates(period: str) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if period == "This Month":
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        if now.month == 12:
            end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    elif period == "This Year":
        start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
        end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:  # This Week
        days_since_monday = now.weekday()
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        from datetime import timedelta
        start = start - timedelta(days=days_since_monday)
        end = start + timedelta(weeks=1)
    return start.date(), end.date()


async def _show_goals_menu(target, user_id: int) -> None:
    """Send or edit the goals menu."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return
        goals = await db.execute(
            select(Goal).where(Goal.user_id == user.id, Goal.is_active == True)  # noqa: E712
        )
        active_goals = goals.scalars().all()

    count = len(active_goals)
    text = f"🎯 *Your Goals*\n\n{count} active goal{'s' if count != 1 else ''}\\."

    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=_goals_keyboard_main())
    else:
        await target.reply_text(text, parse_mode="MarkdownV2", reply_markup=_goals_keyboard_main())


# --- Conversation entry point ---

async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /goals — show the main goals menu."""
    user = await _get_or_create_user(update)
    if not user.strava_athlete_id:
        await update.message.reply_text(
            "Connect your Strava account first with /connect\\.",
            parse_mode="MarkdownV2",
        )
        return ConversationHandler.END

    await _show_goals_menu(update.message, update.effective_user.id)
    return GOAL_CHOOSE_ACTION


# --- Goal action dispatcher ---

async def goal_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "goal:add":
        await query.edit_message_text(
            "Which sport is this goal for?",
            reply_markup=_sport_select_keyboard("goal:sport"),
        )
        return GOAL_CHOOSE_SPORT

    if data == "goal:delete_menu":
        return await _show_delete_menu(query)

    if data == "goal:status":
        return await _show_goal_status(query)

    if data == "goal:cancel":
        await query.edit_message_text("Goals menu closed\\. Use /goals anytime\\.", parse_mode="MarkdownV2")
        return ConversationHandler.END

    return GOAL_CHOOSE_ACTION


# --- Add goal: choose sport ---

async def goal_choose_sport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sport = query.data.replace("goal:sport:", "")
    context.user_data["goal_sport"] = sport

    await query.edit_message_text(
        f"Sport: *{sport}*\n\nChoose a distance category:",
        parse_mode="Markdown",
        reply_markup=_category_select_keyboard(sport),
    )
    return GOAL_CHOOSE_CATEGORY


# --- Add goal: choose category ---

async def goal_choose_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    category = query.data.replace("goal:cat:", "")
    context.user_data["goal_category"] = category

    sport = context.user_data.get("goal_sport", "")
    await query.edit_message_text(
        f"Sport: *{sport}* — Category: *{category}*\n\n"
        "How many times do you want to achieve this?\n\n"
        "Reply with a number \\(e\\.g\\. *4* for 4 rides of 100 km\\):",
        parse_mode="MarkdownV2",
    )
    return GOAL_ENTER_COUNT


# --- Add goal: enter count ---

async def goal_enter_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text(
            "Please enter a positive whole number \\(e\\.g\\. 4\\):",
            parse_mode="MarkdownV2",
        )
        return GOAL_ENTER_COUNT

    context.user_data["goal_count"] = int(text)
    sport = context.user_data.get("goal_sport", "")
    category = context.user_data.get("goal_category", "")

    await update.message.reply_text(
        f"Sport: *{sport}* — {category} × {text}\n\nChoose the time period for this goal:",
        parse_mode="Markdown",
        reply_markup=_period_select_keyboard(),
    )
    return GOAL_CHOOSE_PERIOD


# --- Add goal: choose period → save ---

async def goal_choose_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    period = query.data.replace("goal:period:", "")

    sport_display = context.user_data.get("goal_sport", "")
    category      = context.user_data.get("goal_category", "")
    count         = context.user_data.get("goal_count", 1)
    sport_db      = _SPORT_TYPE_MAP.get(sport_display, sport_display)
    start, end    = _goal_period_dates(period)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == query.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await query.edit_message_text("User not found\\. Try /start first\\.", parse_mode="MarkdownV2")
            return ConversationHandler.END

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

    await query.edit_message_text(
        f"✅ *Goal saved\\!*\n\n"
        f"Sport: *{sport_display}*\n"
        f"Category: *{category}*\n"
        f"Target: *{count}x*\n"
        f"Period: *{period}* \\({start} → {end}\\)\n\n"
        f"Use /goals to view or manage your goals\\.",
        parse_mode="MarkdownV2",
    )
    context.user_data.clear()
    return ConversationHandler.END


# --- Delete goal menu ---

async def _show_delete_menu(query) -> int:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == query.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await query.edit_message_text("User not found\\.", parse_mode="MarkdownV2")
            return ConversationHandler.END

        goals_result = await db.execute(
            select(Goal).where(Goal.user_id == user.id, Goal.is_active == True)  # noqa: E712
        )
        goals = goals_result.scalars().all()

    if not goals:
        await query.edit_message_text(
            "You have no active goals to delete\\.",
            parse_mode="MarkdownV2",
            reply_markup=_goals_keyboard_main(),
        )
        return GOAL_CHOOSE_ACTION

    rows = [
        [InlineKeyboardButton(
            f"{g.activity_type} — {g.category} × {g.target_count} ({g.start_date}→{g.end_date})",
            callback_data=f"goal:confirm_delete:{g.id}"
        )]
        for g in goals
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="goal:back_to_menu")])
    await query.edit_message_text(
        "Select a goal to delete:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return GOAL_CHOOSE_ACTION


async def _show_goal_status(query) -> int:
    from app.models import Activity
    from sqlalchemy import and_, func

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == query.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await query.edit_message_text("User not found\\.", parse_mode="MarkdownV2")
            return ConversationHandler.END

        goals_result = await db.execute(
            select(Goal).where(Goal.user_id == user.id, Goal.is_active == True)  # noqa: E712
        )
        goals = goals_result.scalars().all()

        if not goals:
            await query.edit_message_text(
                "You have no active goals\\. Use ➕ Add Goal to create one\\.",
                parse_mode="MarkdownV2",
                reply_markup=_goals_keyboard_main(),
            )
            return GOAL_CHOOSE_ACTION

        lines = ["✅ *Goal Status*\n"]
        for g in goals:
            start_dt = datetime(g.start_date.year, g.start_date.month, g.start_date.day, tzinfo=timezone.utc)
            end_dt   = datetime(g.end_date.year,   g.end_date.month,   g.end_date.day,   tzinfo=timezone.utc)

            sport_types_map = {
                "Ride":          ["Ride", "VirtualRide"],
                "RideEndurance": ["Ride", "VirtualRide"],
                "Run":           ["Run", "VirtualRun"],
                "Walk":          ["Walk", "Hike"],
                "Swim":          ["Swim", "OpenWaterSwim"],
            }
            act_types = sport_types_map.get(g.activity_type, [g.activity_type])

            # Parse category distance threshold (rough match)
            dist_thresholds = {
                "50 km": 50_000, "100 km": 100_000, "200 km": 200_000,
                "300 km": 300_000, "400 km": 400_000, "600 km": 600_000,
                "1000 km": 1_000_000,
                "5 km": 5_000, "10 km": 10_000,
                "Half Marathon": 21_097, "Full Marathon": 42_195, "Ultra": 50_000,
                "500 m": 500, "1000 m": 1_000, "1500 m": 1_500,
                "2000 m": 2_000, "3800 m": 3_800,
                "2 km": 2_000,
            }
            threshold_m = dist_thresholds.get(g.category, 0)

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
            bar = "✅" * min(achieved, g.target_count) + "⬜" * max(0, g.target_count - achieved)
            pct = min(100, round(achieved / g.target_count * 100))
            lines.append(
                f"*{g.activity_type}* — {g.category}\n"
                f"{bar} {achieved}/{g.target_count} \\({pct}%\\)\n"
                f"_{g.start_date} → {g.end_date}_\n"
            )

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="MarkdownV2",
        reply_markup=_goals_keyboard_main(),
    )
    return GOAL_CHOOSE_ACTION


def _goals_conversation() -> ConversationHandler:
    """Build the ConversationHandler for the goals flow."""
    return ConversationHandler(
        entry_points=[CommandHandler("goals", cmd_goals)],
        states={
            GOAL_CHOOSE_ACTION: [
                CallbackQueryHandler(goal_action_callback,  pattern="^goal:(add|delete_menu|status|cancel|back_to_menu)$"),
                CallbackQueryHandler(_handle_confirm_delete, pattern="^goal:confirm_delete:"),
                CallbackQueryHandler(_handle_back_to_goals,  pattern="^goal:back_to_menu$"),
            ],
            GOAL_CHOOSE_SPORT: [
                CallbackQueryHandler(goal_choose_sport,    pattern="^goal:sport:"),
                CallbackQueryHandler(goal_action_callback, pattern="^goal:cancel$"),
            ],
            GOAL_CHOOSE_CATEGORY: [
                CallbackQueryHandler(goal_choose_category, pattern="^goal:cat:"),
                CallbackQueryHandler(goal_action_callback, pattern="^goal:cancel$"),
            ],
            GOAL_ENTER_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, goal_enter_count),
            ],
            GOAL_CHOOSE_PERIOD: [
                CallbackQueryHandler(goal_choose_period,   pattern="^goal:period:"),
                CallbackQueryHandler(goal_action_callback, pattern="^goal:cancel$"),
            ],
        },
        fallbacks=[CommandHandler("goals", cmd_goals)],
        per_message=False,
        allow_reentry=True,
    )


async def _handle_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    goal_id = query.data.replace("goal:confirm_delete:", "")

    import uuid as _uuid
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Goal).where(Goal.id == _uuid.UUID(goal_id))
        )
        goal = result.scalar_one_or_none()
        if goal:
            goal.is_active = False
            await db.commit()
            await query.edit_message_text(
                f"✅ Goal deleted: *{goal.activity_type}* — {goal.category} × {goal.target_count}\n\nUse /goals to manage your goals\\.",
                parse_mode="MarkdownV2",
            )
        else:
            await query.edit_message_text("Goal not found\\.", parse_mode="MarkdownV2")
    return ConversationHandler.END


async def _handle_back_to_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await _show_goals_menu(query, query.from_user.id)
    return GOAL_CHOOSE_ACTION


# ---------------------------------------------------------------------------
# General callback query handler (outside goals conversation)
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route inline keyboard callbacks to the appropriate sub-handler."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

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
            "📊 *Your Stats*\n\nChoose an activity type to view your stats:",
            parse_mode="Markdown",
            reply_markup=stats_sport_keyboard(),
        )

    elif data == "stats:exit":
        await query.edit_message_text("Stats closed\\. Use /stats anytime to check your numbers\\.", parse_mode="MarkdownV2")

    elif data in ("leaderboard:month", "leaderboard:week"):
        await query.edit_message_text("Use /leaderboard to see this month's standings\\.", parse_mode="MarkdownV2")

    elif data == "quote:random":
        await query.edit_message_text(f'💬 *"{_random_quote()}"*', parse_mode="Markdown")

    elif data == "reconnect:strava":
        from app.strava.auth import build_authorization_url, generate_oauth_state
        state = await generate_oauth_state(query.from_user.id)
        auth_url = build_authorization_url(state)
        from app.telegram.keyboards import connect_strava_keyboard as _ck
        await query.edit_message_text(
            "Tap below to reconnect your Strava account:",
            reply_markup=_ck(auth_url),
        )

    elif data == "disconnect:confirm":
        await _do_disconnect(query)

    elif data in ("disconnect:cancel", "cancel"):
        await query.edit_message_text("Cancelled — your account is still connected\\.", parse_mode="MarkdownV2")

    else:
        logger.warning("Unhandled callback data: %s", data)


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
        except Exception:
            logger.exception("calculate_stats failed for user=%s", user.id)
            await query.edit_message_text("❌ Couldn't load your stats. Try again later.")
            return

    athlete_name = user.strava_athlete_name or user.telegram_first_name
    text = format_stats_message(stats, sport, time_frame, athlete_name)
    await query.edit_message_text(text, parse_mode="Markdown")


async def _do_disconnect(query) -> None:
    """Null out the user's Strava credentials on confirmation."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.telegram_user_id == query.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await query.edit_message_text("Account not found\\.", parse_mode="MarkdownV2")
            return
        user.strava_access_token  = None
        user.strava_refresh_token = None
        user.strava_token_expires_at = None
        user.strava_athlete_id    = None
        await db.commit()

    await query.edit_message_text(
        "✅ Your Strava account has been disconnected\\.\n"
        "Use /connect any time to re\\-link it\\.",
        parse_mode="MarkdownV2",
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
