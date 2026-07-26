"""Strava webhook event processing and OAuth callback.

Strava webhook protocol
-----------------------
1. Subscription: POST /push_subscriptions → Strava GETs the callback URL with a
   hub.challenge; we echo it back to confirm ownership.
2. Events: Strava POSTs JSON to the callback URL within seconds of each event.
   We MUST respond with HTTP 200 within 2 seconds.

Durable ack/process split
--------------------------
Rather than doing the real work (token refresh, Strava fetch, DB write,
Telegram notify) inline before acking — where a Render cold start or a
mid-flight exception can silently lose the event forever — every event is
first persisted as a WebhookEvent row (a fast single INSERT) and only then
acked. The actual processing is dispatched via fire_and_forget afterwards.
If processing fails or the process dies mid-flight, the row is simply left
with processed_at=NULL and the next cron tick's repair pass
(process_pending_webhook_events() in tasks.py) retries it — no per-user
blind Strava polling required to recover a missed event.

OAuth callback
--------------
After a user approves on the Strava website they are redirected here with
?code=…&state=… in the query string.
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.config import get_settings
from app.database import AsyncSessionLocal, get_db
from app.models import Activity, User, WebhookEvent
from app.redis_client import get_redis, key_activity_seen
from app.strava.auth import (
    exchange_code_for_tokens,
    get_valid_access_token,
    save_tokens,
    validate_oauth_state,
)
from app.strava.client import fetch_activity_detail, view_webhook_subscription

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
# Webhook subscription status — GET /strava/webhook/status
# ---------------------------------------------------------------------------

@router.get("/webhook/status", summary="Check active Strava webhook subscription")
async def strava_webhook_status():
    """Return the active Strava webhook subscription(s) for this app.

    Useful to verify that the subscription is pointing to the correct callback
    URL after a service URL change or Railway redeploy.
    """
    try:
        subscriptions = await view_webhook_subscription()
    except Exception as exc:
        logger.error("Failed to fetch webhook subscriptions: %s", exc)
        return {"status": "error", "detail": str(exc), "subscriptions": []}

    expected_url = settings.strava_webhook_callback_url
    return {
        "status": "ok",
        "count": len(subscriptions),
        "subscriptions": subscriptions,
        "expected_callback_url": expected_url,
        "webhook_registered": any(
            s.get("callback_url") == expected_url for s in subscriptions
        ),
    }


# ---------------------------------------------------------------------------
# Webhook event receiver — POST /strava/webhook
# ---------------------------------------------------------------------------

@router.post(
    "/webhook",
    status_code=status.HTTP_200_OK,
    summary="Receive Strava activity events",
)
async def strava_webhook_event(request: Request):
    """Durably persist a Strava event, ack, then process in the background.

    1. INSERT a WebhookEvent row (processed_at=NULL) — a single fast write,
       comfortably inside Strava's 2-second ack window.
    2. Return HTTP 200 immediately.
    3. fire_and_forget the real processing — if it fails or the process is
       killed mid-flight, the row stays unprocessed and is retried by the
       next cron tick's repair pass instead of being lost forever.

    Supported event types:
    - ``activity / create``  → fetch detail, store, send Telegram notification
    - ``activity / update``  → update the stored Activity row
    - ``activity / delete``  → remove the Activity row
    - ``athlete / update``   → handle deauthorisation
    """
    from app.tasks import fire_and_forget
    payload = await request.json()
    logger.debug("Strava webhook payload received: %s", payload)

    aspect_type: str = payload.get("aspect_type", "")
    object_type: str = payload.get("object_type", "")
    object_id: int = int(payload.get("object_id", 0))
    owner_id: int = int(payload.get("owner_id", 0))
    updates: dict = payload.get("updates", {})

    if object_type == "activity" and aspect_type not in ("create", "update", "delete"):
        return {"status": "ok"}
    if object_type == "athlete" and not (aspect_type == "update" and "authorized" in updates):
        return {"status": "ok"}
    if object_type not in ("activity", "athlete"):
        return {"status": "ok"}

    event_id: str | None = None
    async with AsyncSessionLocal() as db:
        event = WebhookEvent(
            object_type=object_type,
            aspect_type=aspect_type,
            object_id=object_id,
            owner_id=owner_id,
            updates_json=json.dumps(updates) if updates else None,
        )
        db.add(event)
        await db.commit()
        event_id = str(event.id)

    # Heartbeat: proves the app was reachable and processing right now —
    # used by catchup_sync_all_users() to distinguish "quiet night, nothing
    # to sync" from "app was actually down". Non-blocking, best-effort.
    fire_and_forget(touch_heartbeat())

    fire_and_forget(process_webhook_event(event_id))

    # Always return 200 immediately — Strava will retry on any other status
    return {"status": "ok"}


async def touch_heartbeat() -> None:
    try:
        import time
        from app.redis_client import get_redis, key_heartbeat
        redis = await get_redis()
        await redis.set(key_heartbeat(), str(int(time.time())))
    except Exception:
        logger.debug("Could not update heartbeat", exc_info=True)


async def process_webhook_event(event_id: str) -> None:
    """Process one durably-queued WebhookEvent row and mark it done.

    Called immediately (fire_and_forget) after a fresh webhook POST, and
    again from tasks.process_pending_webhook_events() for any row that was
    left unprocessed by a crash/restart/transient error. Safe to call twice
    for the same row — the downstream handlers are already idempotent
    (Redis SETNX dedup, ON CONFLICT DO NOTHING upserts).
    """
    async with AsyncSessionLocal() as db:
        event = await db.get(WebhookEvent, uuid.UUID(event_id))
        if event is None:
            return
        if event.processed_at is not None:
            return  # already handled (e.g. a duplicate repair pass)

        updates: dict = json.loads(event.updates_json) if event.updates_json else {}

        try:
            if event.object_type == "activity":
                if event.aspect_type == "create":
                    await _handle_activity_created(owner_id=event.owner_id, activity_id=event.object_id)
                elif event.aspect_type == "update":
                    await _handle_activity_updated(activity_id=event.object_id, updates=updates)
                elif event.aspect_type == "delete":
                    await _handle_activity_deleted(activity_id=event.object_id)
            elif event.object_type == "athlete" and event.aspect_type == "update":
                await _handle_athlete_updated(athlete_id=event.owner_id, updates=updates)

            event.processed_at = _utc_now()
            event.last_error = None
        except Exception as exc:
            event.attempts += 1
            event.last_error = str(exc)[:500]
            logger.exception(
                "process_webhook_event failed id=%s type=%s/%s attempts=%s",
                event_id, event.object_type, event.aspect_type, event.attempts,
            )
            # Leave processed_at NULL — repair pass will retry later.
        finally:
            await db.commit()


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
    # Fire full history sync in the background — returns immediately
    # ------------------------------------------------------------------
    from app.tasks import fire_and_forget, sync_user_activities

    user_id_str = str(user.id)
    fire_and_forget(sync_user_activities(user_id=user_id_str, full=True))
    logger.info("Full history sync scheduled for user_id=%s", user_id_str)

    # ------------------------------------------------------------------
    # Notify the user in Telegram
    # ------------------------------------------------------------------
    try:
        from app.telegram.bot import get_application
        from app.telegram.keyboards import nav_keyboard
        bot = get_application().bot
        # Single message — the persistent nav bar (Stats / Goals / Help) is
        # the one top-level destination menu; no duplicate inline menu.
        await bot.send_message(
            chat_id=telegram_user_id,
            text=(
                f"🎉 *Welcome, {athlete_firstname}\\!* 🚴🏃🏊🚶\n\n"
                f"✅ Your Strava account is connected\\. I'm importing your activity "
                f"history now — this can take a minute for a large history\\.\n\n"
                f"📊 Use /stats to see your activity numbers\n"
                f"🎯 Use /goals to set a new challenge\n"
                f"❓ Use /help to explore all available features"
            ),
            parse_mode="MarkdownV2",
            reply_markup=nav_keyboard(),
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

async def _handle_activity_created(owner_id: int, activity_id: int) -> None:
    """Fetch, persist, and notify for a new Strava activity.

    Runs fully in the background via asyncio.ensure_future — the webhook
    handler has already returned HTTP 200 before this executes.

    Pipeline:
      1. Redis SETNX dedup  — drops duplicate deliveries from Strava
      2. User lookup        — abort if athlete not in our DB
      3. Token refresh      — transparent, uses cached token when possible
      4. Strava API fetch   — full activity detail
      5. DB upsert          — ON CONFLICT DO NOTHING
      6. Notification       — DM athlete + group chats
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.tasks import send_activity_notification

    redis = await get_redis()
    dedup_key = key_activity_seen(activity_id)

    async with AsyncSessionLocal() as db:
        # 1. User lookup
        result = await db.execute(
            select(User).where(User.strava_athlete_id == owner_id)
        )
        user: User | None = result.scalar_one_or_none()

        if user is None:
            logger.warning(
                "No user for strava_athlete_id=%s — activity %s skipped",
                owner_id, activity_id,
            )
            return

        if not user.is_active:
            logger.info(
                "User telegram_id=%s inactive — activity %s skipped",
                user.telegram_user_id, activity_id,
            )
            return

        # 2. Token
        try:
            access_token = await get_valid_access_token(db, user)
        except ValueError as exc:
            logger.warning(
                "No access token for telegram_id=%s: %s — activity %s skipped",
                user.telegram_user_id, exc, activity_id,
            )
            return

        # 3. Fetch from Strava (do this before dedup so a cold-start fetch
        #    failure doesn't permanently block retries via a stale dedup key)
        try:
            activity_data = await fetch_activity_detail(access_token, activity_id)
        except Exception as exc:
            logger.error("Strava fetch failed for activity_id=%s: %s", activity_id, exc)
            return  # Strava will retry; no dedup key set so retry will proceed

        # 4. Dedup — set only after a successful fetch so failures above are
        #    always retryable. NX means concurrent deliveries are still safe.
        is_new = await redis.set(dedup_key, "1", ex=_DEDUP_TTL_SECONDS, nx=True)
        if not is_new:
            logger.info("Activity strava_id=%s already processed — duplicate ignored", activity_id)
            return

        # 5. Upsert
        activity_date = _parse_strava_date(
            activity_data.get("start_date") or activity_data.get("start_date_local")
        )
        _sport = str(activity_data.get("sport_type") or activity_data.get("type") or "")
        is_indoor = bool(activity_data.get("trainer", False)) or _sport.startswith("Virtual")

        stmt = (
            pg_insert(Activity)
            .values(
                strava_activity_id=activity_id,
                user_id=user.id,
                activity_name=activity_data.get("name") or "Unnamed Activity",
                activity_type=activity_data.get("sport_type") or activity_data.get("type") or "Unknown",
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
        await db.commit()

        user_id_str = str(user.id)

    # 6. Send notification (outside the DB session — uses its own session)
    from app.tasks import fire_and_forget
    fire_and_forget(
        send_activity_notification(activity_data=activity_data, user_id=user_id_str)
    )
    logger.info("Activity strava_id=%s saved; notification scheduled", activity_id)


async def _handle_activity_updated(activity_id: int, updates: dict) -> None:
    """Apply a Strava activity update to the local DB row.

    Re-fetches full detail from Strava when metric fields may have changed
    (distance, time, elevation, HR) — the webhook payload only carries
    title/type, which is insufficient for stats accuracy.
    """
    if not updates:
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Activity).where(Activity.strava_activity_id == activity_id)
        )
        activity: Activity | None = result.scalar_one_or_none()
        if activity is None:
            logger.debug("Activity strava_id=%s not in DB — update ignored", activity_id)
            return

        # Always apply title/type from the payload immediately (fast path)
        if "title" in updates:
            activity.activity_name = updates["title"]
        if "type" in updates or "sport_type" in updates:
            activity.activity_type = (
                updates.get("sport_type") or updates.get("type") or activity.activity_type
            )

        # Re-fetch full metrics from Strava if the activity belongs to a connected user
        user_result = await db.execute(select(User).where(User.id == activity.user_id))
        user: User | None = user_result.scalar_one_or_none()
        if user and user.strava_access_token:
            try:
                access_token = await get_valid_access_token(db, user)
                detail = await fetch_activity_detail(access_token, activity_id)
                activity.activity_name = detail.get("name") or activity.activity_name
                activity.activity_type = detail.get("sport_type") or detail.get("type") or activity.activity_type
                activity.distance_meters = float(detail.get("distance") or activity.distance_meters)
                activity.moving_time_seconds = int(detail.get("moving_time") or activity.moving_time_seconds)
                activity.elapsed_time_seconds = int(detail.get("elapsed_time") or activity.elapsed_time_seconds)
                activity.elevation_gain = float(detail.get("total_elevation_gain") or activity.elevation_gain)
                activity.average_speed = float(detail.get("average_speed") or activity.average_speed)
                activity.max_speed = float(detail.get("max_speed") or activity.max_speed)
                activity.average_heartrate = _optional_float(detail.get("average_heartrate")) or activity.average_heartrate
                activity.max_heartrate = _optional_float(detail.get("max_heartrate")) or activity.max_heartrate
                activity.calories = _optional_float(detail.get("calories")) or activity.calories
                logger.info("Activity strava_id=%s fully refreshed from Strava", activity_id)
            except Exception as exc:
                logger.warning(
                    "Could not re-fetch activity strava_id=%s — payload-only update applied: %s",
                    activity_id, exc,
                )

        await db.commit()
    logger.info("Activity strava_id=%s updated: %s", activity_id, list(updates.keys()))


