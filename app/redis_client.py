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
    """Return the shared Redis connection, initialising it on first call.

    Pool is intentionally sized to 1 connection — the app only ever does
    short serial GET/SET/DEL calls, never concurrent bursts.  A single
    connection means one TLS handshake and zero idle keepalive PINGs beyond
    the single connection's socket-level TCP keepalive.
    """
    global _redis
    if _redis is None:
        url = _build_redis_url()
        kwargs: dict = {
            "encoding": "utf-8",
            "decode_responses": True,
            # Single connection — eliminates per-connection PING keepalives
            # that a larger pool would generate even with no traffic.
            "max_connections": 1,
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

def key_oauth_state(state: str) -> str:
    """Short-lived key used to verify Strava OAuth state parameter."""
    return f"oauth:state:{state}"


def key_activity_seen(activity_id: int) -> str:
    """24-hour deduplication key — prevents double-processing webhook deliveries."""
    return f"activity:seen:{activity_id}"


def key_activity_edit(telegram_user_id: int) -> str:
    """Draft key for the in-progress activity name/description edit flow."""
    return f"activity:edit:{telegram_user_id}"


def key_heartbeat() -> str:
    """Last-seen timestamp updated by /ping and /cron/sync-all.

    Used by catchup_sync_all_users() to detect an outage/cold-start gap
    (process was unreachable for longer than the expected keep-alive
    cadence) — the only remaining scenario where a bounded full-user Strava
    scan is actually warranted, instead of scanning every user every tick.
    """
    return "ops:heartbeat:last_seen"


def key_strava_rate_limit() -> str:
    """Latest Strava X-RateLimit-Usage snapshot (15min_usage,daily_usage)."""
    return "strava:rate_limit:usage"


# Bump this whenever render_recap_card()'s visual output changes so stale
# cached renders from before the change are transparently bypassed instead
# of being served for up to the full cache TTL (see _RECAP_CACHE_TTL_SECONDS).
_RECAP_CACHE_VERSION = 9


def key_recap_image(user_id, year: int, month: int) -> str:
    """Cached recap card PNG (base64-encoded) for one user + calendar month."""
    return f"recap:v{_RECAP_CACHE_VERSION}:image:{user_id}:{year}-{month:02d}"


def key_recap_caption(user_id, year: int, month: int) -> str:
    """Cached recap caption text for one user + calendar month."""
    return f"recap:v{_RECAP_CACHE_VERSION}:caption:{user_id}:{year}-{month:02d}"


def key_yearly_recap_image(user_id, year: int) -> str:
    """Cached yearly recap card PNG (base64-encoded) for one user + year."""
    return f"yearrecap:v{_RECAP_CACHE_VERSION}:image:{user_id}:{year}"


def key_yearly_recap_caption(user_id, year: int) -> str:
    """Cached yearly recap caption text for one user + year."""
    return f"yearrecap:v{_RECAP_CACHE_VERSION}:caption:{user_id}:{year}"
