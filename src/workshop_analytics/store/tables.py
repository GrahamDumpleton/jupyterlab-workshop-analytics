"""The table definitions, SQLAlchemy Core.

Two tables. `events` holds every event as it arrived, never updated,
with the fields worth filtering on extracted into indexed columns and
the whole event kept as JSON. `sessions` is a projection over it, one
row per session, rebuilt from `events` by the `rebuild` command, so
its shape can change freely. Timestamps are stored as naive UTC.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
)

metadata = MetaData()

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("received_at", DateTime, nullable=False),
    Column("token_id", String(64), nullable=False, default=""),
    Column("hash", String(64), nullable=False, unique=True),
    Column("kind", String(64), nullable=False),
    Column("ts", DateTime, nullable=False),
    Column("session_id", String(128), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("instance_id", String(128), nullable=False),
    Column("name", String(255), nullable=False),
    Column("source", String(1024), nullable=False, default=""),
    Column("collection", String(1024), nullable=False, default=""),
    Column("workshop", String(1024), nullable=False, default=""),
    Column("version", String(64), nullable=False, default=""),
    Column("frontend", String(64), nullable=False, default=""),
    Column("host", String(64), nullable=False, default=""),
    Column("platform", String(64), nullable=False, default=""),
    Column("page", String(255), nullable=False, default=""),
    Column("action_id", String(255), nullable=False, default=""),
    Column("status", String(64), nullable=False, default=""),
    Column("user", String(255), nullable=False, default=""),
    Column("labels", JSON, nullable=False),
    Column("payload", JSON, nullable=False),
    Index("ix_events_session_seq", "session_id", "seq"),
    Index("ix_events_kind", "kind"),
    Index("ix_events_ts", "ts"),
    Index("ix_events_instance", "instance_id"),
    Index("ix_events_name_collection", "name", "collection"),
    Index("ix_events_token", "token_id"),
)

sessions = Table(
    "sessions",
    metadata,
    Column("session_id", String(128), primary_key=True),
    Column("instance_id", String(128), nullable=False, default=""),
    Column("name", String(255), nullable=False, default=""),
    Column("source", String(1024), nullable=False, default=""),
    Column("collection", String(1024), nullable=False, default=""),
    Column("workshop", String(1024), nullable=False, default=""),
    Column("version", String(64), nullable=False, default=""),
    Column("frontend", String(64), nullable=False, default=""),
    Column("frontend_version", String(64), nullable=False, default=""),
    Column("host", String(64), nullable=False, default=""),
    Column("platform", String(64), nullable=False, default=""),
    Column("trust", String(64), nullable=False, default=""),
    Column("user", String(255), nullable=False, default=""),
    Column("token_id", String(64), nullable=False, default=""),
    Column("labels", JSON, nullable=False),
    Column("started_at", DateTime, nullable=False),
    Column("last_seen", DateTime, nullable=False),
    Column("last_heartbeat", DateTime, nullable=True),
    Column("last_hidden", Boolean, nullable=False, default=False),
    Column("current_page", String(255), nullable=False, default=""),
    Column("pages", JSON, nullable=False),
    Column("pages_entered", JSON, nullable=False),
    Column("pages_left", JSON, nullable=False),
    Column("pages_done", Integer, nullable=False, default=0),
    Column("gates_skipped", Integer, nullable=False, default=0),
    Column("directives_run", JSON, nullable=False, default=list),
    Column("finished_at", DateTime, nullable=True),
    Column("abandoned_at", DateTime, nullable=True),
    Column("resumed_from", String(128), nullable=False, default=""),
    Column("restarted_from", String(128), nullable=False, default=""),
    Column("resumed_by", String(128), nullable=False, default=""),
    Column("recent", JSON, nullable=False),
    Column("events_received", Integer, nullable=False, default=0),
    Column("events_expected", Integer, nullable=False, default=0),
    Column("gaps", JSON, nullable=False),
    Column("terminal", Boolean, nullable=False, default=False),
    Column("complete", Boolean, nullable=False, default=False),
    Index("ix_sessions_name_collection", "name", "collection"),
    Index("ix_sessions_instance", "instance_id"),
    Index("ix_sessions_last_seen", "last_seen"),
    Index("ix_sessions_started", "started_at"),
)
