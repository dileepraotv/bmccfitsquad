"""Inline keyboard builders.

Return InlineKeyboardMarkup objects ready to pass to reply_text / edit_message_text.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu shown after /start."""
    # TODO: build a keyboard with buttons for Stats, Goals, Leaderboard, Notifications, Quote
    #   buttons = [
    #       [InlineKeyboardButton("📊 My Stats", callback_data="stats:menu")],
    #       [InlineKeyboardButton("🎯 My Goals", callback_data="goal:menu")],
    #       [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard:week")],
    #       [InlineKeyboardButton("🔔 Notifications", callback_data="notifications:toggle")],
    #       [InlineKeyboardButton("💬 Quote", callback_data="quote:random")],
    #   ]
    #   return InlineKeyboardMarkup(buttons)
    raise NotImplementedError


def stats_period_keyboard() -> InlineKeyboardMarkup:
    """Period selector for the /stats command."""
    # TODO:
    #   buttons = [
    #       [InlineKeyboardButton("This Week", callback_data="stats:week"),
    #        InlineKeyboardButton("This Month", callback_data="stats:month")],
    #       [InlineKeyboardButton("This Year", callback_data="stats:year"),
    #        InlineKeyboardButton("All Time", callback_data="stats:all")],
    #   ]
    #   return InlineKeyboardMarkup(buttons)
    raise NotImplementedError


def goals_keyboard(goals: list) -> InlineKeyboardMarkup:
    """Goal management keyboard listing active goals with edit/delete options."""
    # TODO:
    #   rows = [[InlineKeyboardButton(f"❌ {g.metric} {g.target_value}{g.unit}", callback_data=f"goal:delete:{g.id}")]
    #           for g in goals]
    #   rows.append([InlineKeyboardButton("➕ Add Goal", callback_data="goal:add")])
    #   return InlineKeyboardMarkup(rows)
    raise NotImplementedError


def confirm_keyboard(confirm_data: str, cancel_data: str = "cancel") -> InlineKeyboardMarkup:
    """Generic Yes / No confirmation keyboard."""
    # TODO:
    #   return InlineKeyboardMarkup([[
    #       InlineKeyboardButton("✅ Yes", callback_data=confirm_data),
    #       InlineKeyboardButton("❌ No", callback_data=cancel_data),
    #   ]])
    raise NotImplementedError


def connect_strava_keyboard(auth_url: str) -> InlineKeyboardMarkup:
    """Single button that opens the Strava OAuth URL."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Connect Strava", url=auth_url)
    ]])


def activity_type_keyboard() -> InlineKeyboardMarkup:
    """Let the user pick an activity type when setting a goal."""
    # TODO: list common types — Ride, Run, Walk, Swim, etc. — plus an "Any" option
    raise NotImplementedError
