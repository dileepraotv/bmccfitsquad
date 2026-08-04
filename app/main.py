"""FastAPI application entry point.

Routes
------
  GET      /cron/sync-all  — event-driven catch-up safety net (UptimeRobot/cron-job.org)
  GET|HEAD /ping            — instant keep-alive (UptimeRobot pings this every 5 min)
  GET  /health              — liveness probe with cached DB check
  GET  /strava/webhook      — Strava hub challenge verification
  POST /strava/webhook      — Strava activity / athlete events
  GET  /strava/callback     — OAuth redirect from Strava after user approval
  POST /telegram/webhook    — Telegram bot updates

Startup sequence
----------------
  1. Configure logging
  2. Create all DB tables if they don't exist (Alembic handles schema changes in prod)
  3. Warm the Redis connection pool
  4. Start the Telegram bot and register the webhook with Telegram API

Shutdown sequence
-----------------
  1. Stop and shutdown the PTB Application
  2. Close the Redis connection pool
"""
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import check_db_connection, init_db
from app.redis_client import close_redis, get_redis
from app.strava.webhook import router as strava_router
from app.telegram.bot import router as telegram_router
from app.telegram.bot import setup_bot, teardown_bot

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---------------------------------------------------------------
    # Startup
    # ---------------------------------------------------------------
    logging.basicConfig(
        level=logging.DEBUG if not settings.is_production else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger.info(
        "Starting BMCC bot — env=%s base_url=%s",
        settings.app_env,
        settings.base_url,
    )

    # 1. Database — create tables if they don't exist
    #    In production, run `alembic upgrade head` instead of relying on this.
    await init_db()
    logger.info("Database tables ready")

    # 2. Redis — establish connection pool now (not lazily on first request)
    await get_redis()
    logger.info("Redis connection ready")

    # 3. Telegram bot — initialise PTB Application and register webhook
    await setup_bot()
    logger.info("Telegram bot ready (webhook registered)")

    # 4. Warn loudly if CRON_SECRET is not configured
    if not settings.cron_secret:
        logger.warning(
            "CRON_SECRET env var is not set — /cron/sync-all will reject all requests. "
            "Set CRON_SECRET in Render environment variables to enable the catchup sync."
        )
    else:
        logger.info("Cron secret configured — /cron/sync-all is active")

    # 5. Verify Strava webhook subscription points to this deployment
    try:
        from app.strava.client import view_webhook_subscription
        subs = await view_webhook_subscription()
        valid_urls = settings.strava_webhook_valid_callback_urls
        registered = any(s.get("callback_url") in valid_urls for s in subs)
        if registered:
            logger.info("Strava webhook subscription OK: %s", [s.get("callback_url") for s in subs])
        else:
            logger.warning(
                "Strava webhook subscription MISMATCH or missing. "
                "Expected one of %s but found: %s. "
                "Run scripts/register_strava_webhook.py to fix.",
                sorted(valid_urls),
                [s.get("callback_url") for s in subs],
            )
    except Exception as exc:
        logger.warning("Could not verify Strava webhook subscription: %s", exc)

    yield

    # ---------------------------------------------------------------
    # Shutdown
    # ---------------------------------------------------------------
    logger.info("Shutting down BMCC bot")
    await teardown_bot()
    await close_redis()
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="BMCC Fitness Bot",
    description=(
        "Telegram bot for **Beyond Miles Cycling Club (BMCC)**.\n\n"
        "Connects to Strava via webhooks, posts activity notifications to the "
        "group chat, and tracks personal stats and goals."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
)

# CORS is only relevant for browser-originated requests.
# Strava and Telegram webhooks are server-to-server and don't use CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not settings.is_production else [],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(strava_router, prefix="/strava", tags=["strava"])
app.include_router(telegram_router, prefix="/telegram", tags=["telegram"])

# ---------------------------------------------------------------------------
# /webhook  — top-level alias for the Strava webhook endpoints
# ---------------------------------------------------------------------------
# Strava subscription 359553 is registered at /webhook (not /strava/webhook).
# These routes delegate directly to the same handlers so no re-registration
# of the subscription is needed when switching base URL.

from app.strava.webhook import strava_webhook_event, strava_webhook_verify  # noqa: E402


@app.get("/webhook", tags=["strava"], include_in_schema=False)
async def webhook_challenge_alias(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
):
    return await strava_webhook_verify(
        hub_mode=hub_mode,
        hub_verify_token=hub_verify_token,
        hub_challenge=hub_challenge,
    )


@app.post("/webhook", tags=["strava"], include_in_schema=False)
async def webhook_event_alias(request: Request):
    return await strava_webhook_event(request)


# ---------------------------------------------------------------------------
# Keep-alive + health endpoints
# ---------------------------------------------------------------------------
# Render free tier spins down after 15 min of inactivity.
# UptimeRobot (free) is configured to ping /ping every 5 minutes — this
# endpoint does zero DB/Redis work so it responds instantly even on a
# cold-start path and does not consume any free-tier quota.
#
# /health does a real (cached) DB check for actual liveness monitoring.

_cron_status: dict = {
    "last_run": None,
    "last_result": None,
    "run_count": 0,
    "last_recap_result": None,
    "last_yearly_recap_result": None,
    "last_goal_checkpoint_result": None,
}


@app.api_route("/cron/sync-all", methods=["GET", "HEAD"], tags=["ops"], summary="Catchup sync — finds activities missed by webhook")
async def cron_sync_all(secret: str = ""):
    """Called periodically (UptimeRobot/cron-job.org) as a reliability safety net.

    Event-driven, not a blind poll — see the module docstring above
    catchup_sync_all_users() in tasks.py for the 5-layer design (webhook
    subscription health check, repair pending webhook events, heartbeat-gated
    outage recovery, low-frequency daily rotation, weekly full-history
    reconciliation). Strava API usage no longer scales with (users x ticks).

    Protected by: ?secret={CRON_SECRET} query parameter
    """
    import asyncio
    from app.tasks import (
        catchup_sync_all_users,
        fire_and_forget,
        maybe_send_goal_checkpoints,
        maybe_send_monthly_recaps,
        maybe_send_yearly_recap,
    )

    if not settings.cron_secret or secret != settings.cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    _cron_status["last_run"] = time.time()
    _cron_status["run_count"] += 1

    async def _run_and_record():
        result = await catchup_sync_all_users()
        _cron_status["last_result"] = result

    async def _run_recaps():
        # Sequential, not parallel: on 31 Dec the yearly card must land
        # right after that same day's monthly card, not race it.
        _cron_status["last_recap_result"] = await maybe_send_monthly_recaps()
        _cron_status["last_yearly_recap_result"] = await maybe_send_yearly_recap()
        # Same 21:00 IST / last-day-of-month trigger, but per recurring goal
        # rather than per user (Flexible Goal Engine Phase 2).
        _cron_status["last_goal_checkpoint_result"] = await maybe_send_goal_checkpoints()

    fire_and_forget(_run_and_record())
    # Piggybacks on this same ping — no-ops on every tick except once, at
    # 21:00 IST on the last day of the month (see maybe_send_monthly_recaps
    # and maybe_send_yearly_recap).
    fire_and_forget(_run_recaps())
    return {"status": "scheduled", "run_count": _cron_status["run_count"]}


@app.get("/cron/status", tags=["ops"], summary="Check if cron sync is running correctly")
async def cron_status():
    """Public endpoint — shows when the cron last ran and what it found.

    Use this to verify CRON_SECRET is set correctly in Render and that
    UptimeRobot is successfully calling /cron/sync-all.

    If last_run is None or very old, CRON_SECRET is likely missing or wrong.
    """
    from app.strava.client import get_rate_limit_status

    last_run = _cron_status["last_run"]
    return {
        "cron_secret_configured": bool(settings.cron_secret),
        "run_count": _cron_status["run_count"],
        "last_run_ts": last_run,
        "last_run_ago_seconds": round(time.time() - last_run) if last_run else None,
        "last_result": _cron_status["last_result"],
        "last_recap_result": _cron_status["last_recap_result"],
        "last_yearly_recap_result": _cron_status["last_yearly_recap_result"],
        "last_goal_checkpoint_result": _cron_status["last_goal_checkpoint_result"],
        "strava_rate_limit": await get_rate_limit_status(),
    }


@app.get("/ops/recent-errors", tags=["ops"], summary="Recent unhandled exceptions from Telegram handlers")
async def recent_errors(secret: str = ""):
    """In-memory ring buffer (last 25) of unhandled handler exceptions.

    Telegram's webhook always gets 200 OK even when a handler raises (see
    telegram_webhook() in bot.py + handle_error() in handlers.py), so these
    failures are otherwise only visible in Render's own log dashboard.
    Protected by ?secret={CRON_SECRET} since it may include update snippets.
    """
    if not settings.cron_secret or secret != settings.cron_secret:
        raise HTTPException(status_code=401, detail="invalid or missing secret")
    from app.telegram.handlers import get_recent_errors

    return {"errors": get_recent_errors()}


@app.get(
    "/ops/scan-duplicates",
    tags=["ops"],
    summary="One-off scan of activity history for possible duplicates + alert affected users",
)
async def ops_scan_duplicates(secret: str = "", dry_run: bool = True):
    """Historical backfill counterpart to the live duplicate check in
    tasks.py — scans every activity already in the DB (not just new ones)
    and DMs each affected athlete once per matched pair.

    Protected by: ?secret={CRON_SECRET} query parameter
    Defaults to dry_run=true (no messages sent) — call with dry_run=false
    to actually send the alerts.
    """
    if not settings.cron_secret or secret != settings.cron_secret:
        raise HTTPException(status_code=401, detail="invalid or missing secret")
    from app.tasks import scan_and_alert_duplicates

    return await scan_and_alert_duplicates(dry_run=dry_run)


@app.get(
    "/ops/send-recap",
    tags=["ops"],
    summary="Manually (re)send a monthly or yearly recap DM to one user, for any period",
)
async def ops_send_recap(
    secret: str = "",
    name: str = "",
    year: int | None = None,
    month: int | None = None,
    yearly: bool = False,
):
    """Ad-hoc trigger for testing/comparing recap output — bypasses the
    "current period only" restriction that /recap and /yearrecap have, so
    you can push e.g. July's recap to a specific person on demand.

    ?name= matches (case-insensitively) against either Telegram first name
    or Strava athlete name; the first match is used. ?year=/&month= select
    the period (defaults to the current UTC year/month); pass &yearly=true
    for a yearly recap (month is then ignored).

    Protected by: ?secret={CRON_SECRET} query parameter
    """
    if not settings.cron_secret or secret != settings.cron_secret:
        raise HTTPException(status_code=401, detail="invalid or missing secret")
    if not name:
        raise HTTPException(status_code=400, detail="?name= is required")

    from datetime import datetime, timezone

    from sqlalchemy import select
    from telegram import Bot as TelegramBot

    from app.database import AsyncSessionLocal
    from app.models import User
    from app.stats.recap import get_or_build_recap, get_or_build_yearly_recap
    from app.telegram.keyboards import recap_goal_prompt_keyboard

    now = datetime.now(timezone.utc)
    year = year or now.year
    month = month or now.month

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.strava_athlete_id.isnot(None)))
        users = result.scalars().all()

    needle = name.lower()
    match = next(
        (
            u for u in users
            if needle in (u.telegram_first_name or "").lower()
            or needle in (u.strava_athlete_name or "").lower()
        ),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail=f"no connected user matching name={name!r}")

    async with AsyncSessionLocal() as db:
        user = await db.get(User, match.id)
        if yearly:
            text = await get_or_build_yearly_recap(db, user, year)
        else:
            text = await get_or_build_recap(db, user, year, month)

    from app.telegram.notifications import send_recap_message

    bot = TelegramBot(token=settings.telegram_bot_token)
    async with bot:
        await send_recap_message(
            bot,
            user.telegram_user_id,
            text,
            reply_markup=recap_goal_prompt_keyboard(),
        )

    return {
        "sent": True,
        "matched_user": user.telegram_first_name,
        "telegram_user_id": user.telegram_user_id,
        "period": {"year": year} if yearly else {"year": year, "month": month},
    }


