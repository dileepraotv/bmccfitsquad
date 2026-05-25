"""Async SQLAlchemy engine and session setup.

The module exposes:
  - ``engine``              — AsyncEngine used by the application at runtime
  - ``AsyncSessionLocal``   — session factory for that engine
  - ``Base``                — DeclarativeBase shared by all ORM models
  - ``get_db()``            — FastAPI dependency that yields a managed AsyncSession
  - ``init_db()``           — creates all tables (dev/test only; use Alembic in prod)

Alembic uses a *synchronous* connection for its autogenerate step; the
``alembic/env.py`` imports ``Base`` and ``SYNC_DATABASE_URL`` from here.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()


def _async_url(raw: str) -> str:
    """Ensure the URL uses the ``postgresql+asyncpg`` driver scheme."""
    for prefix in ("postgresql://", "postgres://"):
        if raw.startswith(prefix):
            return raw.replace(prefix, "postgresql+asyncpg://", 1)
    return raw  # already has the right scheme or is a non-postgres URL


def _sync_url(raw: str) -> str:
    """Return a psycopg2-compatible URL for Alembic's synchronous offline mode."""
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg2://" + raw[len(prefix):]
    return raw


ASYNC_DATABASE_URL: str = _async_url(settings.database_url)
SYNC_DATABASE_URL: str = _sync_url(settings.database_url)

# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------

engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=not settings.is_production,  # log SQL in dev
    pool_size=5,
    max_overflow=10,
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
# Declarative base — imported by every model module
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


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
