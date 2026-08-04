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


def _padded(label: str, width: int = 14) -> str:
    """Centre-pad a button label with spaces so short labels fill as much of
    the button's tap area as longer ones in the same keyboard, instead of
    leaving the button looking mostly empty around a short word.

    ``width`` should never be shorter than the label — ``str.center`` is a
    no-op (not a truncation) when that happens, so it's always safe to call.
    """
    return label.center(width)


# Shared widths so every keyboard in the bot fills the same proportion of
# its row regardless of how many buttons share it — matches the constants
# used for the goals flow in handlers.py (_PAD_FULL/_PAD_2COL/_PAD_3COL).
_PAD_FULL = 42  # 1 button spanning the whole row
_PAD_2COL = 32  # 2 buttons sharing a row
_PAD_3COL = 20  # 3 buttons sharing a row


def stats_sport_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_padded("Ride", _PAD_2COL),           callback_data="stats:sport:Ride"),
         InlineKeyboardButton(_padded("Ride Endurance", _PAD_2COL), callback_data="stats:sport:RideEndurance")],
        [InlineKeyboardButton(_padded("Run", _PAD_3COL),  callback_data="stats:sport:Run"),
         InlineKeyboardButton(_padded("Swim", _PAD_3COL), callback_data="stats:sport:Swim"),
         InlineKeyboardButton(_padded("Walk", _PAD_3COL), callback_data="stats:sport:Walk")],
        [InlineKeyboardButton(_padded("Other Activities", _PAD_FULL), callback_data="stats:other")],
        [InlineKeyboardButton(_padded("Exit", _PAD_FULL),            callback_data="stats:exit")],
    ])


def stats_other_sport_keyboard() -> InlineKeyboardMarkup:
    """Secondary sport menu for the non-core sports (duration/hike-based),
    kept off the main keyboard so it doesn't crowd the primary five sports."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_padded("Yoga", _PAD_2COL),              callback_data="stats:sport:Yoga"),
         InlineKeyboardButton(_padded("Racket Sports", _PAD_2COL),     callback_data="stats:sport:RacketSports")],
        [InlineKeyboardButton(_padded("Hiking", _PAD_2COL),            callback_data="stats:sport:Hiking"),
         InlineKeyboardButton(_padded("Strength Training", _PAD_2COL), callback_data="stats:sport:StrengthTraining")],
        [InlineKeyboardButton(_padded("Back", _PAD_2COL),  callback_data="stats:menu"),
         InlineKeyboardButton(_padded("Exit", _PAD_2COL),  callback_data="stats:exit")],
    ])


def stats_period_keyboard(sport: str) -> InlineKeyboardMarkup:
    s = sport
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_padded("This Week", _PAD_3COL),  callback_data=f"stats:period:{s}:this_week"),
         InlineKeyboardButton(_padded("This Month", _PAD_3COL), callback_data=f"stats:period:{s}:current_month"),
         InlineKeyboardButton(_padded("This Year", _PAD_3COL),  callback_data=f"stats:period:{s}:year_to_date")],
        [InlineKeyboardButton(_padded("All Time", _PAD_3COL),   callback_data=f"stats:period:{s}:all_time"),
         InlineKeyboardButton(_padded("Last Month", _PAD_3COL), callback_data=f"stats:period:{s}:previous_month"),
         InlineKeyboardButton(_padded("Last Year", _PAD_3COL),  callback_data=f"stats:period:{s}:previous_year")],
        [InlineKeyboardButton(_padded("Back", _PAD_2COL),   callback_data="stats:menu"),
         InlineKeyboardButton(_padded("Exit", _PAD_2COL),   callback_data="stats:exit")],
    ])


def stats_nav_keyboard(sport: str) -> InlineKeyboardMarkup:
    """Navigation keyboard shown below a stats result."""
    from app.utils import OTHER_ACTIVITY_SPORTS
    change_sport_target = "stats:other" if sport in OTHER_ACTIVITY_SPORTS else "stats:menu"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(_padded("Change Period", _PAD_3COL), callback_data=f"stats:sport:{sport}"),
        InlineKeyboardButton(_padded("Change Sport", _PAD_3COL),  callback_data=change_sport_target),
        InlineKeyboardButton(_padded("Close", _PAD_3COL),         callback_data="stats:exit"),
    ]])


def goals_keyboard(goals: list) -> InlineKeyboardMarkup:
    """Goal management keyboard listing active goals with delete options."""
    rows = [
        [InlineKeyboardButton(
            _padded(f"{g.metric} — {g.target_value} {g.unit}", _PAD_2COL),
            callback_data=f"goal:delete:{g.id}"
        )]
        for g in goals
    ]
    rows.append([InlineKeyboardButton(_padded("Add Goal", _PAD_2COL), callback_data="goal:add")])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(confirm_data: str, cancel_data: str = "cancel") -> InlineKeyboardMarkup:
    """Generic Yes / No confirmation keyboard."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(_padded("Yes", _PAD_2COL), callback_data=confirm_data),
        InlineKeyboardButton(_padded("No", _PAD_2COL),  callback_data=cancel_data),
    ]])


def connect_strava_keyboard(auth_url: str) -> InlineKeyboardMarkup:
    """Single button that opens the Strava OAuth URL in the browser."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(_padded("Connect Strava", _PAD_FULL), url=auth_url)
    ]])


def activity_edit_description_keyboard() -> InlineKeyboardMarkup:
    """Skip / Cancel while entering activity description (after name step)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(_padded("Skip description", _PAD_2COL), callback_data="activity:desc_skip"),
        InlineKeyboardButton(_padded("Cancel", _PAD_2COL),           callback_data="activity:desc_cancel"),
    ]])


def post_dismiss_keyboard() -> InlineKeyboardMarkup:
    """Shown after an activity notification is dismissed — offers the three
    top-level destinations instead of just ending the interaction, so the
    user has an obvious next step to follow."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(_padded("Stats", _PAD_3COL), callback_data="postact:stats"),
        InlineKeyboardButton(_padded("Goals", _PAD_3COL), callback_data="postact:goals"),
        InlineKeyboardButton(_padded("Help", _PAD_3COL),  callback_data="postact:help"),
    ]])


def recap_goal_prompt_keyboard() -> InlineKeyboardMarkup:
    """Shown under the monthly recap caption, offering to set next month's goal."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(_padded("Set a Goal", _PAD_2COL), callback_data="goal:add"),
        InlineKeyboardButton(_padded("Not now", _PAD_2COL),    callback_data="recap:dismiss"),
    ]])


def activity_type_keyboard() -> InlineKeyboardMarkup:
    """Let the user pick an activity type when setting a goal."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_padded("Ride", _PAD_2COL), callback_data="goal:type:Ride"),
         InlineKeyboardButton(_padded("Run", _PAD_2COL),  callback_data="goal:type:Run")],
        [InlineKeyboardButton(_padded("Swim", _PAD_2COL), callback_data="goal:type:Swim"),
         InlineKeyboardButton(_padded("Walk", _PAD_2COL), callback_data="goal:type:Walk")],
        [InlineKeyboardButton(_padded("Any", _PAD_FULL),  callback_data="goal:type:Any")],
    ])
