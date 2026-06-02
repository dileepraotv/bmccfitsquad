# Deploying BMCC Bot on Render (Free Forever)

## Architecture

| Component | Provider | Plan | Cost | Notes |
|-----------|----------|------|------|-------|
| Web service | Render | Free | $0 | 750 hrs/month, sleep after 15 min inactivity |
| Keep-alive | UptimeRobot | Free | $0 | Pings `/ping` every 5 min — prevents sleep |
| PostgreSQL | Neon | Free | $0 | 0.5 GB, **never expires**, sleeps when idle |
| Redis | Upstash | Free | $0 | 10k commands/day |

> **Why Neon instead of Render Postgres?**
> Render's free Postgres is deleted after 30 days — same problem as Railway.
> Neon's free tier never expires and works identically (standard PostgreSQL).

---

## Step 1 — Get a free Neon PostgreSQL database

1. Go to [neon.tech](https://neon.tech) and sign up (free, no card)
2. Create a new project → name it `bmcc-bot`
3. Copy the **Connection string** — it looks like:
   ```
   postgresql://user:password@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```
4. Keep this for Step 3

> If you already have a Railway Postgres with data you want to keep, export it first:
> ```bash
> pg_dump "YOUR_RAILWAY_DATABASE_URL" > bmcc_backup.sql
> psql "YOUR_NEON_DATABASE_URL" < bmcc_backup.sql
> ```

---

## Step 2 — Deploy to Render

1. Go to [render.com](https://render.com) and sign up / log in
2. Click **New → Web Service**
3. Connect your GitHub repo: `dileepraotv/bmccfitsquad`
4. Configure:
   - **Name:** `bmcc-bot`
   - **Region:** Singapore
   - **Branch:** `main`
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Plan:** Free
5. Click **Advanced** → **Add Environment Variable** for each of the following:

| Variable | Value |
|----------|-------|
| `APP_ENV` | `production` |
| `DATABASE_URL` | Your Neon connection string (from Step 1) |
| `REDIS_URL` | Your Upstash Redis URL (`rediss://...`) |
| `STRAVA_CLIENT_ID` | From Strava developer dashboard |
| `STRAVA_CLIENT_SECRET` | From Strava developer dashboard |
| `STRAVA_WEBHOOK_VERIFY_TOKEN` | Any random string you choose |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Any random string (used to verify Telegram calls) |
| `ENCRYPTION_KEY` | Your existing Fernet key (from Railway env) |
| `BASE_URL` | `https://bmcc-bot.onrender.com` ← set AFTER deploy gives you the URL |

6. Click **Create Web Service**
7. Wait for the first deploy to finish (~3-5 minutes)
8. Copy your Render URL (e.g. `https://bmcc-bot.onrender.com`)
9. Go back to **Environment** → update `BASE_URL` to that URL → **Save**
10. Trigger a **Manual Deploy** so the bot picks up the correct `BASE_URL`

---

## Step 3 — Run Alembic migrations on Neon

Once deployed, open Render's **Shell** tab (or run locally with Neon URL):

```bash
# In Render Shell or locally with DATABASE_URL set to Neon:
alembic upgrade head
```

---

## Step 4 — Update Strava app settings

1. Go to [strava.com/settings/api](https://www.strava.com/settings/api)
2. Set **Authorization Callback Domain** to: `bmcc-bot.onrender.com`
   (just the domain, no `https://`, no path)

---

## Step 5 — Register Strava webhook

After deploy, run this once to point Strava webhooks at your new Render URL:

```bash
# Locally with your env vars set, or in Render Shell:
python scripts/register_strava_webhook.py
```

Or call the Strava API directly:
```bash
curl -X POST https://www.strava.com/api/v3/push_subscriptions \
  -F client_id=YOUR_CLIENT_ID \
  -F client_secret=YOUR_CLIENT_SECRET \
  -F callback_url=https://bmcc-bot.onrender.com/strava/webhook \
  -F verify_token=YOUR_VERIFY_TOKEN
```

Verify it's registered:
```
https://bmcc-bot.onrender.com/strava/webhook/status
```

---

## Step 6 — Set up UptimeRobot (prevents Render sleep)

Render free tier sleeps after **15 minutes** of no traffic. A sleeping service
takes ~50 seconds to wake up — users would see very slow responses or timeouts.

**Fix: UptimeRobot pings `/ping` every 5 minutes, keeping the service always awake.**

1. Go to [uptimerobot.com](https://uptimerobot.com) → Sign up free
2. Click **Add New Monitor**
3. Configure:
   - **Monitor Type:** HTTP(s)
   - **Friendly Name:** BMCC Bot
   - **URL:** `https://bmcc-bot.onrender.com/ping`
   - **Monitoring Interval:** 5 minutes
4. Click **Create Monitor**

That's it. UptimeRobot will ping your service every 5 minutes 24/7 for free.
The `/ping` endpoint returns instantly with no DB or Redis calls, so it costs
nothing in Upstash quota.

---

## Verifying everything works

```bash
# Health check (DB status)
curl https://bmcc-bot.onrender.com/health

# Keep-alive endpoint
curl https://bmcc-bot.onrender.com/ping

# Strava webhook status
curl https://bmcc-bot.onrender.com/strava/webhook/status
```

---

## Monthly free tier usage estimate

| Resource | Usage | Limit | Headroom |
|----------|-------|-------|----------|
| Render hours | 720 hrs (24/7) | 750 hrs | ✅ 30 hrs spare |
| Neon Postgres | ~10 MB growing | 512 MB | ✅ Fine for years |
| Upstash Redis | ~500 cmds/day | 10k/day | ✅ 20× headroom |
| UptimeRobot monitors | 1 | 50 | ✅ Fine |

---

## Switching from Railway

1. Export your Railway Postgres data: `pg_dump "RAILWAY_URL" > backup.sql`
2. Import to Neon: `psql "NEON_URL" < backup.sql`
3. Follow Steps 2–6 above
4. Once Render is confirmed working, delete the Railway project to avoid any leftover costs
