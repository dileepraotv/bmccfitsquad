"""Alembic environment — supports both online (async) and offline modes.

Online mode uses asyncpg via SQLAlchemy's async engine.
Offline mode (``alembic upgrade head --sql``) uses the sync URL for SQL
script generation without touching a live database.

The database URL is always read from the DATABASE_URL environment variable
(via app.database) so alembic.ini never needs to hold a real credential.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ---------------------------------------------------------------------------
# Import application metadata so Alembic can autogenerate migrations
# ---------------------------------------------------------------------------
# These imports have a side effect: they register every ORM model with Base.
from app.database import ASYNC_DATABASE_URL, SYNC_DATABASE_URL, Base
import app.models  # noqa: F401 — registers User, Activity, Goal, GroupChat

# ---------------------------------------------------------------------------
# Alembic Config object — gives access to alembic.ini values
# ---------------------------------------------------------------------------
config = context.config

# Wire up Python logging from alembic.ini [loggers] section
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline mode
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to the database.

    Run with:  alembic upgrade head --sql
    """
    context.configure(
        url=SYNC_DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Render UUIDs as native PostgreSQL uuid type
        render_as_batch=False,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online (async) mode
# ---------------------------------------------------------------------------

def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,        # detect column-type changes
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations inside a sync-wrapped callback."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = ASYNC_DATABASE_URL

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,   # no connection pooling during migrations
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations (default when a database is reachable)."""
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
