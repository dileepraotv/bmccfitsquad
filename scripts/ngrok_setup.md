# ngrok Setup for Local Strava Webhook Testing

Strava's webhook API requires a **publicly accessible HTTPS URL** to deliver events.
During local development, ngrok creates a secure tunnel from a public URL to your
machine, so Strava can reach your bot running on `localhost:8000`.

---

## 1. Install ngrok

```bash
# macOS (Homebrew)
brew install ngrok

# macOS / Linux (manual)
# Download from https://ngrok.com/download and move to /usr/local/bin/ngrok

# Verify
ngrok version
```

---

## 2. Create a free ngrok account

1. Sign up at [ngrok.com](https://ngrok.com/signup) — the free tier is sufficient.
2. Copy your **authtoken** from the [ngrok dashboard](https://dashboard.ngrok.com/get-started/your-authtoken).
3. Configure ngrok with your token:

```bash
ngrok config add-authtoken YOUR_AUTHTOKEN_HERE
```

You only need to do this once per machine.

---

## 3. Start the tunnel

In its own terminal tab (leave it running while you develop):

```bash
ngrok http 8000
```

ngrok prints something like:

```
Forwarding   https://abc123def456.ngrok-free.app -> http://localhost:8000
```

Copy the `https://...ngrok-free.app` URL — this is your `BASE_URL`.

> **Note:** The free tier generates a new random URL every time you restart ngrok.
> You'll need to re-register the Strava webhook subscription each session.
> The paid "personal" plan ($8/month) lets you reserve a static domain.

---

## 4. Update .env with the ngrok URL

Open `.env` and set:

```
BASE_URL=https://abc123def456.ngrok-free.app
APP_ENV=development
```

Then restart the bot server so it picks up the new `BASE_URL`:

```bash
# Ctrl+C to stop, then restart
./scripts/run_local.sh
```

---

## 5. Register the ngrok URL as a Strava webhook

With the bot server running and the ngrok tunnel active:

```bash
python scripts/register_strava_webhook.py
```

Expected output:
```
Creating new subscription → https://abc123def456.ngrok-free.app/strava/webhook

SUCCESS — Strava webhook subscription created!
  Subscription ID : 12345
```

Strava immediately sends a GET to `/strava/webhook` to verify the URL is live.
If registration fails, confirm the server is running and `BASE_URL` is correct.

> Strava allows only **one active subscription per app**.
> If you already have one from a previous session, run:
>
> ```bash
> python scripts/register_strava_webhook.py --replace
> ```

---

## 6. Test the full pipeline

With ngrok running, the bot server running, and the Strava subscription registered:

```bash
# Option A — send a real webhook event (requires a user in the DB)
python scripts/test_notification.py --athlete-id YOUR_STRAVA_ATHLETE_ID

# Option B — preview the notification format without any server
python scripts/test_notification.py --dry-run --sport Ride
```

For Option A, go to [strava.com](https://www.strava.com) and record a manual activity,
or use Strava's "Create an Activity" API to fire a real event.

---

## 7. Telegram bot in development mode

By default (`APP_ENV=development`), the bot **deletes the Telegram webhook** on startup
and does not register a new one. This means Telegram updates (messages, `/commands`)
won't reach your local bot unless you either:

**Option A — use polling** (simplest, no ngrok needed for Telegram):

```bash
APP_ENV=polling ./scripts/run_local.sh
```

Set `APP_ENV=polling` in `.env`, then start the bot. PTB runs polling automatically.
Note: Strava webhook delivery still requires ngrok.

**Option B — register the Telegram webhook through ngrok** (full production simulation):

```bash
APP_ENV=production ./scripts/run_local.sh
```

With `APP_ENV=production` the bot calls `setWebhook` pointing to your ngrok URL.
Telegram delivers updates to `https://abc123.ngrok-free.app/telegram/webhook`.

---

## Quick Reference

| Terminal | Command | Purpose |
|---|---|---|
| 1 | `ngrok http 8000` | Public HTTPS tunnel |
| 2 | `./scripts/run_local.sh` | FastAPI + Celery |
| 3 | `python scripts/test_notification.py` | Fire test webhook |

---

## Alternative: cloudflared (Cloudflare Tunnel)

If you prefer Cloudflare's tool over ngrok:

```bash
# Install
brew install cloudflared

# Start a quick tunnel (no account required)
cloudflared tunnel --url http://localhost:8000
```

The tunnel URL format is `https://random-words.trycloudflare.com`.
Use it as `BASE_URL` exactly as described for ngrok above.
