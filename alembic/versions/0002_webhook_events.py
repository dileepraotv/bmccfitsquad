"""Add webhook_events — durable ack/process queue for Strava push events.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-26 21:00:00.000000
"""
from __future__ import annotations

import uuid
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("aspect_type", sa.Text(), nullable=False),
        sa.Column("object_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("updates_json", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    # Repair-pass query is "WHERE processed_at IS NULL ORDER BY received_at" —
    # a partial index keeps that cheap even after months of processed rows.
    op.create_index(
        "ix_webhook_events_pending",
        "webhook_events",
        ["received_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_events_pending", table_name="webhook_events")
    op.drop_table("webhook_events")
