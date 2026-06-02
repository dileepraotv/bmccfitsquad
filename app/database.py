"""Async SQLAlchemy engine and session setup.

The module exposes:
  - ``engine``              — AsyncEngine used by the application at runtime
  - ``AsyncSessionLocal``   — session factory for that engine
  - ``Base``                — DeclarativeBase shared by all ORM models
  - ``get_db()``            — FastAPI dependency that yields a managed AsyncSession
  - ``init_db()``           — creates all tables (dev/test only; use Alembic in prod)

Alembic uses a *synchronous* connection for its autogenerate step; the
``alembic/env.py`` imports ``Base`` and ``SYNC_DATABASE_URL`` from here.

URL normalisation
-----------------
Railway injects ``?sslmode=require`` into DATABASE_URL.  That is a libpq /
psycopg2 parameter — asyncpg does not accept it and raises:

    TypeError: connect() got an unexpected keyword argument 'sslmode'

``_async_url()`` strips ``sslmode`` from the query string and returns the
equivalent asyncpg ``connect_args`` so the engine is configured correctly.
"""
from __future__ import annotations

import ssl as _ssl
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.base import Base  # noqa: F401 — re-exported for backwards compat
from app.config import get_settings

settings = get_settings()


def _async_url(raw: str) -> tuple[str, dict]:
    """Normalise *raw* into an asyncpg-compatible URL + connect_args dict.

    Steps:
    1. Replace ``postgresql://`` / ``postgres://`` scheme with
       ``postgresql+asyncpg://``.
    2. Strip ``sslmode`` from the query string (asyncpg rejects it).
    3. Translate the sslmode value into an ``ssl`` connect_arg:
       - ``require``              → ssl context with cert verification disabled
         (Railway's internal Postgres uses a self-signed cert)
       - ``verify-ca`` / ``verify-full`` → ssl context with full verification
       - ``disable`` / absent     → no SSL

    Returns ``(url, connect_args)`` ready to pass to ``create_async_engine``.
    """
    # 1. Fix driver scheme
    for prefix in ("postgresql://", "postgres://"):
        if raw.startswith(prefix):
            raw = raw.replace(prefix, "postgresql+asyncpg://", 1)
            break

    connect_args: dict = {}

    # 2. Strip sslmode and translate to asyncpg ssl connect_arg
    if "sslmode" in raw:
        parsed = urlparse(raw)
        params = parse_qs(parsed.query, keep_blank_values=True)
        sslmode_values = params.pop("sslmode", [])
        sslmode = sslmode_values[0] if sslmode_values else "disable"

        if sslmode in ("require", "verify-ca", "verify-full"):
            ctx = _ssl.create_default_context()
            if sslmode == "require":
                # Railway's Postgres uses a self-signed cert — skip hostname
                # verification while still encrypting the connection.
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
            connect_args["ssl"] = ctx

        # Rebuild URL without sslmode
        new_query = urlencode({k: v[0] for k, v in params.items()})
        raw = urlunparse(parsed._replace(query=new_query))

    return raw, connect_args


def _sync_url(raw: str) -> str:
    """Return a psycopg2-compatible URL for Alembic's synchronous offline mode."""
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg2://" + raw[len(prefix):]
    return raw


ASYNC_DATABASE_URL, _CONNECT_ARGS = _async_url(settings.database_url)
SYNC_DATABASE_URL: str = _sync_url(settings.database_url)

# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------

engine = create_async_engine(
    ASYNC_DATABASE_URL,
    connect_args=_CONNECT_ARGS,
    echo=not settings.is_production,  # log SQL in dev
    # Railway Postgres free tier has a low connection limit (~20).
    # Web + background tasks share this pool, so keep it small.
    pool_size=3,
    max_overflow=2,     # absolute max = 5 connections
    pool_pre_ping=True,               # recycle stale connections automatically
    pool_recycle=1800,                # recycle after 30 min regardless
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create all tables defined on ``Base``.

    Only use this in development or tests.  In production, run
    ``alembic upgrade head`` instead.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """FastAPI dependency — yields a transactional :class:`AsyncSession`.

    Commits on clean exit, rolls back on exception, always closes the session.

    Usage::

        @router.get("/")
        async def handler(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_connection() -> bool:
    """Return True if the database is reachable (useful for health checks)."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
