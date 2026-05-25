"""All Telegram command and callback handlers.

Register every handler in `register_handlers()` — it is called once at startup.
"""
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.telegram.keyboards import (
    main_menu_keyboard,
    stats_period_keyboard,
    goals_keyboard,
)

logger = logging.getLogger(__name__)


def register_handlers(app: Application) -> None:
    """Attach all handlers to the PTB Application."""
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("goals", cmd_goals))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("notifications", cmd_notifications))
    app.add_handler(CommandHandler("quote", cmd_quote))

    app.add_handler(CallbackQueryHandler(handle_callback))

    # Catch-all for unrecognised messages in private chats
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_unknown))

    app.add_error_handler(handle_error)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message and main menu."""
    # TODO:
    #   1. Upsert the User row (id, first_name, username) in the database
    #   2. If user already has a linked Strava account, show the main menu
    #   3. Otherwise, prompt them to /connect their Strava account
    #   await update.message.reply_text("Welcome to BMCC Bot! 🚴", reply_markup=main_menu_keyboard())
    raise NotImplementedError


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the help message listing all available commands."""
    # TODO: format and send a help text string listing all commands with descriptions
    raise NotImplementedError


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Begin the Strava OAuth flow for the user."""
    # TODO:
    #   1. Generate an OAuth state token via strava.auth.generate_oauth_state()
    #   2. Build the authorization URL via strava.auth.build_authorization_url()
    #   3. Send the URL as an inline button so the user taps to authorise
    raise NotImplementedError


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unlink the user's Strava account."""
    # TODO:
    #   1. Confirm the action with an inline Yes/No keyboard
    #   2. On confirmation, null-out strava_* fields on the User row
    #   3. Optionally revoke the token via Strava's deauthorise endpoint
    raise NotImplementedError


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a stats period selector (this week / this month / this year / all-time)."""
    # TODO:
    #   1. Check the user is connected; prompt them to /connect if not
    #   2. Send a message with stats_period_keyboard() for the user to choose a period
    raise NotImplementedError


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the user's active goals and show goal management options."""
    # TODO:
    #   1. Fetch active goals from the database
    #   2. Format each goal with current progress via stats.calculator
    #   3. Send with goals_keyboard() for add/edit/delete actions
    raise NotImplementedError


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the group leaderboard for the current week/month."""
    # TODO:
    #   1. Only meaningful in group chats — send a tip if used in private chat
    #   2. Aggregate distance across all connected users for the chosen period
    #   3. Format and send the ranked list
    raise NotImplementedError


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle activity notifications for the user."""
    # TODO:
    #   1. Flip user.notifications_enabled in the database
    #   2. Confirm the new state to the user
    raise NotImplementedError


async def cmd_quote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a random motivational quote from data/quotes.txt."""
    # TODO:
    #   import random, pathlib
    #   quotes = pathlib.Path("data/quotes.txt").read_text().splitlines()
    #   await update.message.reply_text(random.choice(quotes))
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Callback query handler (inline keyboard button presses)
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route inline keyboard callbacks to the appropriate sub-handler."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # TODO: implement a routing scheme, e.g.:
    #   if data.startswith("stats:"):    await _callback_stats(query, context, data)
    #   elif data.startswith("goal:"):   await _callback_goal(query, context, data)
    #   elif data.startswith("disconnect_confirm"): ...
    logger.warning("Unhandled callback data: %s", data)


# ---------------------------------------------------------------------------
# Fallback handlers
# ---------------------------------------------------------------------------

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nudge users who send plain text in a private chat."""
    await update.message.reply_text("Use /help to see what I can do.")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log all unhandled exceptions raised by handlers."""
    logger.exception("Unhandled error for update %s", update, exc_info=context.error)
