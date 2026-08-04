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


def key_activity_edit_recent(telegram_user_id: int) -> str:
    """Longer-lived pointer to the activity_id last opened for editing.

    Outlives the short-TTL draft above (key_activity_edit) so that if a
    user's edit session times out, the "Session expired" recovery message
    can still offer a one-tap 'Try Again' back into editing the same
    activity instead of leaving them with no way back short of finding the
    original Strava notification again.
    """
    return f"activity:edit:recent:{telegram_user_id}"


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


def key_reconcile_sweep(user_id, period: str) -> str:
    """Dedup key ensuring a user's periodic full-history reconciliation sync
    (see reconcile_sweep() in tasks.py) runs at most once per period even
    though the cron ticks every few minutes on their assigned day. *period*
    is an ISO "YYYY-Www" week string.
    """
    return f"reconcile:sweep:{user_id}:{period}"


def key_daily_sweep_tick() -> str:
    """Monotonically incrementing counter for Layer 3's daily rotation
    sweep — each catchup_sync_all_users() tick claims the next slot via
    INCR, rather than deriving a slot from wall-clock time-of-day. A
    time-of-day slot only gets visited if some tick happens to land in
    that exact 5-minute window; since the cron actually fires roughly
    every 30 min (not every 5), most of the 288 time-of-day slots would
    never be visited at all. A tick counter guarantees every slot value
    is eventually claimed once per full cycle, regardless of the cron's
    actual cadence or alignment.
    """
    return "ops:daily_sweep:tick"


def key_webhook_health_check() -> str:
    """Dedup key so the Strava webhook-subscription health check (part of
    catchup_sync_all_users()) only actually calls Strava's subscription API
    once per day, not on every cron tick."""
    return "ops:webhook_health_check:last_run_date"


# Recap text is no longer cached (see app/stats/recap.py) — removed
# key_recap_text/key_yearly_recap_text/_RECAP_CACHE_VERSION.


# Shared TTL for the rendered /leaderboard text — a shared, expensive
# aggregation with no per-user staleness concern, so a short blanket
# expiry (rather than event-based invalidation) is the simplest fit.
_LEADERBOARD_CACHE_TTL_SECONDS = 180


def key_leaderboard(month_key: str) -> str:
    """Cached rendered leaderboard text for one calendar month ("YYYY-MM")."""
    return f"leaderboard:v1:{month_key}"


# Short TTL: the same per-goal progress query is run independently by both
# the activity-notification goal footer and the /goals status screen, often
# within seconds of each other. A new activity for that user always
# recomputes (and re-caches) fresh progress immediately, so this rarely
# serves anything more than a few seconds stale.
_GOAL_COUNT_CACHE_TTL_SECONDS = 60


def key_goal_count(goal_id, period_start=None) -> str:
    """Cached progress ("mode|current|target|pct") for one goal's current
    period. v2 — bumped from v1 when get_goal_achieved_count (bare int) was
    replaced by get_goal_progress (structured, metric/aggregation-aware)
    as part of the Flexible Goal Engine, since the cached value's shape
    changed and old v1 entries would otherwise fail to parse.

    *period_start* (a date, optional) distinguishes a Phase 2 recurring
    goal's per-sub-period evaluations (one cache entry per month/quarter)
    from the whole-goal cache entry used when recurrence="none" — without
    it, a monthly sub-period's cached progress could collide with (and be
    overwritten by) another sub-period's, or with the goal's own overall
    entry, since they'd otherwise all share one goal_id-keyed slot.
    """
    if period_start is not None:
        return f"goal:count:v2:{goal_id}:{period_start.isoformat()}"
    return f"goal:count:v2:{goal_id}"
