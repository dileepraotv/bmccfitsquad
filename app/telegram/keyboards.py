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


def stats_sport_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ride",           callback_data="stats:sport:Ride"),
         InlineKeyboardButton("Ride Endurance", callback_data="stats:sport:RideEndurance")],
        [InlineKeyboardButton("Run",            callback_data="stats:sport:Run"),
         InlineKeyboardButton("Swim",           callback_data="stats:sport:Swim"),
         InlineKeyboardButton("Walk",           callback_data="stats:sport:Walk")],
        [InlineKeyboardButton("Exit",           callback_data="stats:exit")],
    ])


def _padded(label: str, width: int = 14) -> str:
    """Centre-pad a button label with spaces so short labels fill as much of
    the button's tap area as longer ones in the same keyboard, instead of
    leaving the button looking mostly empty around a short word."""
    return label.center(width)


def stats_period_keyboard(sport: str) -> InlineKeyboardMarkup:
    s = sport
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_padded("This Week"),  callback_data=f"stats:period:{s}:this_week"),
         InlineKeyboardButton(_padded("This Month"), callback_data=f"stats:period:{s}:current_month"),
         InlineKeyboardButton(_padded("This Year"),  callback_data=f"stats:period:{s}:year_to_date")],
        [InlineKeyboardButton(_padded("All Time"),   callback_data=f"stats:period:{s}:all_time"),
         InlineKeyboardButton(_padded("Last Month"), callback_data=f"stats:period:{s}:previous_month"),
         InlineKeyboardButton(_padded("Last Year"),  callback_data=f"stats:period:{s}:previous_year")],
        [InlineKeyboardButton(_padded("Back", 21),   callback_data="stats:menu"),
         InlineKeyboardButton(_padded("Exit", 21),   callback_data="stats:exit")],
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


def activity_edit_description_keyboard() -> InlineKeyboardMarkup:
    """Skip / Cancel while entering activity description (after name step)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Skip description", callback_data="activity:desc_skip"),
        InlineKeyboardButton("Cancel", callback_data="activity:desc_cancel"),
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
