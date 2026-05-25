"""Goal CRUD and progress tracking.

Thin service layer between the Telegram handlers and the database.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Goal


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def get_active_goals(db: AsyncSession, user_id: int) -> list[Goal]:
    """Return all active goals for a user, ordered by creation date."""
    # TODO:
    #   result = await db.execute(
    #       select(Goal)
    #       .where(Goal.user_id == user_id, Goal.is_active == True)
    #       .order_by(Goal.created_at)
    #   )
    #   return result.scalars().all()
    raise NotImplementedError


async def get_goal_by_id(db: AsyncSession, goal_id: int, user_id: int) -> Goal | None:
    """Fetch a single goal, enforcing ownership."""
    # TODO:
    #   result = await db.execute(
    #       select(Goal).where(Goal.id == goal_id, Goal.user_id == user_id)
    #   )
    #   return result.scalar_one_or_none()
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

async def create_goal(
    db: AsyncSession,
    user_id: int,
    metric: str,
    target_value: float,
    unit: str,
    period: str,
    activity_type: str | None = None,
) -> Goal:
    """Create and persist a new goal for the user.

    Args:
        metric: One of "distance", "duration", "elevation".
        target_value: Numeric target (e.g. 500 for 500 km).
        unit: Display unit matching the metric (e.g. "km", "hours", "m").
        period: "weekly", "monthly", or "yearly".
        activity_type: Optional Strava activity type filter (e.g. "Ride").
    """
    # TODO:
    #   goal = Goal(
    #       user_id=user_id,
    #       metric=metric,
    #       target_value=target_value,
    #       unit=unit,
    #       period=period,
    #       activity_type=activity_type,
    #   )
    #   db.add(goal)
    #   await db.flush()
    #   return goal
    raise NotImplementedError


async def deactivate_goal(db: AsyncSession, goal_id: int, user_id: int) -> bool:
    """Soft-delete a goal by marking it inactive. Returns True if found and updated."""
    # TODO:
    #   goal = await get_goal_by_id(db, goal_id, user_id)
    #   if goal is None:
    #       return False
    #   goal.is_active = False
    #   await db.flush()
    #   return True
    raise NotImplementedError


async def update_goal(
    db: AsyncSession,
    goal_id: int,
    user_id: int,
    **fields,
) -> Goal | None:
    """Update arbitrary fields on a goal. Returns the updated Goal or None if not found."""
    # TODO:
    #   goal = await get_goal_by_id(db, goal_id, user_id)
    #   if goal is None:
    #       return None
    #   for key, value in fields.items():
    #       setattr(goal, key, value)
    #   await db.flush()
    #   return goal
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

VALID_METRICS = {"distance", "duration", "elevation"}
VALID_PERIODS = {"weekly", "monthly", "yearly"}
METRIC_UNITS = {
    "distance": "km",
    "duration": "hours",
    "elevation": "m",
}


def validate_goal_input(metric: str, target_value: float, period: str) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []
    if metric not in VALID_METRICS:
        errors.append(f"Invalid metric '{metric}'. Choose from: {', '.join(VALID_METRICS)}.")
    if target_value <= 0:
        errors.append("Target value must be greater than zero.")
    if period not in VALID_PERIODS:
        errors.append(f"Invalid period '{period}'. Choose from: {', '.join(VALID_PERIODS)}.")
    return errors
