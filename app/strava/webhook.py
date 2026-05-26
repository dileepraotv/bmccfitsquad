"""Strava webhook event processing and OAuth callback.

Strava webhook protocol
-----------------------
1. Subscription: POST /push_subscriptions → Strava GETs the callback URL with a
   hub.challenge; we echo it back to confirm ownership.
2. Events: Strava POSTs JSON to the callback URL within seconds of each event.
   We MUST respond with HTTP 200 within 2 seconds — all heavy work is
   dispatched to Celery immediately.

OAuth callback
--------------
After a user approves on the Strava website they are redirected here with
?code=…&state=… in the query string.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import Activity, User
from app.redis_client import get_redis, key_activity_seen
from app.strava.auth import (
    exchange_code_for_tokens,
    get_valid_access_token,
    save_tokens,
    validate_oauth_state,
)
from app.strava.client import fetch_activity_detail

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

# How long to keep deduplication keys in Redis (24 hours)
_DEDUP_TTL_SECONDS = 86_400


# ---------------------------------------------------------------------------
# Webhook verification — GET /strava/webhook
# ---------------------------------------------------------------------------

@router.get("/webhook", summary="Strava webhook challenge verification")
async def strava_webhook_verify(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
):
    """Respond to Strava's hub challenge when a subscription is created.

    Strava sends:
        GET /strava/webhook?hub.mode=subscribe&hub.verify_token=…&hub.challenge=…

    We validate the verify_token and echo the challenge back.
    """
    if hub_mode != "subscribe":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unexpected hub.mode: {hub_mode!r}",
        )
    if hub_verify_token != settings.strava_webhook_verify_token:
        logger.warning("Strava webhook verification failed — wrong verify_token")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid verify token",
        )
    logger.info("Strava webhook challenge verified successfully")
    return {"hub.challenge": hub_challenge}


# ---------------------------------------------------------------------------
# Webhook event receiver — POST /strava/webhook
# ---------------------------------------------------------------------------

@router.post(
    "/webhook",
    status_code=status.HTTP_200_OK,
    summary="Receive Strava activity events",
)
async def strava_webhook_event(request: Request, db: AsyncSession = Depends(get_db)):
    """Receive a Strava event, deduplicate, and dispatch to a Celery worker.

    Strava expects HTTP 200 within 2 seconds — we ack immediately and process
    asynchronously.  Supported event types:

    - ``activity / create``  → fetch detail, store, send Telegram notification
    - ``activity / update``  → update the stored Activity row
    - ``activity / delete``  → remove the Activity row
    - ``athlete / update``   → handle deauthorisation
    """
    payload = await request.json()
    logger.debug("Strava webhook payload received: %s", payload)

    aspect_type: str = payload.get("aspect_type", "")   # create / update / delete
    object_type: str = payload.get("object_type", "")   # activity / athlete
    object_id: int = int(payload.get("object_id", 0))
    owner_id: int = int(payload.get("owner_id", 0))
    updates: dict = payload.get("updates", {})

    if object_type == "activity":
        if aspect_type == "create":
            await _handle_activity_created(db, owner_id=owner_id, activity_id=object_id)
        elif aspect_type == "update":
            await _handle_activity_updated(db, activity_id=object_id, updates=updates)
        elif aspect_type == "delete":
            await _handle_activity_deleted(db, activity_id=object_id)
    elif object_type == "athlete" and aspect_type == "update":
        await _handle_athlete_updated(db, athlete_id=owner_id, updates=updates)

    # Always return 200 quickly — Strava will retry on any other status code
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# OAuth callback — GET /strava/callback
# ---------------------------------------------------------------------------

@router.get("/callback", response_class=HTMLResponse, summary="Strava OAuth redirect handler")
async def strava_oauth_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    scope: str = Query(default=""),
):
    """Handle the OAuth redirect from Strava after the user approves access.

    Query parameters (set by Strava):
        code:  Short-lived authorisation code to exchange for tokens.
        state: The state value we generated — used to look up the Telegram user.
        error: Set to ``access_denied`` if the user rejected the request.
        scope: Comma-separated list of granted scopes.
    """
    # ------------------------------------------------------------------
    # User declined — show a polite rejection page
    # ------------------------------------------------------------------
    if error:
        logger.info("Strava OAuth declined: error=%s state=%s", error, state)
        return HTMLResponse(content=_html_page(
            title="Connection Cancelled",
            body="You cancelled the Strava authorisation. "
                 "Send <b>/connect</b> in the BMCC bot to try again.",
            success=False,
        ))

    # ------------------------------------------------------------------
    # Validate state → resolve Telegram user ID
    # ------------------------------------------------------------------
    if not state or not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing code or state parameter",
        )

    telegram_user_id = await validate_oauth_state(state)
    if telegram_user_id is None:
        return HTMLResponse(
            status_code=400,
            content=_html_page(
                title="Link Expired",
                body="This authorisation link has expired or already been used. "
                     "Send <b>/connect</b> in the BMCC bot to get a fresh link.",
                success=False,
            ),
        )

    # ------------------------------------------------------------------
    # Exchange code for tokens
    # ------------------------------------------------------------------
    try:
        token_data = await exchange_code_for_tokens(code)
    except Exception as exc:
        logger.exception("Token exchange failed for telegram_user_id=%s", telegram_user_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to exchange authorisation code with Strava.",
        ) from exc

    athlete: dict = token_data.get("athlete", {})
    strava_athlete_id: int = athlete.get("id", 0)
    athlete_firstname: str = athlete.get("firstname", "")
    athlete_lastname: str = athlete.get("lastname", "")
    athlete_name = f"{athlete_firstname} {athlete_lastname}".strip()

    # ------------------------------------------------------------------
    # Look up the user in our database (must exist from /start)
    # ------------------------------------------------------------------
    result = await db.execute(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )
    user: User | None = result.scalar_one_or_none()

    if user is None:
        logger.error(
            "OAuth callback: no User row for telegram_user_id=%s — was /start called?",
            telegram_user_id,
        )
        return HTMLResponse(
            status_code=404,
            content=_html_page(
                title="Account Not Found",
                body="We couldn't find your BMCC account. "
                     "Please open the bot and send <b>/start</b> first, "
                     "then use <b>/connect</b> to link Strava.",
                success=False,
            ),
        )

    # ------------------------------------------------------------------
    # Persist the Strava identity and encrypted tokens
    # ------------------------------------------------------------------
    user.strava_athlete_id = strava_athlete_id
    user.strava_athlete_name = athlete_name

    await save_tokens(
        db,
        user,
        access_token=token_data["access_token"],
        refresh_token=token_data["refresh_token"],
        expires_at=token_data["expires_at"],
    )
    await db.commit()

    logger.info(
        "Strava connected: telegram_user_id=%s strava_athlete_id=%s name=%r",
        telegram_user_id,
        strava_athlete_id,
        athlete_name,
    )

    # ------------------------------------------------------------------
    # Kick off a background history sync
    # Try Celery first; fall back to running the async function directly
    # in a background FastAPI task so the OAuth redirect returns fast.
    # ------------------------------------------------------------------
    from app.tasks import _sync_user_activities_async
    import asyncio

    user_id_str = str(user.id)

    try:
        from app.tasks import sync_user_activities
        sync_user_activities.delay(user_id=user_id_str)
        logger.info("History sync dispatched to Celery for user_id=%s", user_id_str)
    except Exception:
        logger.warning(
            "Celery not available — running history sync inline for user_id=%s", user_id_str
        )
        asyncio.ensure_future(_sync_user_activities_async(user_id=user_id_str))

    # ------------------------------------------------------------------
    # Notify the user in Telegram
    # ------------------------------------------------------------------
    try:
        from app.telegram.bot import get_application
        bot = get_application().bot
        await bot.send_message(
            chat_id=telegram_user_id,
            text=(
                f"🎉 *Welcome, {athlete_firstname}\\!* 🚴🏃🏊🚶\n\n"
                f"✅ Your Strava account has been successfully connected\\.\n\n"
                f"You've taken a step in the right direction toward achieving your fitness goals\\!\n\n"
                f"Here's how to get started:\n\n"
                f"📊 Use /stats to see your activity numbers\n"
                f"🎯 Use /goals to set a new challenge\n"
                f"❓ Use /help to explore all available features"
            ),
            parse_mode="MarkdownV2",
        )
    except Exception as exc:
        # Non-fatal — the web response is more important
        logger.warning("Could not send Telegram confirmation to %s: %s", telegram_user_id, exc)

    return HTMLResponse(content=_html_page(
        title="Strava Connected!",
        body=f"<strong>{athlete_firstname}</strong>, your Strava account is now linked "
             f"to the BMCC bot. You can close this tab and return to Telegram.",
        success=True,
    ))


# ---------------------------------------------------------------------------
# Internal event handlers
# ---------------------------------------------------------------------------

async def _handle_activity_created(db: AsyncSession, owner_id: int, activity_id: int) -> None:
    """Fetch, persist, then dispatch a Celery notification task for a new activity.

    Pipeline (all async, keeps total latency well under Strava's 2-second limit):
      1. Redis SETNX dedup  — set early so Strava retries are silently dropped
      2. User lookup        — abort if the athlete isn't in our DB
      3. Token refresh      — transparent, uses cached token when possible
      4. Strava API fetch   — full activity detail (~200–400 ms)
      5. DB upsert          — ON CONFLICT DO NOTHING for safety
      6. Celery dispatch    — send_activity_notification.delay(...)
    """
    # ------------------------------------------------------------------
    # 1. Dedup — set the key before any network calls so that concurrent
    #    Strava retries are ignored even if this handler is still running
    # ------------------------------------------------------------------
    redis = await get_redis()
    dedup_key = key_activity_seen(activity_id)
    is_new = await redis.set(dedup_key, "1", ex=_DEDUP_TTL_SECONDS, nx=True)
    if not is_new:
        logger.info("Activity strava_id=%s already queued — duplicate ignored", activity_id)
        return

    # ------------------------------------------------------------------
    # 2. Look up the user by their Strava athlete ID
    # ------------------------------------------------------------------
    result = await db.execute(
        select(User).where(User.strava_athlete_id == owner_id)
    )
    user: User | None = result.scalar_one_or_none()

    if user is None:
        logger.warning(
            "No user found for strava_athlete_id=%s — activity %s skipped",
            owner_id, activity_id,
        )
        await redis.delete(dedup_key)   # allow future re-processing if user registers later
        return

    if not user.is_active:
        logger.info(
            "User telegram_id=%s is inactive — activity %s skipped",
            user.telegram_user_id, activity_id,
        )
        return

    # ------------------------------------------------------------------
    # 3. Obtain a valid (possibly refreshed) Strava access token
    # ------------------------------------------------------------------
    try:
        access_token = await get_valid_access_token(db, user)
    except ValueError as exc:
        logger.warning(
            "No access token for telegram_id=%s: %s — activity %s skipped",
            user.telegram_user_id, exc, activity_id,
        )
        return

    # ------------------------------------------------------------------
    # 4. Fetch full activity detail from the Strava API
    # ------------------------------------------------------------------
    try:
        activity_data = await fetch_activity_detail(access_token, activity_id)
    except Exception as exc:
        logger.error(
            "Strava API fetch failed for activity_id=%s: %s", activity_id, exc
        )
        await redis.delete(dedup_key)   # allow retry on next webhook delivery
        raise

    # ------------------------------------------------------------------
    # 5. Upsert the activity into the database
    # ------------------------------------------------------------------
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    activity_date = _parse_strava_date(
        activity_data.get("start_date") or activity_data.get("start_date_local")
    )
    is_indoor = (
        bool(activity_data.get("trainer", False))
        or str(activity_data.get("type", "")).startswith("Virtual")
    )

    stmt = (
        pg_insert(Activity)
        .values(
            strava_activity_id=activity_id,
            user_id=user.id,
            activity_name=activity_data.get("name") or "Unnamed Activity",
            activity_type=activity_data.get("type") or "Unknown",
            activity_date=activity_date,
            distance_meters=float(activity_data.get("distance") or 0),
            moving_time_seconds=int(activity_data.get("moving_time") or 0),
            elapsed_time_seconds=int(activity_data.get("elapsed_time") or 0),
            elevation_gain=float(activity_data.get("total_elevation_gain") or 0),
            average_speed=float(activity_data.get("average_speed") or 0),
            max_speed=float(activity_data.get("max_speed") or 0),
            average_heartrate=_optional_float(activity_data.get("average_heartrate")),
            max_heartrate=_optional_float(activity_data.get("max_heartrate")),
            calories=_optional_float(activity_data.get("calories")),
            is_indoor=is_indoor,
        )
        .on_conflict_do_nothing(index_elements=["strava_activity_id"])
    )
    await db.execute(stmt)
    await db.flush()

    # ------------------------------------------------------------------
    # 6. Dispatch the notification Celery task
    # ------------------------------------------------------------------
    from app.tasks import send_activity_notification
    send_activity_notification.delay(
        activity_data=activity_data,
        user_id=str(user.id),
    )
    logger.info(
        "send_activity_notification dispatched: strava_id=%s user_id=%s",
        activity_id, user.id,
    )


async def _handle_activity_updated(db: AsyncSession, activity_id: int, updates: dict) -> None:
    """Apply an activity update from Strava to the local database row.

    The ``updates`` dict keys mirror Strava field names:
      - ``title``   → activity_name
      - ``type``    → activity_type
      - ``private`` → (ignored — we don't store privacy flag)
    """
    if not updates:
        return

    result = await db.execute(
        select(Activity).where(Activity.strava_activity_id == activity_id)
    )
    activity: Activity | None = result.scalar_one_or_none()
    if activity is None:
        logger.debug(
            "Activity strava_id=%s not in DB — update event ignored", activity_id
        )
        return

    if "title" in updates:
        activity.activity_name = updates["title"]
    if "type" in updates:
        activity.activity_type = updates["type"]

    await db.flush()
    logger.info("Activity strava_id=%s updated: %s", activity_id, updates)


async def _handle_activity_deleted(db: AsyncSession, activity_id: int) -> None:
    """Remove a deleted Strava activity from the local database."""
    result = await db.execute(
        select(Activity).where(Activity.strava_activity_id == activity_id)
    )
    activity: Activity | None = result.scalar_one_or_none()
    if activity is None:
        logger.debug(
            "Activity strava_id=%s not in DB — delete event ignored", activity_id
        )
        return

    await db.delete(activity)
    await db.flush()

    # Clean up deduplication key so a re-upload of the same activity is processed fresh
    redis = await get_redis()
    await redis.delete(key_activity_seen(activity_id))

    logger.info("Activity strava_id=%s deleted from DB", activity_id)


async def _handle_athlete_updated(db: AsyncSession, athlete_id: int, updates: dict) -> None:
    """Handle an athlete-level update event, primarily deauthorisation.

    When a user revokes access in the Strava app, Strava sends:
        { "updates": { "authorized": "false" } }

    We null out all Strava token fields so future calls fail gracefully.
    """
    authorized: str = updates.get("authorized", "true")
    if authorized != "false":
        return

    result = await db.execute(
        select(User).where(User.strava_athlete_id == athlete_id)
    )
    user: User | None = result.scalar_one_or_none()
    if user is None:
        logger.warning(
            "Deauthorisation event for unknown strava_athlete_id=%s", athlete_id
        )
        return

    # Null out tokens — we can't call the Strava API any more for this user
    user.strava_access_token = None
    user.strava_refresh_token = None
    user.strava_token_expires_at = None
    # Keep strava_athlete_id and strava_athlete_name for display purposes
    await db.flush()

    logger.info(
        "Strava deauthorised for telegram_user_id=%s strava_athlete_id=%s",
        user.telegram_user_id,
        athlete_id,
    )

    # Inform the user in Telegram
    try:
        from app.telegram.bot import get_application
        bot = get_application().bot
        await bot.send_message(
            chat_id=user.telegram_user_id,
            text=(
                "⚠️ Your Strava access has been revoked.\n\n"
                "If this was unintentional, use /connect to re-link your account."
            ),
        )
    except Exception as exc:
        logger.warning(
            "Could not notify telegram_user_id=%s of deauthorisation: %s",
            user.telegram_user_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Webhook-internal helpers
# ---------------------------------------------------------------------------

from datetime import datetime, timezone as _tz


def _parse_strava_date(date_str: str | None) -> datetime:
    if not date_str:
        return datetime.now(_tz.utc)
    return datetime.fromisoformat(date_str.rstrip("Z")).replace(tzinfo=_tz.utc)


def _optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _html_page(title: str, body: str, success: bool = True) -> str:
    """Return a minimal self-contained HTML page for OAuth redirect responses."""
    accent = "#fc4c02" if success else "#e53e3e"   # Strava orange or red
    icon = "✅" if success else "❌"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — BMCC Bot</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f7f7f7;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 1rem;
    }}
    .card {{
      background: #fff;
      border-radius: 12px;
      box-shadow: 0 4px 24px rgba(0,0,0,.08);
      max-width: 420px;
      width: 100%;
      padding: 2.5rem 2rem;
      text-align: center;
    }}
    .icon {{ font-size: 3rem; margin-bottom: 1rem; }}
    h1 {{ font-size: 1.5rem; color: {accent}; margin-bottom: .75rem; }}
    p {{ color: #555; line-height: 1.6; }}
    .brand {{
      margin-top: 2rem;
      font-size: .75rem;
      color: #aaa;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>{title}</h1>
    <p>{body}</p>
    <p class="brand">Beyond Miles Cycling Club &mdash; BMCC Bot</p>
  </div>
</body>
</html>"""
