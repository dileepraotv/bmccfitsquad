"""SQLAlchemy declarative base.

Kept in its own module so that alembic/env.py and scripts can import
``Base`` without triggering ``app.database`` (which creates the async
engine and immediately imports asyncpg).
"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
