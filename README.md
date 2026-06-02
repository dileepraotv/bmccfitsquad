# BMCC Fitness Bot

Telegram bot for **Beyond Miles Cycling Club (BMCC)**.  
Connects to Strava via webhooks to automatically post activity notifications, personal stats, and motivational quotes to your group chat.

---

## Architecture

```
Strava ──webhook──► POST /strava/webhook
                         │
                    FastAPI (Render)       Redis (Upstash)
                         │                  ├── OAuth state keys
                    dedup + save ──────────►└── Activity dedup keys
                         │
                    asyncio background task
                         │
                    format message
                         │
                    Telegram Bot API ──► Group chat + DM
                         ▲
Telegram ──webhook──► POST /telegram/webhook

UptimeRobot ──ping──► GET /ping   (every 5 min — prevents Render free-tier sleep)
```

**Infrastructure (all free):**

| Component | Provider | Notes |
|-----------|----------|-------|
| Web service | [Render](https://render.com) | Free tier, 750 hrs/month |
| Keep-alive | [UptimeRobot](https://uptimerobot.com) | Pings `/ping` every 5 min |
| PostgreSQL | [Neon](https://neon.tech) | Free forever, never expires |
| Redis | [Upstash](https://upstash.com) | 10k commands/day free |

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
│   ├── tasks.py             # asyncio background tasks: notify, sync history
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
├── render.yaml              # Render deployment config
└── requirements.txt         # Pinned dependencies
```

---

## Local Development

### Prerequisites

- Python 3.11+
- PostgreSQL running locally (`brew install postgresql`)
- A [Neon](https://neon.tech) free database (or local Postgres)
- A free [Upstash](https://upstash.com) Redis database
- A [Strava API application](https://www.strava.com/settings/api)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- [ngrok](https://ngrok.com) for local Strava webhook testing

### Setup

```bash
# 1. Create and activate a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and fill in environment variables
cp .env.example .env
```

Generate the required secrets:

```bash
# Fernet encryption key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Webhook secrets (run twice — one for each)
python3 -c "import secrets; print(secrets.token_hex(32))"
```

```bash
# 4. Run database migrations
.venv/bin/python scripts/setup_db.py

# 5. Start the server
uvicorn app.main:app --reload --port 8000
```

For local Strava webhooks, use ngrok:

```bash
ngrok http 8000
# Set BASE_URL=https://abc123.ngrok.io in .env, then restart the server
.venv/bin/python scripts/register_strava_webhook.py
```

---

## Render Deployment

### One-time setup

1. **Create a [Render](https://render.com) Web Service** connected to this GitHub repo
   - Build command: `pip install -r requirements.txt`
   - Start command: `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Plan: Free

2. **Set environment variables** in Render dashboard (see table below)

3. **Update Strava callback domain** at [strava.com/settings/api](https://www.strava.com/settings/api):
   - Authorization Callback Domain: `your-service.onrender.com`

4. **Register Strava webhook** (run once locally after deploy):
   ```bash
   .venv/bin/python scripts/register_strava_webhook.py
   ```

5. **Set up [UptimeRobot](https://uptimerobot.com)** to prevent Render free-tier sleep:
   - Monitor type: HTTP(s)
   - URL: `https://your-service.onrender.com/ping`
   - Interval: 5 minutes

### Verify everything is working

```bash
curl https://your-service.onrender.com/ping    # {"status":"ok"}
curl https://your-service.onrender.com/health  # {"status":"ok","db":"ok","env":"production"}
curl https://your-service.onrender.com/strava/webhook/status  # shows active subscription
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | Neon PostgreSQL connection string |
| `REDIS_URL` | Upstash Redis URL (`rediss://...`) |
| `STRAVA_CLIENT_ID` | From [strava.com/settings/api](https://www.strava.com/settings/api) |
| `STRAVA_CLIENT_SECRET` | From Strava developer dashboard |
| `STRAVA_WEBHOOK_VERIFY_TOKEN` | Random string for Strava webhook verification |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_WEBHOOK_SECRET` | Random string to verify Telegram webhook calls |
| `ENCRYPTION_KEY` | Fernet key — encrypts Strava tokens at rest |
| `APP_ENV` | `production` or `development` |
| `BASE_URL` | Public HTTPS URL of this deployment (no trailing slash) |

---

## Bot Commands

| Command | Scope | Description |
|---------|-------|-------------|
| `/start` | Private + Group | Register and show welcome message |
| `/connect` | Private | Link your Strava account |
| `/disconnect` | Private | Unlink your Strava account |
| `/stats` | Private + Group | View personal activity stats |
| `/goals` | Private | Set and track distance / time / elevation goals |
| `/sync` | Private | Incremental sync of recent Strava activities |
| `/fullsync` | Private | Full history sync (use if stats look wrong) |
| `/help` | Private + Group | Show all commands |
| `/cancel` | Private | Cancel any active flow |

---

## Database Migrations

Migrations run automatically on every Render deploy via the start command.  
To create a new migration after changing `models.py`:

```bash
alembic revision --autogenerate -m "describe the change"
git add alembic/versions/
git commit -m "add migration: describe the change"
git push
```

Render will apply it automatically on next deploy.

---

## Customising the Bot

- **Quotes:** Edit `data/quotes.txt` — one quote per line
- **Club message:** Edit `data/club_message.txt` — appended to every activity notification
- **Notification format:** `app/telegram/notifications.py` → `format_activity_notification()`

---

*Built for Beyond Miles Cycling Club (BMCC) — keep riding, keep climbing.*
