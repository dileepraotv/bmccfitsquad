"""Inline keyboard builders.

Return InlineKeyboardMarkup objects ready to pass to reply_text / edit_message_text.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu shown after /start."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 My Stats",     callback_data="stats:menu")],
        [InlineKeyboardButton("🎯 My Goals",     callback_data="goal:menu")],
        [InlineKeyboardButton("🏆 Leaderboard",  callback_data="leaderboard:week")],
        [InlineKeyboardButton("💬 Random Quote", callback_data="quote:random")],
    ])


def stats_sport_keyboard() -> InlineKeyboardMarkup:
    """Sport type selector shown after /stats."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚴 Rides",    callback_data="stats:sport:Ride"),
         InlineKeyboardButton("🏃 Runs",     callback_data="stats:sport:Run")],
        [InlineKeyboardButton("🏊 Swims",    callback_data="stats:sport:Swim"),
         InlineKeyboardButton("🚶 Walks",    callback_data="stats:sport:Walk")],
    ])


def stats_period_keyboard(sport: str) -> InlineKeyboardMarkup:
    """Time-period selector shown after the user picks a sport."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 This Month",    callback_data=f"stats:period:{sport}:current_month"),
         InlineKeyboardButton("📅 Last Month",    callback_data=f"stats:period:{sport}:previous_month")],
        [InlineKeyboardButton("📆 Year to Date",  callback_data=f"stats:period:{sport}:year_to_date"),
         InlineKeyboardButton("📆 Last Year",     callback_data=f"stats:period:{sport}:previous_year")],
        [InlineKeyboardButton("🗓  All Time",      callback_data=f"stats:period:{sport}:all_time")],
    ])


def goals_keyboard(goals: list) -> InlineKeyboardMarkup:
    """Goal management keyboard listing active goals with delete options."""
    rows = [
        [InlineKeyboardButton(
            f"❌ {g.metric} — {g.target_value} {g.unit}",
            callback_data=f"goal:delete:{g.id}"
        )]
        for g in goals
    ]
    rows.append([InlineKeyboardButton("➕ Add Goal", callback_data="goal:add")])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(confirm_data: str, cancel_data: str = "cancel") -> InlineKeyboardMarkup:
    """Generic Yes / No confirmation keyboard."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=confirm_data),
        InlineKeyboardButton("❌ No",  callback_data=cancel_data),
    ]])


def connect_strava_keyboard(auth_url: str) -> InlineKeyboardMarkup:
    """Single button that opens the Strava OAuth URL in the browser."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Connect Strava", url=auth_url)
    ]])


def activity_type_keyboard() -> InlineKeyboardMarkup:
    """Let the user pick an activity type when setting a goal."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚴 Ride", callback_data="goal:type:Ride"),
         InlineKeyboardButton("🏃 Run",  callback_data="goal:type:Run")],
        [InlineKeyboardButton("🏊 Swim", callback_data="goal:type:Swim"),
         InlineKeyboardButton("🚶 Walk", callback_data="goal:type:Walk")],
        [InlineKeyboardButton("🌐 Any",  callback_data="goal:type:Any")],
    ])
