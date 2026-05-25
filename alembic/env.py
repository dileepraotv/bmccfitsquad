"""Alembic environment — sync psycopg2 connection for migrations.

Reads DATABASE_URL directly from the environment (or .env) — does NOT import
app.config so that migrations can run with only DATABASE_URL set.

Online mode:  alembic upgrade head          (default)
Offline mode: alembic upgrade head --sql    (emit SQL, no DB connection)
"""
from __future__ import annotations

import os
import re
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# ---------------------------------------------------------------------------
# Load .env if present (only sets vars that aren't already in the environment)
# ---------------------------------------------------------------------------
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _val = _line.partition("=")
        os.environ.setdefault(_key.strip(), _val.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# Resolve a psycopg2-compatible sync URL from DATABASE_URL
# ---------------------------------------------------------------------------
_raw_url = os.environ.get("DATABASE_URL", "")
if not _raw_url:
    raise RuntimeError(
        "DATABASE_URL is not set. "
        "Export it or add it to .env before running alembic."
    )

def _to_sync_url(url: str) -> str:
    """Convert any Postgres URL variant to a psycopg2-compatible sync URL.

    Handles:
      postgresql://...          → postgresql+psycopg2://...
      postgresql+asyncpg://...  → postgresql+psycopg2://...
      postgres://...            → postgresql+psycopg2://...
    """
    url = re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql+psycopg2://", url)
    return url

SYNC_DATABASE_URL = _to_sync_url(_raw_url)

# ---------------------------------------------------------------------------
# Stub out env vars that app.config (pydantic Settings) requires but
# migrations do not actually use.  We set them BEFORE importing any app
# module so that Settings() can be constructed without a full .env file.
# Only DATABASE_URL carries a real value — all others are ignored at
# migration time.
# ---------------------------------------------------------------------------
_MIGRATION_STUBS = {
    "REDIS_URL":                  "redis://localhost",
    "STRAVA_CLIENT_ID":           "0",
    "STRAVA_CLIENT_SECRET":       "stub",
    "STRAVA_WEBHOOK_VERIFY_TOKEN":"stub",
    "TELEGRAM_BOT_TOKEN":         "0:stub",
    "TELEGRAM_WEBHOOK_SECRET":    "stub",
    # Any non-empty string passes pydantic's `str` type check.
    # Fernet key validation only happens when crypto.py functions are called,
    # which never happens during migrations.
    "ENCRYPTION_KEY":             "c3R1Yi1rZXktZm9yLW1pZ3JhdGlvbnMtb25seQ==",
    "BASE_URL":                   "https://example.com",
}
for _k, _v in _MIGRATION_STUBS.items():
    os.environ.setdefault(_k, _v)

# ---------------------------------------------------------------------------
# Import ORM models so Alembic can autogenerate migrations
# ---------------------------------------------------------------------------
from app.base import Base       # does NOT import app.database → no asyncpg needed
import app.models  # noqa: F401 — registers User, Activity, Goal, GroupChat with Base

# ---------------------------------------------------------------------------
# Alembic config
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline mode — emit SQL without connecting
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    context.configure(
        url=SYNC_DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode — connect and run migrations synchronously via psycopg2
# ---------------------------------------------------------------------------

def run_migrations_online() -> None:
    connectable = create_engine(
        SYNC_DATABASE_URL,
        poolclass=pool.NullPool,   # no pooling during migrations
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
