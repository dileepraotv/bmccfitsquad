"""Keyboard builders — both inline and reply keyboards.

Return InlineKeyboardMarkup or ReplyKeyboardMarkup objects ready to pass to
reply_text / edit_message_text.
"""
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# Labels used by the persistent nav bar — imported in handlers.py to route
# incoming text messages back to the matching command.
NAV_STATS = "Stats"
NAV_GOALS = "Goals"
NAV_HELP  = "Help"


def nav_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom-row navigation bar."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(NAV_STATS), KeyboardButton(NAV_GOALS), KeyboardButton(NAV_HELP)]],
        resize_keyboard=True,
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu shown after /start or when user is already connected."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("My Stats",  callback_data="stats:menu"),
         InlineKeyboardButton("My Goals",  callback_data="goal:menu")],
    ])


def stats_sport_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ride",           callback_data="stats:sport:Ride"),
         InlineKeyboardButton("Ride Endurance", callback_data="stats:sport:RideEndurance")],
        [InlineKeyboardButton("Run",            callback_data="stats:sport:Run"),
         InlineKeyboardButton("Swim",           callback_data="stats:sport:Swim"),
         InlineKeyboardButton("Walk",           callback_data="stats:sport:Walk")],
        [InlineKeyboardButton("Exit",           callback_data="stats:exit")],
    ])


def stats_period_keyboard(sport: str) -> InlineKeyboardMarkup:
    s = sport
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("All Time",       callback_data=f"stats:period:{s}:all_time"),
         InlineKeyboardButton("Year to Date",   callback_data=f"stats:period:{s}:year_to_date"),
         InlineKeyboardButton("Previous Year",  callback_data=f"stats:period:{s}:previous_year")],
        [InlineKeyboardButton("Current Month",  callback_data=f"stats:period:{s}:current_month"),
         InlineKeyboardButton("Previous Month", callback_data=f"stats:period:{s}:previous_month")],
        [InlineKeyboardButton("Back",           callback_data="stats:menu"),
         InlineKeyboardButton("Exit",           callback_data="stats:exit")],
    ])


def stats_nav_keyboard(sport: str) -> InlineKeyboardMarkup:
    """Navigation keyboard shown below a stats result."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Change Period", callback_data=f"stats:sport:{sport}"),
        InlineKeyboardButton("Change Sport",  callback_data="stats:menu"),
        InlineKeyboardButton("Close",         callback_data="stats:exit"),
    ]])


def goals_keyboard(goals: list) -> InlineKeyboardMarkup:
    """Goal management keyboard listing active goals with delete options."""
    rows = [
        [InlineKeyboardButton(
            f"{g.metric} — {g.target_value} {g.unit}",
            callback_data=f"goal:delete:{g.id}"
        )]
        for g in goals
    ]
    rows.append([InlineKeyboardButton("Add Goal", callback_data="goal:add")])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(confirm_data: str, cancel_data: str = "cancel") -> InlineKeyboardMarkup:
    """Generic Yes / No confirmation keyboard."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes", callback_data=confirm_data),
        InlineKeyboardButton("No",  callback_data=cancel_data),
    ]])


def connect_strava_keyboard(auth_url: str) -> InlineKeyboardMarkup:
    """Single button that opens the Strava OAuth URL in the browser."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Connect Strava", url=auth_url)
    ]])


def activity_type_keyboard() -> InlineKeyboardMarkup:
    """Let the user pick an activity type when setting a goal."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ride", callback_data="goal:type:Ride"),
         InlineKeyboardButton("Run",  callback_data="goal:type:Run")],
        [InlineKeyboardButton("Swim", callback_data="goal:type:Swim"),
         InlineKeyboardButton("Walk", callback_data="goal:type:Walk")],
        [InlineKeyboardButton("Any",  callback_data="goal:type:Any")],
    ])
