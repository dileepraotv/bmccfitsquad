"""Strava OAuth token management.

Flow
----
1. Bot calls generate_oauth_state() → stores {state: telegram_user_id} in Redis for 10 min
2. Bot sends user the URL from build_authorization_url(state)
3. User authorises on Strava → Strava redirects to GET /strava/callback?code=…&state=…
4. Callback validates state, calls exchange_code_for_tokens(), saves encrypted tokens via save_tokens()
5. On every Strava API call, get_valid_access_token() refreshes tokens automatically if ≤5 min from expiry
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.models import User
from app.redis_client import get_redis, key_oauth_state

logger = logging.getLogger(__name__)
settings = get_settings()

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_OAUTH_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_DEAUTHORIZE_URL = "https://www.strava.com/oauth/deauthorize"

# Refresh the token if it expires within this window
_REFRESH_BUFFER = timedelta(minutes=5)
# OAuth state lives in Redis for this long
_STATE_TTL_SECONDS = 600


# ---------------------------------------------------------------------------
# OAuth state helpers
# ---------------------------------------------------------------------------

async def generate_oauth_state(telegram_user_id: int) -> str:
    """Create a cryptographically random state token and store it in Redis.

    The state maps to the Telegram user ID so the OAuth callback can look up
    which user is connecting.  It expires after 10 minutes.

    Returns:
        The state string to embed in the authorization URL.
    """
    state = secrets.token_urlsafe(32)
    redis = await get_redis()
    await redis.setex(key_oauth_state(state), _STATE_TTL_SECONDS, str(telegram_user_id))
    logger.debug("OAuth state generated for telegram_user_id=%s", telegram_user_id)
    return state


async def validate_oauth_state(state: str) -> int | None:
    """Consume the state token from Redis and return the associated Telegram user ID.

    The key is deleted atomically so it cannot be replayed.

    Returns:
        Telegram user ID, or None if the state is unknown or expired.
    """
    redis = await get_redis()
    value = await redis.getdel(key_oauth_state(state))
    if value is None:
        logger.warning("OAuth state not found or expired: %s", state)
        return None
    return int(value)


def build_authorization_url(state: str) -> str:
    """Return the Strava OAuth URL the user must open to grant access.

    Scopes requested:
      - ``read``               – public profile
      - ``activity:read_all``  – all activities (including private ones)
    """
    params = urlencode({
        "client_id": settings.strava_client_id,
        "redirect_uri": settings.strava_redirect_uri,
        "response_type": "code",
        "scope": "read,read_all,activity:read_all,activity:write,profile:read_all",
        "state": state,
        "approval_prompt": "force",  # always show consent so new scopes are accepted
    })
    return f"{STRAVA_OAUTH_AUTHORIZE_URL}?{params}"


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

async def exchange_code_for_tokens(code: str) -> dict:
    """Exchange a short-lived authorisation code for access + refresh tokens.

    Args:
        code: The ``code`` query parameter from the OAuth redirect.

    Returns:
        Full Strava token response dict, including ``access_token``,
        ``refresh_token``, ``expires_at``, and ``athlete`` sub-dict.

    Raises:
        httpx.HTTPStatusError: If Strava returns a non-2xx status.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": settings.strava_client_id,
                "client_secret": settings.strava_client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        data = response.json()
    logger.debug(
        "Token exchange success: athlete_id=%s expires_at=%s",
        data.get("athlete", {}).get("id"),
        data.get("expires_at"),
    )
    return data


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

async def save_tokens(
    db: AsyncSession,
    user: User,
    access_token: str,
    refresh_token: str,
    expires_at: int,
) -> None:
    """Encrypt and persist Strava tokens on the User model instance.

    Flushes the session so the changes are visible within the current
    transaction without committing yet.

    Args:
        db:            The active async session.
        user:          ORM instance to update in-place.
        access_token:  Plaintext access token from Strava.
        refresh_token: Plaintext refresh token from Strava.
        expires_at:    Unix timestamp (int) when the access token expires.
    """
    user.strava_access_token = encrypt(access_token)
    user.strava_refresh_token = encrypt(refresh_token)
    user.strava_token_expires_at = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    await db.flush()
    logger.debug(
        "Tokens saved for user telegram_id=%s expires_at=%s",
        user.telegram_user_id,
        user.strava_token_expires_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Token retrieval with transparent refresh
# ---------------------------------------------------------------------------

async def get_valid_access_token(db: AsyncSession, user: User) -> str:
    """Return a valid plaintext access token, refreshing automatically if needed.

    Args:
        db:   Active async session (needed if a refresh call is required).
        user: User ORM instance — must have Strava tokens stored.

    Returns:
        Plaintext access token ready to pass to the Strava API.

    Raises:
        ValueError: If the user has no tokens stored (not connected to Strava).
    """
    if not user.strava_access_token or not user.strava_token_expires_at:
        raise ValueError(
            f"User telegram_id={user.telegram_user_id} has no Strava tokens. "
            "They need to authorise via /connect first."
        )

    needs_refresh = datetime.now(timezone.utc) >= (
        user.strava_token_expires_at - _REFRESH_BUFFER
    )
    if needs_refresh:
        logger.info(
            "Access token expiring soon for telegram_id=%s — refreshing",
            user.telegram_user_id,
        )
        await _refresh_access_token(db, user)

    return decrypt(user.strava_access_token)


async def _refresh_access_token(db: AsyncSession, user: User) -> None:
    """Call Strava's token refresh endpoint and update the stored tokens.

    Args:
        db:   Active async session.
        user: User ORM instance to update in-place.

    Raises:
        ValueError:            If the user has no refresh token stored.
        httpx.HTTPStatusError: If Strava returns a non-2xx status.
    """
    if not user.strava_refresh_token:
        raise ValueError(
            f"User telegram_id={user.telegram_user_id} has no refresh token stored."
        )

    plaintext_refresh_token = decrypt(user.strava_refresh_token)

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": settings.strava_client_id,
                "client_secret": settings.strava_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": plaintext_refresh_token,
            },
        )
        response.raise_for_status()
        data = response.json()

    await save_tokens(
        db,
        user,
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=data["expires_at"],
    )
    logger.info("Access token refreshed for telegram_id=%s", user.telegram_user_id)


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

async def deauthorize(access_token: str) -> None:
    """Revoke a Strava access token (called when a user disconnects).

    Strava invalidates the token server-side; we also null out the fields
    in the DB separately.

    Errors are swallowed so that local DB cleanup still proceeds even if the
    Strava API is unavailable.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                STRAVA_DEAUTHORIZE_URL,
                data={"access_token": access_token},
            )
    except Exception as exc:
        logger.warning("Strava deauthorize call failed (ignored): %s", exc)
