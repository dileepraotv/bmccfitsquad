"""python-telegram-bot application setup.

The module owns the single global PTB ``Application`` instance used by the
FastAPI web process.  Celery workers send messages directly through
``telegram.Bot`` without going through this Application.

Webhook registration
--------------------
``setup_bot()`` always calls ``setWebhook`` on startup (both production and
development).  In development, point ``BASE_URL`` to your ngrok / cloudflared
tunnel URL.  To use polling locally instead, set ``APP_ENV=polling``.

Route
-----
  POST /telegram/webhook  — Telegram delivers updates here.
                            Guarded by X-Telegram-Bot-Api-Secret-Token header.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request, Response
from telegram import Update
from telegram.ext import Application, ApplicationBuilder

from app.config import get_settings
from app.telegram.handlers import register_handlers

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

_application: Application | None = None


# ---------------------------------------------------------------------------
# Application accessor
# ---------------------------------------------------------------------------

def get_application() -> Application:
    """Return the global PTB Application (raises if ``setup_bot`` not yet called)."""
    if _application is None:
        raise RuntimeError("Telegram bot not initialised — call setup_bot() first")
    return _application


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def setup_bot() -> None:
    """Initialise the PTB Application and register the Telegram webhook.

    Behaviour by APP_ENV:
      - ``production`` or any other value: set webhook via Telegram API
      - ``polling``: delete webhook so you can run ``bot.run_polling()`` locally
    """
    global _application

    _application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .build()
    )

    register_handlers(_application)

    await _application.initialize()
    await _application.start()

    if settings.app_env.lower() == "polling":
        # Local development without a public URL — clear any existing webhook
        await _application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Polling mode — existing Telegram webhook deleted")
        return

    # ------------------------------------------------------------------
    # Register webhook with the Telegram Bot API
    # POST https://api.telegram.org/bot{TOKEN}/setWebhook
    # ------------------------------------------------------------------
    webhook_url = settings.telegram_webhook_url   # e.g. https://myapp.up.railway.app/telegram/webhook

    await _application.bot.set_webhook(
        url=webhook_url,
        secret_token=settings.telegram_webhook_secret,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,          # discard updates received while offline
    )

    # Confirm registration by querying getWebhookInfo
    info = await _application.bot.get_webhook_info()
    if info.url == webhook_url:
        logger.info(
            "Telegram webhook registered: url=%s pending_updates=%s",
            info.url,
            info.pending_update_count,
        )
    else:
        logger.warning(
            "Telegram webhook URL mismatch after setWebhook: expected=%s got=%s",
            webhook_url,
            info.url,
        )


async def teardown_bot() -> None:
    """Gracefully stop the PTB Application on server shutdown."""
    global _application
    if _application is not None:
        await _application.stop()
        await _application.shutdown()
        _application = None
        logger.info("Telegram bot stopped")


# ---------------------------------------------------------------------------
# FastAPI route — POST /telegram/webhook
# ---------------------------------------------------------------------------

@router.post("/webhook", summary="Receive Telegram updates")
async def telegram_webhook(request: Request) -> Response:
    """Receive an update from Telegram and hand it to PTB for dispatch.

    Telegram sends the secret token set during ``setWebhook`` in the
    ``X-Telegram-Bot-Api-Secret-Token`` header.  Requests without a
    matching token are rejected with 403.
    """
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != settings.telegram_webhook_secret:
        logger.warning("Telegram webhook received with wrong secret token")
        return Response(status_code=403)

    payload = await request.json()
    update = Update.de_json(payload, get_application().bot)
    await get_application().process_update(update)
    return Response(status_code=200)
