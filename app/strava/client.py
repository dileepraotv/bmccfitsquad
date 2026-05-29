"""Strava API v3 client.

All outbound HTTP calls to the Strava REST API live here.  Every function
accepts a plaintext ``access_token`` — callers must obtain one via
``app.strava.auth.get_valid_access_token`` before calling these functions.

Rate limits (as of 2024)
-------------------------
  - 100 requests / 15 min
  - 1 000 requests / day

We do not implement client-side rate-limit tracking here; Strava returns
HTTP 429 when the limit is hit, which surfaces as an httpx.HTTPStatusError.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

STRAVA_API_BASE = "https://www.strava.com/api/v3"

# Maximum activities Strava will return in a single page
_MAX_PER_PAGE = 200


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auth_client(access_token: str) -> httpx.AsyncClient:
    """Return a pre-authenticated HTTPX client pointed at the Strava API."""
    return httpx.AsyncClient(
        base_url=STRAVA_API_BASE,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=15.0,
    )


def _app_client() -> httpx.AsyncClient:
    """Return an HTTPX client authenticated with app credentials (no user token).

    Used for webhook subscription management endpoints.
    """
    return httpx.AsyncClient(
        base_url=STRAVA_API_BASE,
        timeout=15.0,
    )


async def _get(access_token: str, path: str, **params) -> dict | list:
    """Perform a GET request and return the parsed JSON body.

    Raises:
        httpx.HTTPStatusError: On any non-2xx response.
    """
    async with _auth_client(access_token) as client:
        response = await client.get(path, params={k: v for k, v in params.items() if v is not None})
        response.raise_for_status()
        return response.json()


# ---------------------------------------------------------------------------
# Athlete
# ---------------------------------------------------------------------------

async def get_athlete(access_token: str) -> dict:
    """Fetch the authenticated athlete's public profile.

    Endpoint: GET /athlete

    Returns:
        Strava Athlete Summary object as a dict.
    """
    data = await _get(access_token, "/athlete")
    logger.debug("Fetched athlete id=%s", data.get("id"))
    return data


async def fetch_athlete_stats(strava_athlete_id: int, access_token: str) -> dict:
    """Fetch recent, year-to-date and all-time totals for a given athlete.

    Endpoint: GET /athletes/{strava_athlete_id}/stats

    Args:
        strava_athlete_id: The numeric Strava athlete ID.
        access_token:      Plaintext access token for that athlete.

    Returns:
        ActivityStats summary dict with keys like ``recent_ride_totals``,
        ``ytd_run_totals``, ``all_run_totals``, etc.
    """
    data = await _get(access_token, f"/athletes/{strava_athlete_id}/stats")
    logger.debug("Fetched stats for strava_athlete_id=%s", strava_athlete_id)
    return data


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------

async def fetch_activity_detail(access_token: str, activity_id: int) -> dict:
    """Fetch the full detail for a single activity.

    Endpoint: GET /activities/{activity_id}

    Args:
        access_token: Plaintext access token.
        activity_id:  Strava activity ID.

    Returns:
        DetailedActivity dict (all fields populated, including segment efforts).
    """
    data = await _get(
        access_token,
        f"/activities/{activity_id}",
        include_all_efforts="false",
    )
    logger.debug(
        "Fetched activity detail id=%s type=%s", data.get("id"), data.get("type")
    )
    return data


async def update_activity(
    access_token: str,
    activity_id: int,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """Update mutable fields on a Strava activity.

    Endpoint: PUT /activities/{activity_id}

    Args:
        access_token: Plaintext access token.
        activity_id:  Strava activity ID to update.
        name:         New activity name (optional).
        description:  New activity description (optional).

    Returns:
        Updated DetailedActivity dict from Strava.

    Raises:
        httpx.HTTPStatusError: On non-2xx response (e.g. 403 if scope missing).
    """
    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description

    async with _auth_client(access_token) as client:
        response = await client.put(f"/activities/{activity_id}", json=payload)
        response.raise_for_status()
        data = response.json()

    logger.info("Updated activity id=%s fields=%s", activity_id, list(payload.keys()))
    return data


async def fetch_activities(
    access_token: str,
    after: int | None = None,
    before: int | None = None,
    per_page: int = _MAX_PER_PAGE,
) -> list[dict]:
    """Fetch all of an athlete's activities, handling pagination automatically.

    Endpoint: GET /athlete/activities (repeated until an empty page)

    Args:
        access_token: Plaintext access token.
        after:        Only return activities after this Unix timestamp (optional).
        before:       Only return activities before this Unix timestamp (optional).
        per_page:     Page size (max 200, Strava's hard limit).

    Returns:
        Combined list of ActivitySummary dicts across all pages.
    """
    per_page = min(per_page, _MAX_PER_PAGE)
    all_activities: list[dict] = []
    page = 1

    async with _auth_client(access_token) as client:
        while True:
            params: dict = {"page": page, "per_page": per_page}
            if after is not None:
                params["after"] = after
            if before is not None:
                params["before"] = before

            response = await client.get("/athlete/activities", params=params)
            response.raise_for_status()
            batch: list[dict] = response.json()

            if not batch:
                break

            all_activities.extend(batch)
            logger.debug(
                "fetch_activities page=%s fetched=%s cumulative=%s",
                page,
                len(batch),
                len(all_activities),
            )

            if len(batch) < per_page:
                # Last partial page — no more data
                break

            page += 1
            # Be a good citizen: small delay between pages to respect rate limits
            await asyncio.sleep(0.3)

    logger.info("fetch_activities: total=%s", len(all_activities))
    return all_activities


# ---------------------------------------------------------------------------
# Webhook subscription management
# ---------------------------------------------------------------------------

async def create_webhook_subscription(verify_token: str, callback_url: str) -> dict:
    """Register this app's webhook callback URL with Strava.

    Only one subscription per application is allowed.  Strava will send a
    GET validation challenge to ``callback_url`` immediately after this call.

    Endpoint: POST /push_subscriptions

    Returns:
        Subscription dict containing ``id`` (store this for deletion later).

    Raises:
        httpx.HTTPStatusError: On failure (e.g. duplicate subscription → 409).
    """
    async with _app_client() as client:
        response = await client.post(
            "/push_subscriptions",
            data={
                "client_id": settings.strava_client_id,
                "client_secret": settings.strava_client_secret,
                "callback_url": callback_url,
                "verify_token": verify_token,
            },
        )
        response.raise_for_status()
        data = response.json()
    logger.info("Webhook subscription created: id=%s", data.get("id"))
    return data


async def view_webhook_subscription() -> list[dict]:
    """List this app's active webhook subscriptions.

    Endpoint: GET /push_subscriptions

    Returns:
        List of subscription dicts (usually 0 or 1 items).
    """
    async with _app_client() as client:
        response = await client.get(
            "/push_subscriptions",
            params={
                "client_id": settings.strava_client_id,
                "client_secret": settings.strava_client_secret,
            },
        )
        response.raise_for_status()
        return response.json()


async def delete_webhook_subscription(subscription_id: int) -> None:
    """Delete an existing webhook subscription by ID.

    Endpoint: DELETE /push_subscriptions/{subscription_id}

    Raises:
        httpx.HTTPStatusError: If the subscription does not exist.
    """
    async with _app_client() as client:
        response = await client.delete(
            f"/push_subscriptions/{subscription_id}",
            params={
                "client_id": settings.strava_client_id,
                "client_secret": settings.strava_client_secret,
            },
        )
        response.raise_for_status()
    logger.info("Webhook subscription deleted: id=%s", subscription_id)
