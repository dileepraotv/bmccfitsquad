#!/usr/bin/env python3
"""Register (or re-register) the BMCC bot as a Strava webhook subscriber.

What it does
------------
1. Calls GET  /push_subscriptions  — check whether a subscription already exists.
2. If one exists:
   - Prints the current subscription details.
   - Asks for confirmation before deleting and re-creating it (or exits if --no-replace).
3. Calls POST /push_subscriptions  — create the subscription.
4. Prints the resulting subscription ID.

Strava webhook docs:
    https://developers.strava.com/docs/webhooks/

Usage
-----
    # From the project root (bmcc-bot/):
    python scripts/register_strava_webhook.py

    # Skip the "replace existing?" prompt:
    python scripts/register_strava_webhook.py --replace
    python scripts/register_strava_webhook.py --no-replace

Environment (must be set or present in .env)
--------------------------------------------
    STRAVA_CLIENT_ID
    STRAVA_CLIENT_SECRET
    STRAVA_WEBHOOK_VERIFY_TOKEN
    BASE_URL                     — public HTTPS root URL of your deployment
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERROR: httpx is not installed. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Project root + .env loader
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent.resolve()

STRAVA_API = "https://www.strava.com/api/v3/push_subscriptions"


def _load_dotenv() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        print(f"ERROR: {name} is not set. Add it to .env or export it.", file=sys.stderr)
        sys.exit(1)
    return value


# ---------------------------------------------------------------------------
# Strava API helpers
# ---------------------------------------------------------------------------

def _get_existing_subscription(client_id: str, client_secret: str) -> dict | None:
    """Return the current webhook subscription dict, or None if none exists."""
    resp = httpx.get(
        STRAVA_API,
        params={"client_id": client_id, "client_secret": client_secret},
        timeout=15,
    )
    if resp.status_code == 200:
        subs = resp.json()
        return subs[0] if subs else None
    if resp.status_code == 404:
        return None
    print(f"WARNING: Unexpected status {resp.status_code} checking subscriptions: {resp.text}")
    return None


def _delete_subscription(client_id: str, client_secret: str, subscription_id: int) -> None:
    """Delete an existing push subscription by ID."""
    resp = httpx.delete(
        f"{STRAVA_API}/{subscription_id}",
        params={"client_id": client_id, "client_secret": client_secret},
        timeout=15,
    )
    if resp.status_code in (200, 204):
        print(f"Deleted existing subscription ID {subscription_id}.")
    else:
        print(
            f"ERROR: Failed to delete subscription {subscription_id}: "
            f"{resp.status_code} {resp.text}",
            file=sys.stderr,
        )
        sys.exit(1)


def _create_subscription(
    client_id: str,
    client_secret: str,
    callback_url: str,
    verify_token: str,
) -> dict:
    """POST to Strava to create a new push subscription.

    Strava will immediately send a GET to callback_url with hub.challenge.
    Our /strava/webhook route must be live and publicly accessible for this to succeed.
    """
    resp = httpx.post(
        STRAVA_API,
        data={
            "client_id":     client_id,
            "client_secret": client_secret,
            "callback_url":  callback_url,
            "verify_token":  verify_token,
        },
        timeout=30,  # Strava calls our webhook synchronously during this request
    )
    if resp.status_code == 201:
        return resp.json()

    # Common error cases
    body = resp.text
    if resp.status_code == 422:
        print(
            f"ERROR: Strava rejected the subscription (422 Unprocessable Entity).\n"
            f"  This usually means:\n"
            f"  • The callback URL is not publicly reachable (check BASE_URL)\n"
            f"  • The /strava/webhook route returned a non-200 response to Strava's challenge\n"
            f"  • A subscription with this callback URL already exists\n\n"
            f"  Strava response: {body}",
            file=sys.stderr,
        )
    else:
        print(
            f"ERROR: Strava returned {resp.status_code}: {body}",
            file=sys.stderr,
        )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register the BMCC bot with the Strava Webhook API"
    )
    replace_group = parser.add_mutually_exclusive_group()
    replace_group.add_argument(
        "--replace",
        action="store_true",
        help="Delete existing subscription without prompting and create a new one",
    )
    replace_group.add_argument(
        "--no-replace",
        action="store_true",
        help="Exit without changes if a subscription already exists",
    )
    args = parser.parse_args()

    _load_dotenv()

    client_id     = _require("STRAVA_CLIENT_ID")
    client_secret = _require("STRAVA_CLIENT_SECRET")
    verify_token  = _require("STRAVA_WEBHOOK_VERIFY_TOKEN")
    base_url      = _require("BASE_URL").rstrip("/")

    callback_url = f"{base_url}/strava/webhook"

    print("=" * 60)
    print("Strava Webhook Registration — BMCC Bot")
    print("=" * 60)
    print(f"  Callback URL : {callback_url}")
    print(f"  Verify Token : {'*' * len(verify_token)}")
    print()

    # ------------------------------------------------------------------
    # Step 1: check for existing subscription
    # ------------------------------------------------------------------
    print("Checking for existing Strava webhook subscription...")
    existing = _get_existing_subscription(client_id, client_secret)

    if existing:
        sub_id  = existing.get("id")
        sub_url = existing.get("callback_url")
        print(f"  Found existing subscription:")
        print(f"    ID           : {sub_id}")
        print(f"    Callback URL : {sub_url}")
        print()

        if args.no_replace:
            print("--no-replace set — exiting without changes.")
            sys.exit(0)

        if not args.replace:
            answer = input("Delete it and create a new subscription? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted — no changes made.")
                sys.exit(0)

        _delete_subscription(client_id, client_secret, sub_id)
    else:
        print("  No existing subscription found.")
        print()

    # ------------------------------------------------------------------
    # Step 2: create new subscription
    # ------------------------------------------------------------------
    print(f"Creating new subscription → {callback_url}")
    print("(Strava will call your /strava/webhook endpoint to verify — make sure it is live)")
    print()

    sub = _create_subscription(client_id, client_secret, callback_url, verify_token)

    print("=" * 60)
    print("SUCCESS — Strava webhook subscription created!")
    print("=" * 60)
    print(f"  Subscription ID : {sub.get('id')}")
    print(f"  Callback URL    : {sub.get('callback_url', callback_url)}")
    print()
    print("Strava will now POST to your callback URL for every activity event.")
    print("Run this script again any time your BASE_URL changes.")


if __name__ == "__main__":
    main()
