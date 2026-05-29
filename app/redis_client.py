import redis.asyncio as aioredis

from app.config import get_settings

settings = get_settings()

_redis: aioredis.Redis | None = None


def _build_redis_url() -> str:
    """Ensure Upstash URLs always use the rediss:// (TLS) scheme.

    Upstash requires TLS but users sometimes paste the redis:// URL by mistake.
    Any URL pointing at a known Upstash host is upgraded to rediss:// so the
    TLS handshake succeeds regardless of which URL was copied from the console.
    """
    url = settings.redis_url
    if url.startswith("redis://") and "upstash.io" in url:
        url = "rediss://" + url[len("redis://"):]
    return url


async def get_redis() -> aioredis.Redis:
    """Return the shared Redis connection, initialising it on first call."""
    global _redis
    if _redis is None:
        url = _build_redis_url()
        kwargs: dict = {
            "encoding": "utf-8",
            "decode_responses": True,
        }
        # Upstash uses a wildcard TLS cert that Python's ssl module rejects.
        # Disable certificate verification while keeping the connection encrypted.
        if url.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = None
        _redis = aioredis.from_url(url, **kwargs)
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


def key_activity_edit(telegram_user_id: int) -> str:
    """Draft key for the in-progress activity name/description edit flow."""
    return f"activity:edit:{telegram_user_id}"
