"""The events and sessions tables.

Revision ID: 0001
Revises:
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create both tables and their indexes."""

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("token_id", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False, unique=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(1024), nullable=False),
        sa.Column("collection", sa.String(1024), nullable=False),
        sa.Column("workshop", sa.String(1024), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("frontend", sa.String(64), nullable=False),
        sa.Column("host", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("page", sa.String(255), nullable=False),
        sa.Column("action_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("user", sa.String(255), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_events_session_seq", "events", ["session_id", "seq"])
    op.create_index("ix_events_kind", "events", ["kind"])
    op.create_index("ix_events_ts", "events", ["ts"])
    op.create_index("ix_events_instance", "events", ["instance_id"])
    op.create_index("ix_events_name_collection", "events", ["name", "collection"])
    op.create_index("ix_events_token", "events", ["token_id"])

    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(1024), nullable=False),
        sa.Column("collection", sa.String(1024), nullable=False),
        sa.Column("workshop", sa.String(1024), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("frontend", sa.String(64), nullable=False),
        sa.Column("frontend_version", sa.String(64), nullable=False),
        sa.Column("host", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("trust", sa.String(64), nullable=False),
        sa.Column("user", sa.String(255), nullable=False),
        sa.Column("token_id", sa.String(64), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(), nullable=True),
        sa.Column("last_hidden", sa.Boolean(), nullable=False),
        sa.Column("current_page", sa.String(255), nullable=False),
        sa.Column("pages", sa.JSON(), nullable=False),
        sa.Column("pages_entered", sa.JSON(), nullable=False),
        sa.Column("pages_left", sa.JSON(), nullable=False),
        sa.Column("pages_done", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(), nullable=True),
        sa.Column("resumed_from", sa.String(128), nullable=False),
        sa.Column("restarted_from", sa.String(128), nullable=False),
        sa.Column("resumed_by", sa.String(128), nullable=False),
        sa.Column("recent", sa.JSON(), nullable=False),
        sa.Column("events_received", sa.Integer(), nullable=False),
        sa.Column("events_expected", sa.Integer(), nullable=False),
        sa.Column("gaps", sa.JSON(), nullable=False),
        sa.Column("terminal", sa.Boolean(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_sessions_name_collection", "sessions", ["name", "collection"])
    op.create_index("ix_sessions_instance", "sessions", ["instance_id"])
    op.create_index("ix_sessions_last_seen", "sessions", ["last_seen"])
    op.create_index("ix_sessions_started", "sessions", ["started_at"])


def downgrade() -> None:
    """Drop both tables."""

    op.drop_table("sessions")
    op.drop_table("events")
