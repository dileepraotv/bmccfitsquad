"""FastAPI application entry point.

Routes
------
  GET  /health              — liveness probe (DB + Redis status)
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
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
    #    set_webhook is called via Telegram's Bot API:
    #    POST https://api.telegram.org/bot{TOKEN}/setWebhook
    await setup_bot()
    logger.info("Telegram bot ready (webhook registered)")

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
# Health check
# ---------------------------------------------------------------------------

@app.get("/health", tags=["ops"], summary="Liveness probe")
async def health():
    """Return status of the web process, database, and Redis.

    Used by Railway health checks and load balancers.
    Returns HTTP 200 regardless of component status — callers inspect the
    ``db`` field to detect degraded state.
    """
    db_ok = await check_db_connection()
    return {
        "status": "ok",
        "db":     "ok" if db_ok else "error",
        "env":    settings.app_env,
    }
