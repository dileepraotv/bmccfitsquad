# BMCC Fitness Bot

Telegram bot for **Beyond Miles Cycling Club (BMCC)**.  
Connects to Strava via webhooks to automatically post activity notifications, personal stats, and motivational quotes to your group chat.

---

## Table of Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Project Structure](#project-structure)
4. [Local Development](#local-development)
5. [Railway Deployment](#railway-deployment)
6. [Post-Deploy Setup](#post-deploy-setup)
7. [Database Migrations](#database-migrations)
8. [Scripts Reference](#scripts-reference)
9. [Environment Variables](#environment-variables)
10. [Bot Commands](#bot-commands)
11. [Troubleshooting](#troubleshooting)

---

## Features

| Feature | Description |
|---|---|
| 🔗 Strava OAuth | Members link their Strava account with `/connect` |
| 🔔 Activity notifications | New rides/runs posted to the group chat automatically |
| 📊 Personal stats | `/stats` shows distance, time, elevation by sport and period |
| 🎯 Goals | Set and track personal distance / duration / elevation goals |
| 💬 Motivational quotes | Random quote appended to every activity notification |
| 🔒 Encrypted storage | Strava tokens encrypted with Fernet before hitting the DB |

---

## Architecture

```
Strava ──webhook──► POST /strava/webhook
                         │
                    FastAPI (web)          Redis (Upstash)
                         │                  ├── OAuth state keys
                    dedup + save ──────────►├── Activity dedup keys
                         │                  └── Celery broker/backend
                    Celery task ◄──────────┘
                         │
                    format message
                         │
                    Telegram Bot API ──► Group chat + DM
                         ▲
Telegram ──webhook──► POST /telegram/webhook
```

**Two processes run on Railway:**

| Process | Command | Role |
|---|---|---|
| `web` | `uvicorn app.main:app` | Receives Strava + Telegram webhooks, serves the OAuth callback |
| `worker` | `celery -A app.celery_app worker` | Formats messages, sends Telegram notifications, syncs history |

---

## Project Structure

```
bmcc-bot/
├── app/
│   ├── main.py              # FastAPI app + lifespan (DB init, Redis, bot setup)
│   ├── config.py            # Pydantic Settings — all env vars in one place
│   ├── database.py          # Async SQLAlchemy engine + session factory
│   ├── redis_client.py      # Upstash Redis connection + key helpers
│   ├── models.py            # ORM models: User, Activity, Goal, GroupChat
│   ├── celery_app.py        # Celery instance + broker configuration
│   ├── tasks.py             # Background tasks: notify, sync history
│   ├── crypto.py            # Fernet encrypt / decrypt helpers
│   ├── strava/
│   │   ├── auth.py          # OAuth flow + encrypted token management
│   │   ├── client.py        # Strava REST API calls (activities, stats)
│   │   └── webhook.py       # Strava webhook receiver + OAuth callback page
│   ├── telegram/
│   │   ├── bot.py           # PTB Application setup + webhook route
│   │   ├── handlers.py      # /command handlers
│   │   ├── keyboards.py     # InlineKeyboardMarkup builders
│   │   └── notifications.py # Activity message formatter + dispatcher
│   └── stats/
│       └── calculator.py    # Aggregation queries + stats formatting
├── alembic/                 # Database migration scripts
├── data/
│   ├── quotes.txt           # One motivational quote per line
│   └── club_message.txt     # Footer appended to every notification
├── scripts/
│   ├── setup_db.py          # Run Alembic migrations
│   └── register_strava_webhook.py  # Register Strava push subscription
├── Procfile                 # Railway process definitions
├── railway.toml             # Railway build + deploy config
├── nixpacks.toml            # Python version + install phase
├── requirements.txt         # Pinned dependencies
└── .env.example             # Environment variable template
```

---

## Local Development

### Prerequisites

- Python 3.11+
- PostgreSQL 15+ running locally (`brew install postgresql`)
- Redis running locally (`brew install redis` then `brew services start redis`)  
  Or use a free [Upstash](https://upstash.com) database (no local Redis needed)
- A [Strava API application](https://www.strava.com/settings/api)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- [ngrok](https://ngrok.com) for Strava webhook testing (Strava requires a public HTTPS URL)

### Setup

```bash
# 1. Enter the project directory
cd bmcc-bot

# 2. Create and activate a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and fill in environment variables
cp .env.example .env
```

Open `.env` and fill in every value. Generate the required secrets:

```bash
# Fernet encryption key (save this — losing it makes stored tokens unreadable)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Webhook secrets (run twice — one for each variable)
python -c "import secrets; print(secrets.token_hex(32))"
```

```bash
# 5. Create the local database
createdb bmcc_bot

# 6. Run database migrations
python scripts/setup_db.py

# 7. Start the FastAPI server
uvicorn app.main:app --reload --port 8000
```

The API is now at `http://localhost:8000`.  
Interactive docs: `http://localhost:8000/docs` (visible only in development mode).

### Running the Celery Worker Locally

Open a second terminal:

```bash
source .venv/bin/activate
celery -A app.celery_app worker --loglevel=info
```

### Local Strava Webhooks

Strava requires a publicly accessible HTTPS URL to send webhook events. Use ngrok to create a tunnel:

```bash
ngrok http 8000
# Outputs something like: https://abc123.ngrok.io
```

Set `BASE_URL=https://abc123.ngrok.io` in your `.env`, then restart the server.  
Register the webhook subscription:

```bash
python scripts/register_strava_webhook.py
```

---

## Railway Deployment

This is the **complete, step-by-step guide** to go from zero to a running bot on Railway.

### Step 1 — Create a Railway account and project

1. Sign up at [railway.app](https://railway.app) if you haven't already.
2. Click **New Project** → **Deploy from GitHub repo**.
3. Authorise Railway to access your GitHub account and select this repository.

> **Tip:** Railway's Hobby plan ($5/month) is sufficient for this bot. It covers the web service, worker service, and PostgreSQL.

---

### Step 2 — Add a PostgreSQL database

1. Inside your project, click **+ New** → **Database** → **Add PostgreSQL**.
2. Railway automatically provisions a database and injects `DATABASE_URL` into your project's shared variables.

> You do **not** need to copy the `DATABASE_URL` manually — Railway makes it available to all services in the project automatically.

---

### Step 3 — Add Redis (Upstash)

Railway has a native Redis plugin, but **Upstash** is recommended because:
- It has a generous free tier (10,000 commands/day)
- It provides TLS (`rediss://`) out of the box, which Celery handles correctly
- It persists data across restarts

**Option A — Upstash (recommended)**

1. Go to [upstash.com](https://upstash.com), create a free account.
2. Click **Create Database**, choose a region close to your Railway deployment.
3. Select **TLS** (the `rediss://` URL) — copy the **Redis URL** from the console.
4. You'll paste this as `REDIS_URL` in Step 5 below.

**Option B — Railway native Redis**

1. Inside your project, click **+ New** → **Database** → **Add Redis**.
2. Railway injects `REDIS_URL` automatically.
3. Note: Railway Redis uses `redis://` (no TLS). The bot handles both schemes correctly.

---

### Step 4 — Create Strava API credentials

1. Go to [strava.com/settings/api](https://www.strava.com/settings/api).
2. Create a new application:
   - **Application Name:** BMCC Bot (or any name)
   - **Category:** Training
   - **Website:** your Railway URL (e.g. `https://your-app.up.railway.app`)
   - **Authorization Callback Domain:** your Railway domain **without** `https://`  
     e.g. `your-app.up.railway.app`
3. Note the **Client ID** and **Client Secret**.

---

### Step 5 — Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, follow the prompts to choose a name and username.
3. BotFather replies with your **bot token** — keep it secret.
4. Send `/setprivacy` → select your bot → **Disable**.  
   This lets the bot see all messages in the group (needed to register group chat IDs).

---

### Step 6 — Set environment variables on Railway

In your Railway project, click your **web service** → **Variables** tab → **Raw Editor**.  
Paste and fill in all of the following:

```
DATABASE_URL=            ← injected automatically from Railway Postgres — leave blank or let Railway inject it
REDIS_URL=               ← paste the rediss:// URL from Upstash (or leave for Railway Redis injection)
STRAVA_CLIENT_ID=        ← from strava.com/settings/api
STRAVA_CLIENT_SECRET=    ← from strava.com/settings/api
STRAVA_WEBHOOK_VERIFY_TOKEN=  ← generate: python -c "import secrets; print(secrets.token_hex(32))"
TELEGRAM_BOT_TOKEN=      ← from @BotFather
TELEGRAM_WEBHOOK_SECRET= ← generate: python -c "import secrets; print(secrets.token_hex(32))"
ENCRYPTION_KEY=          ← generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
APP_ENV=production
BASE_URL=https://your-app.up.railway.app
```

> **Finding your Railway URL:**  
> Go to your web service → **Settings** → **Networking** → **Public Networking** → **Generate Domain**.  
> The URL looks like `https://bmcc-bot-production.up.railway.app`.  
> Set `BASE_URL` to this exact URL (no trailing slash).

> **Sharing variables with the worker service:**  
> If Railway creates a separate worker service, click the worker → **Variables** →  
> **Shared Variables** and link the same variable group so both services use identical values.

---

### Step 7 — Configure the worker service

Railway reads the `Procfile` to determine what processes to run. By default it only starts the `web` process. To also run the Celery worker:

1. In your Railway project, click **+ New** → **Empty Service**.
2. Name it `worker`.
3. Connect it to the same GitHub repository.
4. Under **Settings** → **Deploy** → **Start Command**, enter:
   ```
   celery -A app.celery_app worker --loglevel=info
   ```
5. Add the same environment variables as the web service (or share the variable group).
6. The worker does **not** need a public domain — leave networking unconfigured.

> **Alternative (single-service):** Railway also supports Procfile-based multi-process via a third-party buildpack. The two-service approach above is simpler and more reliable.

---

### Step 8 — Deploy

1. Push your code to the GitHub branch connected to Railway.  
   Railway automatically builds and deploys on every push.
2. Watch the **Build Logs** tab — nixpacks will install Python 3.11 and run `pip install -r requirements.txt`.
3. Watch the **Deploy Logs** tab — you should see:
   ```
   Database tables ready
   Redis connection ready
   Telegram bot ready (webhook registered)
   ```
4. The health check (`GET /health`) must return `200` within 300 seconds or Railway marks the deploy as failed.

> **If the deploy fails:** See the [Troubleshooting](#troubleshooting) section.

---

### Step 9 — Run database migrations

Migrations must be run once after the first deploy (and again after any schema change).

**Option A — Railway shell (easiest)**

1. In your Railway project, click your web service → **Settings** → **Railway Shell**.
2. Run:
   ```bash
   python scripts/setup_db.py
   ```

**Option B — Railway release command**

1. Go to web service → **Settings** → **Deploy** → **Release Command**.
2. Enter:
   ```bash
   python scripts/setup_db.py
   ```
3. Railway will run this automatically before every deploy, ensuring migrations are always up to date.

**Option C — From your local machine** (requires the production `DATABASE_URL`)

```bash
DATABASE_URL="postgresql://..." python scripts/setup_db.py
```

---

### Step 10 — Register the Strava webhook subscription

Strava must be told where to send activity events. This is a one-time setup step (repeat only if your `BASE_URL` changes).

**Your app must be deployed and the `/strava/webhook` route must be live before running this.**

Run from your local machine (with production env vars) or from the Railway shell:

```bash
python scripts/register_strava_webhook.py
```

On success you'll see:

```
SUCCESS — Strava webhook subscription created!
  Subscription ID : 12345
  Callback URL    : https://your-app.up.railway.app/strava/webhook
```

Save the subscription ID in case you need to delete/replace it later.

> **Strava allows only one active webhook subscription per application.**  
> If you get a 422 error, run with `--replace` to delete and recreate:
> ```bash
> python scripts/register_strava_webhook.py --replace
> ```

---

### Step 11 — Verify the Telegram webhook

The bot registers its webhook automatically on every startup via `setWebhook`. To confirm it worked:

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

Expected response:

```json
{
  "ok": true,
  "result": {
    "url": "https://your-app.up.railway.app/telegram/webhook",
    "has_custom_certificate": false,
    "pending_update_count": 0
  }
}
```

If `url` is empty or wrong, check the `APP_ENV` and `BASE_URL` variables and redeploy.

---

### Step 12 — Add the bot to your Telegram group

1. Open your Telegram group.
2. Tap the group name → **Add Members** → search for your bot's username.
3. Add the bot as a member (it doesn't need admin rights for basic operation).
4. Send `/start` in the group — the bot should reply, confirming it received the message.

> **To enable activity notifications in this group,** a group admin needs to register the group chat  
> by using the `/register_chat` command (or however your handlers expose this). The bot saves the  
> `chat_id` to the `group_chats` table with `notifications_enabled=true`.

---

### Step 13 — Connect a Strava account

Each member who wants their activities posted must link their Strava account:

1. Start a **private chat** with the bot.
2. Send `/connect`.
3. The bot replies with a Strava OAuth link — click it.
4. Approve the permission request on Strava's website.
5. You're redirected back to the success page. The bot now receives your activities.

To sync past activities (current year):

```
/sync
```

---

## Post-Deploy Setup

Quick reference for the one-time post-deploy steps:

```bash
# 1. Run migrations
python scripts/setup_db.py

# 2. Register Strava webhook (app must be live first)
python scripts/register_strava_webhook.py

# 3. Verify Telegram webhook
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"

# 4. Health check
curl "https://your-app.up.railway.app/health"
# Expected: {"status":"ok","db":"ok","env":"production"}
```

---

## Database Migrations

Migrations are managed with [Alembic](https://alembic.sqlalchemy.org).

```bash
# Apply all pending migrations (upgrade to latest)
python scripts/setup_db.py

# Downgrade one step
python scripts/setup_db.py --revision -1

# Create a new migration after changing models.py
alembic revision --autogenerate -m "add column X to activities"

# View migration history
alembic history

# Check current revision in the database
alembic current
```

> **In production:** Always run `python scripts/setup_db.py` after deploying schema changes.  
> Alembic is idempotent — running it twice is safe.

---

## Scripts Reference

### `scripts/setup_db.py`

Runs Alembic migrations against `DATABASE_URL`.

```
usage: setup_db.py [-h] [--revision REVISION]

options:
  --revision REVISION   Target revision (default: head)
                        Use '-1' to downgrade one step, 'base' to roll back all.
```

### `scripts/register_strava_webhook.py`

Registers (or re-registers) the Strava push subscription.

```
usage: register_strava_webhook.py [-h] [--replace | --no-replace]

options:
  --replace     Delete existing subscription and create a new one (no prompt)
  --no-replace  Exit without changes if a subscription already exists
```

Reads `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_WEBHOOK_VERIFY_TOKEN`,  
and `BASE_URL` from the environment (or `.env`).

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL connection string — Railway injects this automatically |
| `REDIS_URL` | Yes | Redis / Upstash URL. Use `rediss://` for TLS (Upstash) |
| `STRAVA_CLIENT_ID` | Yes | Strava application client ID |
| `STRAVA_CLIENT_SECRET` | Yes | Strava application client secret |
| `STRAVA_WEBHOOK_VERIFY_TOKEN` | Yes | Random string used to verify Strava subscription challenges |
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Yes | Secret header value for Telegram webhook requests |
| `ENCRYPTION_KEY` | Yes | Fernet key (base64, 32 bytes) — encrypts Strava tokens at rest |
| `APP_ENV` | Yes | `production` (webhook mode) · `development` · `polling` |
| `BASE_URL` | Yes | Public HTTPS URL of this deployment — no trailing slash |

**Generating secrets:**

```bash
# ENCRYPTION_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# STRAVA_WEBHOOK_VERIFY_TOKEN and TELEGRAM_WEBHOOK_SECRET
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Bot Commands

| Command | Scope | Description |
|---|---|---|
| `/start` | Private + Group | Register user, show welcome message |
| `/connect` | Private | Start Strava OAuth flow |
| `/disconnect` | Private | Revoke Strava access |
| `/stats` | Private + Group | Show personal activity stats (prompts for sport + period) |
| `/sync` | Private | Sync Strava activity history for current year |
| `/help` | Private + Group | Show available commands |

---

## Troubleshooting

### Deploy fails at health check

The `/health` endpoint must return `200` within 300 seconds. Common causes:

| Symptom in logs | Fix |
|---|---|
| `could not connect to server` | `DATABASE_URL` is wrong or Postgres service isn't ready yet |
| `Connection refused` for Redis | `REDIS_URL` is wrong — check the Upstash console for the correct URL |
| `Unauthorized` from Telegram | `TELEGRAM_BOT_TOKEN` is invalid — verify with `@BotFather` |
| `ssl.SSLError` | `REDIS_URL` starts with `redis://` but Upstash requires `rediss://` |

### Strava webhook registration returns 422

Strava POSTs a verification request to your `/strava/webhook` route the moment you call  
`register_strava_webhook.py`. If your app isn't live or returns an error, Strava rejects the registration.

Checklist:
- [ ] Your Railway app is deployed and the `/health` endpoint returns `200`
- [ ] `BASE_URL` matches the actual Railway domain exactly (no trailing slash, correct subdomain)
- [ ] `STRAVA_WEBHOOK_VERIFY_TOKEN` in `.env` matches the value set in Railway variables
- [ ] Strava app's "Authorization Callback Domain" matches your Railway domain

### Activities not appearing in Telegram

1. **Check the Celery worker logs** — look for errors in `send_activity_notification`
2. **Check the web logs** — look for errors in `_handle_activity_created`
3. **Verify the Strava subscription** is active:
   ```bash
   curl "https://www.strava.com/api/v3/push_subscriptions?client_id=$STRAVA_CLIENT_ID&client_secret=$STRAVA_CLIENT_SECRET"
   ```
4. **Verify the group chat is registered** — the `group_chats` table must have a row with `notifications_enabled=true`
5. **Test with a manual activity** on Strava — Strava sends the webhook within ~30 seconds

### Telegram bot not responding to commands

```bash
# Check webhook status
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"

# If url is empty, the bot isn't registered — redeploy or check APP_ENV and BASE_URL
```

### `ENCRYPTION_KEY` error on startup

```
ValueError: Fernet key must be 32 url-safe base64-encoded bytes
```

The key must be generated with `Fernet.generate_key()`, not typed manually.  
If you rotate the key, all existing encrypted tokens in the database become unreadable —  
you'll need to re-run the OAuth flow for all users.

### Celery worker: `ModuleNotFoundError`

The worker process needs all the same dependencies as the web process. Make sure:
- Both services use the same nixpacks build (same `requirements.txt`)
- Both services have the same environment variables

### Database migration fails

```bash
# Check which revision the DB is currently on
alembic current

# View the full migration history
alembic history --verbose

# If the DB is ahead of the code (e.g. after a bad deploy):
alembic downgrade -1
```

---

## Updating the Bot

```bash
# 1. Make your changes locally
# 2. Run tests
# 3. Commit and push — Railway auto-deploys

git add .
git commit -m "feat: add leaderboard command"
git push origin main

# 4. If you changed models.py, create and apply a migration:
alembic revision --autogenerate -m "describe the schema change"
git add alembic/versions/
git commit -m "chore: add migration for leaderboard table"
git push origin main

# 5. After deploy, run migrations (or set it as Railway's release command):
python scripts/setup_db.py
```

---

## Customising the Bot

### Quotes

Edit `data/quotes.txt` — one quote per line. The bot picks one at random for each notification.

### Club message

Edit `data/club_message.txt` — this text is appended after every activity notification.  
Use it for links to your club's Strava page, website, or any recurring message.

### Notification format

The message template lives in `app/telegram/notifications.py` → `format_activity_notification()`.

---

*Built for Beyond Miles Cycling Club (BMCC) — keep riding, keep climbing.*
