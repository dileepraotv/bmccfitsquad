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
# Import ORM models so Alembic can autogenerate migrations
# These imports register models with Base.metadata.
# We import Base from app.database but guard against missing env vars by
# pre-setting DATABASE_URL (already done above) before the import.
# ---------------------------------------------------------------------------
# Minimal stub — import Base without triggering full Settings validation.
# We only need Base.metadata; we supply our own URL above.
import app.models  # noqa: F401 — registers User, Activity, Goal, GroupChat with Base
from app.database import Base

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