async def _handle_activity_deleted(activity_id: int) -> None:
    """Remove a deleted Strava activity from the local database."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Activity).where(Activity.strava_activity_id == activity_id)
        )
        activity: Activity | None = result.scalar_one_or_none()
        if activity is None:
            logger.debug("Activity strava_id=%s not in DB — delete ignored", activity_id)
            return

        await db.delete(activity)
        await db.commit()

    # Clear dedup key so the same activity_id can be re-processed if re-uploaded
    redis = await get_redis()
    await redis.delete(key_activity_seen(activity_id))
    logger.info("Activity strava_id=%s deleted", activity_id)


async def _handle_athlete_updated(athlete_id: int, updates: dict) -> None:
    """Handle athlete-level update events, primarily Strava deauthorisation.

    When a user revokes access in the Strava app, Strava sends:
        { "updates": { "authorized": "false" } }
    """
    authorized: str = updates.get("authorized", "true")
    if authorized != "false":
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.strava_athlete_id == athlete_id)
        )
        user: User | None = result.scalar_one_or_none()
        if user is None:
            logger.warning("Deauth event for unknown strava_athlete_id=%s", athlete_id)
            return

        # Clear identity too — not just tokens — so is_strava_connected and
        # every "are you connected?" check (/start, /sync, /stats, /goals)
        # immediately reflect reality instead of showing a stale "Connected"
        # state that can never actually sync again until they reconnect.
        user.strava_access_token     = None
        user.strava_refresh_token    = None
        user.strava_token_expires_at = None
        user.strava_athlete_id       = None
        user.strava_athlete_name     = None
        await db.commit()
        telegram_user_id = user.telegram_user_id

    logger.info(
        "Strava deauthorised: telegram_user_id=%s strava_athlete_id=%s",
        telegram_user_id, athlete_id,
    )

    try:
        from app.telegram.bot import get_application
        bot = get_application().bot
        await bot.send_message(
            chat_id=telegram_user_id,
            text=(
                "⚠️ Your Strava access has been revoked.\n\n"
                "If this was unintentional, use /connect to re-link your account."
            ),
        )
    except Exception as exc:
        logger.warning(
            "Could not notify telegram_user_id=%s of deauthorisation: %s",
            telegram_user_id, exc,
        )


# ---------------------------------------------------------------------------
# Webhook-internal helpers
# ---------------------------------------------------------------------------

from datetime import datetime, timezone as _tz


def _parse_strava_date(date_str: str | None) -> datetime:
    if not date_str:
        return datetime.now(_tz.utc)
    return datetime.fromisoformat(date_str.rstrip("Z")).replace(tzinfo=_tz.utc)


def _utc_now() -> datetime:
    return datetime.now(_tz.utc)


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
