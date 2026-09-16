"""Inserting events: hashing, dedupe and the extracted columns.

Raw events are never updated. The hash is SHA-256 over the event
serialised with sorted keys and `labels` left out, so a batch delivered
twice, or a file imported twice under different labels, inserts nothing
new and the first arrival's labels stand.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, select
from sqlalchemy.dialects import postgresql, sqlite

from .tables import events


@dataclass(frozen=True)
class StoredEvent:
    """An event ready to store: the record, its hash and who sent it."""

    event: dict[str, Any]
    labels: dict[str, str]
    hash: str
    token_id: str
    received_at: datetime


def event_hash(event: Mapping[str, Any]) -> str:
    """The content hash of an event, labels excluded."""

    content = {key: value for key, value in event.items() if key != "labels"}
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_timestamp(value: Any) -> datetime:
    """A naive UTC datetime from an ISO 8601 string, refusing anything else."""

    if not isinstance(value, str):
        raise ValueError("the timestamp must be a string")

    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if moment.tzinfo is None:
        return moment

    return moment.astimezone(UTC).replace(tzinfo=None)


def utcnow() -> datetime:
    """The current time as a naive UTC datetime, the store's convention."""

    return datetime.now(UTC).replace(tzinfo=None)


def collection_identity(event: Mapping[str, Any]) -> str:
    """The collection an event belongs to, as the service identifies it.

    A collection index that declares an `id` is known by it wherever it
    runs; one that does not is known by where it was subscribed from,
    which the `collection` field carries. Empty means no collection.
    """

    return str(event.get("collection_id") or event.get("collection") or "")


def row_for(record: StoredEvent) -> dict[str, Any]:
    """The insert values for a stored event."""

    event = record.event

    return {
        "received_at": record.received_at,
        "token_id": record.token_id,
        "hash": record.hash,
        "kind": str(event.get("kind", "")),
        "ts": parse_timestamp(event.get("ts")),
        "session_id": str(event.get("session_id", "")),
        "seq": int(event.get("seq", 0)),
        "instance_id": str(event.get("instance_id", "")),
        "name": str(event.get("name", "")),
        "source": str(event.get("source", "")),
        "collection": collection_identity(event),
        "workshop": str(event.get("workshop", "")),
        "version": str(event.get("version", "")),
        "frontend": str(event.get("frontend", "")),
        "host": str(event.get("host", "")),
        "platform": str(event.get("platform", "")),
        "page": str(event.get("page", "")),
        "action_id": str(event.get("id", "")),
        "status": str(event.get("status", "")),
        "user": str(event.get("user", "")),
        "labels": record.labels,
        "payload": event,
    }


def insert_events(
    connection: Connection, records: Sequence[StoredEvent]
) -> list[StoredEvent]:
    """Insert the records not already stored; returns the ones inserted.

    Duplicates within the batch and against the table are both dropped.
    The insert itself says "on conflict do nothing", so two batches
    racing on the same hash cannot fail either.
    """

    unique: dict[str, StoredEvent] = {}

    for record in records:
        unique.setdefault(record.hash, record)

    if not unique:
        return []

    existing = set(
        connection.execute(
            select(events.c.hash).where(events.c.hash.in_(list(unique)))
        ).scalars()
    )

    fresh = [record for digest, record in unique.items() if digest not in existing]

    if not fresh:
        return []

    rows = [row_for(record) for record in fresh]

    if connection.dialect.name == "postgresql":
        statement: Any = postgresql.insert(events).values(rows)
    else:
        statement = sqlite.insert(events).values(rows)

    connection.execute(statement.on_conflict_do_nothing(index_elements=["hash"]))

    return fresh