@app.get(
    "/ops/compare-stats",
    tags=["ops"],
    summary="Compare Postgres-computed activity totals against Strava's live /athlete/activities feed",
)
async def ops_compare_stats(secret: str = "", names: str = ""):
    """Diagnostic: for each matching user, sums every stored `Activity` row
    per sport and compares it against a fresh pull straight from Strava's
    `/athlete/activities` (not the pre-aggregated `/athlete/stats` endpoint
    — see fetch_athlete_stats' docstring in app/strava/client.py for why
    the bot doesn't use that one for anything user-facing). Surfaces any
    activity present on one side but not the other (id-level diff), plus
    per-sport count/distance/elevation/time mismatches.

    Must run in this environment (not locally) since it needs the
    deployed ENCRYPTION_KEY to decrypt each user's stored Strava tokens.

    ?names=Dileep,Manoj,Ganesh — comma-separated, case-insensitive
    substring match against telegram_first_name / strava_athlete_name /
    telegram_username. Each name may match more than one account (e.g. two
    "Manoj"s) — all matches are reported.

    Protected by: ?secret={CRON_SECRET} query parameter
    """
    if not settings.cron_secret or secret != settings.cron_secret:
        raise HTTPException(status_code=401, detail="invalid or missing secret")
    if not names:
        raise HTTPException(status_code=400, detail="?names= is required (comma-separated)")

    from collections import defaultdict

    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models import Activity, User
    from app.strava.auth import get_valid_access_token
    from app.strava.client import fetch_activities

    def _totals(rows_or_dicts, is_pg: bool) -> dict:
        totals: dict = defaultdict(lambda: {"count": 0, "distance_m": 0.0, "elev_m": 0.0, "moving_s": 0})
        for a in rows_or_dicts:
            if is_pg:
                sport = a.activity_type
                d, e, m = a.distance_meters or 0.0, a.elevation_gain or 0.0, a.moving_time_seconds or 0
            else:
                sport = a.get("sport_type") or a.get("type") or "Unknown"
                d, e, m = a.get("distance") or 0.0, a.get("total_elevation_gain") or 0.0, a.get("moving_time") or 0
            t = totals[sport]
            t["count"] += 1
            t["distance_m"] += d
            t["elev_m"] += e
            t["moving_s"] += m
        return totals

    report = []
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User))
        all_users = result.scalars().all()

        for name in [n.strip() for n in names.split(",") if n.strip()]:
            needle = name.lower()
            matches = [
                u for u in all_users
                if needle in (u.telegram_first_name or "").lower()
                or needle in (u.strava_athlete_name or "").lower()
                or needle in (u.telegram_username or "").lower()
            ]
            if not matches:
                report.append({"name": name, "error": "no matching user found"})
                continue

            for user in matches:
                entry = {
                    "name": name,
                    "telegram_first_name": user.telegram_first_name,
                    "strava_athlete_name": user.strava_athlete_name,
                    "strava_athlete_id": user.strava_athlete_id,
                }
                if not user.is_strava_connected:
                    entry["error"] = "not connected to Strava"
                    report.append(entry)
                    continue

                pg_rows = (await db.execute(
                    select(Activity).where(Activity.user_id == user.id)
                )).scalars().all()

                try:
                    token = await get_valid_access_token(db, user)
                    sv_rows = await fetch_activities(token)
                except Exception as exc:
                    entry["error"] = f"Strava fetch failed: {exc!r}"
                    report.append(entry)
                    continue

                pg_ids = {a.strava_activity_id for a in pg_rows}
                sv_ids = {a["id"] for a in sv_rows}
                pg_totals = _totals(pg_rows, is_pg=True)
                sv_totals = _totals(sv_rows, is_pg=False)
                sv_by_id = {a["id"]: a for a in sv_rows}
                pg_by_id = {a.strava_activity_id: a for a in pg_rows}

                # Activities present on both sides but whose sport type
                # disagrees — e.g. the athlete reclassified a ride on
                # Strava (GravelRide -> Ride) after it was originally
                # synced, and no "update" webhook ever reached us to pick
                # up the change.
                reclassified = []
                for i in sorted(pg_ids & sv_ids):
                    pg_type = pg_by_id[i].activity_type
                    sv_type = sv_by_id[i].get("sport_type") or sv_by_id[i].get("type")
                    if pg_type != sv_type:
                        reclassified.append({
                            "id": i, "name": pg_by_id[i].activity_name,
                            "postgres_sport_type": pg_type, "strava_sport_type": sv_type,
                        })

                per_sport = []
                for sport in sorted(set(pg_totals) | set(sv_totals)):
                    p = pg_totals.get(sport, {"count": 0, "distance_m": 0.0, "elev_m": 0.0, "moving_s": 0})
                    s = sv_totals.get(sport, {"count": 0, "distance_m": 0.0, "elev_m": 0.0, "moving_s": 0})
                    match = (
                        p["count"] == s["count"]
                        and abs(p["distance_m"] - s["distance_m"]) < 5
                        and abs(p["moving_s"] - s["moving_s"]) < 5
                    )
                    per_sport.append({
                        "sport": sport,
                        "postgres": p,
                        "strava": s,
                        "match": match,
                    })

                entry.update({
                    "postgres_activity_count": len(pg_ids),
                    "strava_activity_count": len(sv_ids),
                    "on_strava_not_in_postgres": [
                        {
                            "id": i, "name": sv_by_id[i].get("name"),
                            "sport_type": sv_by_id[i].get("sport_type") or sv_by_id[i].get("type"),
                            "start_date": sv_by_id[i].get("start_date"),
                            "distance_m": sv_by_id[i].get("distance"),
                        }
                        for i in sorted(sv_ids - pg_ids)
                    ],
                    "in_postgres_not_on_strava": [
                        {
                            "id": i, "name": pg_by_id[i].activity_name,
                            "sport_type": pg_by_id[i].activity_type,
                            "start_date": pg_by_id[i].activity_date.isoformat(),
                            "distance_m": pg_by_id[i].distance_meters,
                        }
                        for i in sorted(pg_ids - sv_ids)
                    ],
                    "reclassified_sport_type": reclassified,
                    "per_sport": per_sport,
                })
                report.append(entry)

    return {"report": report}


