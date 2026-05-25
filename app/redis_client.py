import redis.asyncio as aioredis

from app.config import get_settings

settings = get_settings()

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return the shared Redis connection, initialising it on first call."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


# ---------------------------------------------------------------------------
# Convenience key helpers (centralise key naming to avoid typos)
# ---------------------------------------------------------------------------

def key_strava_token(telegram_user_id: int) -> str:
    return f"strava:token:{telegram_user_id}"


def key_oauth_state(state: str) -> str:
    """Short-lived key used to verify Strava OAuth state parameter."""
    return f"oauth:state:{state}"


def key_rate_limit(telegram_user_id: int, command: str) -> str:
    return f"ratelimit:{telegram_user_id}:{command}"


def key_activity_seen(activity_id: int) -> str:
    """Deduplication key so we never broadcast the same activity twice."""
    return f"activity:seen:{activity_id}"
