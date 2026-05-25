#!/usr/bin/env python3
"""Simulate a Strava webhook event to test the full notification pipeline locally.

What it does
------------
1. Sends a POST to /strava/webhook with a fake "activity create" event.
2. The FastAPI handler will:
   a. Deduplicate via Redis
   b. Look up the user by strava_athlete_id
   c. Fetch the full activity from Strava (mocked or real, see --mock flag)
   d. Upsert to DB
   e. Dispatch a Celery task that formats + sends the Telegram notification
3. Prints the server's response and any useful debug info.

Usage
-----
    # Start the bot first:
    ./scripts/run_local.sh

    # Then in another terminal:
    python scripts/test_notification.py                    # defaults
    python scripts/test_notification.py --athlete-id 12345 --activity-id 99999
    python scripts/test_notification.py --host http://localhost:9000
    python scripts/test_notification.py --list-sports      # show all supported sport types

Note
----
The webhook handler will try to fetch the activity from the real Strava API
using the user's stored access token.  If you want to skip Strava and test
just the notification formatting, use the --dry-run flag — it calls
format_activity_notification() directly with the sample payload.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root on sys.path so we can import app modules for --dry-run
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))


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


# ---------------------------------------------------------------------------
# Sample Strava activity payloads
# ---------------------------------------------------------------------------

def _sample_webhook_payload(athlete_id: int, activity_id: int) -> dict:
    """Minimal Strava webhook event body for an activity create."""
    return {
        "object_type":  "activity",
        "object_id":    activity_id,
        "aspect_type":  "create",
        "owner_id":     athlete_id,
        "subscription_id": 999999,
        "event_time":   int(datetime.now(timezone.utc).timestamp()),
        "updates":      {},
    }


def _sample_activity_detail(activity_id: int, athlete_id: int, sport: str = "Ride") -> dict:
    """Realistic Strava activity detail dict (what /activities/{id} returns).

    This is the same structure passed to format_activity_notification().
    Distances are in metres, speed in m/s, elevation in metres.
    """
    sport_defaults: dict[str, dict] = {
        "Ride": {
            "distance":             105_340.0,   # 105.34 km
            "moving_time":          13_860,       # 3h 51m 0s
            "elapsed_time":         14_400,       # 4h 0m 0s
            "total_elevation_gain": 1_420.0,      # 1420 m
            "average_speed":        7.60,         # ~27.4 km/h
            "max_speed":            15.28,        # ~55.0 km/h
            "calories":             2_850.0,
            "average_heartrate":    142.0,
            "max_heartrate":        178.0,
            "average_watts":        198.0,
            "max_watts":            612.0,
            "type":                 "Ride",
        },
        "VirtualRide": {
            "distance":             42_000.0,
            "moving_time":          4_500,
            "elapsed_time":         4_500,
            "total_elevation_gain": 380.0,
            "average_speed":        9.33,
            "max_speed":            16.11,
            "calories":             820.0,
            "average_heartrate":    None,
            "max_heartrate":        None,
            "average_watts":        215.0,
            "max_watts":            520.0,
            "type":                 "VirtualRide",
            "trainer":              True,
        },
        "Run": {
            "distance":             21_097.5,    # Half marathon
            "moving_time":          7_200,        # 2h
            "elapsed_time":         7_380,
            "total_elevation_gain": 185.0,
            "average_speed":        2.93,         # ~10.5 km/h
            "max_speed":            4.17,
            "calories":             1_150.0,
            "average_heartrate":    158.0,
            "max_heartrate":        182.0,
            "type":                 "Run",
        },
        "Walk": {
            "distance":             8_500.0,
            "moving_time":          5_400,
            "elapsed_time":         5_700,
            "total_elevation_gain": 95.0,
            "average_speed":        1.57,
            "max_speed":            2.8,
            "calories":             420.0,
            "average_heartrate":    None,
            "max_heartrate":        None,
            "type":                 "Walk",
        },
        "Swim": {
            "distance":             2_000.0,     # 2 km open water
            "moving_time":          2_700,
            "elapsed_time":         2_700,
            "total_elevation_gain": 0.0,
            "average_speed":        0.74,
            "max_speed":            1.2,
            "calories":             460.0,
            "average_heartrate":    None,
            "max_heartrate":        None,
            "type":                 "Swim",
        },
    }

    defaults = sport_defaults.get(sport, sport_defaults["Ride"])
    now = datetime.now(timezone.utc)

    base = {
        "id":            activity_id,
        "name":          f"Morning {sport} with BMCC",
        "start_date":    now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start_date_local": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "athlete":       {"id": athlete_id},
        "trainer":       False,
        "commute":       False,
        "manual":        False,
        "private":       False,
        "flagged":       False,
    }
    return {**base, **defaults}


# ---------------------------------------------------------------------------
# Webhook mode — POST to the running server
# ---------------------------------------------------------------------------

def run_webhook_test(
    host: str,
    athlete_id: int,
    activity_id: int,
) -> None:
    try:
        import httpx
    except ImportError:
        print("ERROR: httpx is not installed. Run: pip install httpx")
        sys.exit(1)

    url = f"{host.rstrip('/')}/strava/webhook"
    payload = _sample_webhook_payload(athlete_id, activity_id)

    print("=" * 60)
    print("Strava Webhook Simulation")
    print("=" * 60)
    print(f"  Target URL   : {url}")
    print(f"  Athlete ID   : {athlete_id}")
    print(f"  Activity ID  : {activity_id}")
    print()
    print("  Payload:")
    print(f"  {json.dumps(payload, indent=4)}")
    print()

    try:
        resp = httpx.post(url, json=payload, timeout=15)
    except httpx.ConnectError:
        print(f"ERROR: Could not connect to {url}")
        print("  Is the server running? Start it with:  ./scripts/run_local.sh")
        sys.exit(1)

    print(f"  HTTP {resp.status_code}")
    try:
        print(f"  Response: {json.dumps(resp.json(), indent=4)}")
    except Exception:
        print(f"  Response: {resp.text!r}")

    if resp.status_code == 200:
        print()
        print("  The webhook was accepted!")
        print("  Check the Celery worker terminal for the notification task output.")
        print("  Check your Telegram group chat for the activity message.")
    else:
        print()
        print(f"  Unexpected status {resp.status_code} — check the FastAPI server logs.")


# ---------------------------------------------------------------------------
# Dry-run mode — call format_activity_notification() directly, no server
# ---------------------------------------------------------------------------

async def run_dry_run(sport: str, athlete_id: int, activity_id: int) -> None:
    _load_dotenv()
    from app.telegram.notifications import format_activity_notification

    activity = _sample_activity_detail(activity_id, athlete_id, sport)
    athlete_name = "Alex Rider"

    print("=" * 60)
    print("Dry-run: format_activity_notification() preview")
    print("=" * 60)
    print(f"  Sport type   : {sport}")
    print(f"  Athlete      : {athlete_name}")
    print()
    print("─" * 60)

    text = await format_activity_notification(activity, athlete_name)
    print(text)

    print("─" * 60)
    print()
    print("  This is exactly what would be sent to Telegram.")
    print("  No DB, Redis, or Strava API calls were made.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate a Strava webhook event to test the notification flow"
    )
    parser.add_argument(
        "--host",
        default="http://localhost:8000",
        help="Base URL of the running bot server (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--athlete-id",
        type=int,
        default=12345678,
        metavar="ID",
        help="Strava athlete ID stored in the users table (default: 12345678)",
    )
    parser.add_argument(
        "--activity-id",
        type=int,
        default=9_999_999_999,
        metavar="ID",
        help="Fake Strava activity ID to use (default: 9999999999)",
    )
    parser.add_argument(
        "--sport",
        choices=["Ride", "VirtualRide", "Run", "Walk", "Swim"],
        default="Ride",
        help="Sport type for dry-run mode (default: Ride)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip the HTTP request — call format_activity_notification() "
            "directly and print the rendered Telegram message. No server needed."
        ),
    )
    parser.add_argument(
        "--list-sports",
        action="store_true",
        help="List all supported sport types and exit",
    )
    args = parser.parse_args()

    if args.list_sports:
        print("Supported sport types for --sport flag:")
        for s in ["Ride", "VirtualRide", "Run", "Walk", "Swim"]:
            print(f"  {s}")
        return

    if args.dry_run:
        asyncio.run(run_dry_run(args.sport, args.athlete_id, args.activity_id))
    else:
        run_webhook_test(args.host, args.athlete_id, args.activity_id)


if __name__ == "__main__":
    main()