@app.get("/telegram/status", tags=["ops"], summary="Telegram's own view of webhook delivery health")
async def telegram_status():
    """Surfaces Telegram's getWebhookInfo so we can tell an app outage
    (Telegram couldn't deliver — pending_update_count grows, last_error_message
    set) apart from an in-handler exception (Telegram sees 200 OK every time,
    since telegram_webhook() always returns 200 even when handle_error() fires).
    """
    from app.telegram.bot import get_application

    info = await get_application().bot.get_webhook_info()
    return {
        "url": info.url,
        "pending_update_count": info.pending_update_count,
        "last_error_date": info.last_error_date.isoformat() if info.last_error_date else None,
        "last_error_message": info.last_error_message,
        "last_synchronization_error_date": (
            info.last_synchronization_error_date.isoformat()
            if info.last_synchronization_error_date else None
        ),
        "max_connections": info.max_connections,
    }


@app.get("/version", tags=["ops"], summary="Deployed git commit — for confirming a deploy actually landed")
async def version():
    import subprocess
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=".", stderr=subprocess.DEVNULL
        ).decode().strip()
        msg = subprocess.check_output(
            ["git", "log", "-1", "--format=%s"], cwd=".", stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception as exc:
        return {"error": str(exc)}
    return {"commit": sha, "message": msg}


@app.api_route("/ping", methods=["GET", "HEAD"], tags=["ops"], summary="Keep-alive ping — zero DB/Redis touch")
async def ping():
    """Instant 200 response used by UptimeRobot to prevent Render sleep.

    Response never waits on DB/Redis — the heartbeat write (used by
    catchup_sync_all_users() to detect real outages vs. quiet nights) is
    dispatched fire-and-forget so a slow Redis round-trip can never delay
    this response, even on a cold-start path.
    """
    from app.strava.webhook import touch_heartbeat
    from app.tasks import fire_and_forget
    fire_and_forget(touch_heartbeat())
    return {"status": "ok"}


# Cache DB health result for 30 s so Render's health probe doesn't
# hammer Neon Postgres on every single check.
_health_cache: dict = {"db": True, "ts": 0.0}
_HEALTH_CACHE_TTL = 30.0   # seconds


@app.get("/health", tags=["ops"], summary="Liveness probe with DB check")
async def health():
    """Return process + database health with a 30-second cached DB check.

    Redis is NOT checked here — it was verified at startup and its
    single connection is maintained by the pool.
    """
    now = time.monotonic()
    if now - _health_cache["ts"] > _HEALTH_CACHE_TTL:
        _health_cache["db"] = await check_db_connection()
        _health_cache["ts"] = now

    return {
        "status": "ok",
        "db":     "ok" if _health_cache["db"] else "error",
        "env":    settings.app_env,
    }
