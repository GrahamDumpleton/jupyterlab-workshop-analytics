"""The sink: parse a batch, validate it, store it, project it, publish it.

One code path serves the HTTP endpoint and the `import` command, so a
file exported from an offline class lands exactly as a posted batch
would. The phases inside one batch are marked with wrapture blocks and
the counts annotated onto the enclosing event, so a trace of a request
shows where its time went and what it did.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import wrapture
from sqlalchemy import Engine

from .config import Settings
from .labels import merge_labels
from .live import Broadcaster
from .projection import apply_events, live_row
from .schema import EventValidator
from .store.writes import StoredEvent, event_hash, insert_events, utcnow
from .tokens import Claims

log = logging.getLogger(__name__)

PROBLEMS_REPORTED = 5


class BatchError(Exception):
    """A body that is not a batch at all: the request is refused whole."""


@dataclass
class IngestResult:
    """What became of one batch."""

    received: int = 0
    stored: int = 0
    duplicates: int = 0
    rejected: int = 0
    problems: list[str] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict[str, Any]:
        """The response body for the batch."""

        return {
            "received": self.received,
            "stored": self.stored,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "problems": self.problems[:PROBLEMS_REPORTED],
        }


def parse_body(body: bytes, content_type: str) -> list[Any]:
    """The events in a request body: JSON lines, or a JSON array.

    The content type decides: `application/x-ndjson` (what the extension
    sends) is one event per line, blank lines skipped; `application/json`
    is a JSON array. A line that is not JSON is kept as a marker so the
    validator counts it as rejected rather than the whole body failing.
    """

    media = content_type.split(";", 1)[0].strip().lower()

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BatchError("the body is not UTF-8") from error

    if media == "application/json":
        try:
            parsed = json.loads(text)
        except ValueError as error:
            raise BatchError(f"the body is not JSON: {error}") from error

        if isinstance(parsed, dict):
            return [parsed]

        if not isinstance(parsed, list):
            raise BatchError("a JSON body must be an array of events")

        return parsed

    if media not in {"application/x-ndjson", "application/jsonlines", "", "text/plain"}:
        raise BatchError(
            f'unsupported content type "{media}": send application/x-ndjson'
        )

    lines: list[Any] = []

    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue

        try:
            lines.append(json.loads(line))
        except ValueError:
            lines.append(_Unparsed(number))

    return lines


@dataclass(frozen=True)
class _Unparsed:
    line: int


class RateLimiter:
    """A per-token cap on batches per minute, in memory."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = {}

    def allow(self, token_id: str, moment: float | None = None) -> bool:
        """Whether one more batch from the token is within its allowance."""

        if self.per_minute <= 0:
            return True

        now = time.monotonic() if moment is None else moment

        with self._lock:
            window = self._windows.setdefault(token_id, deque())

            while window and now - window[0] >= 60.0:
                window.popleft()

            if len(window) >= self.per_minute:
                return False

            window.append(now)

            return True


@dataclass
class Ingest:
    """The batch pipeline bound to a store, a schema and a broadcaster."""

    engine: Engine
    settings: Settings
    validator: EventValidator
    broadcaster: Broadcaster

    def accept(
        self,
        items: Sequence[Any],
        claims: Claims | None,
        extra_labels: dict[str, str] | None = None,
        received_at: datetime | None = None,
    ) -> IngestResult:
        """Validate, store, project and publish one batch.

        `claims` are the verified token the batch arrived under, or None
        for an import with no token; `extra_labels` are trusted labels
        for the import alone. The token's labels win over an event's
        own; a colliding event label is kept under the `client.` prefix.
        """

        result = IngestResult(received=len(items))
        trusted = dict(extra_labels or {})
        token_id = ""

        if claims is not None:
            trusted.update(claims.labels)
            token_id = claims.jti

        moment = received_at or utcnow()
        records: list[StoredEvent] = []

        # Validation: every item is judged on its own, so one bad line
        # never loses the batch it arrived in.
        with wrapture.block("ingest.parse"):
            for index, item in enumerate(items[: self.settings.max_batch]):
                if isinstance(item, _Unparsed):
                    result.rejected += 1
                    result.problems.append(f"line {item.line}: not JSON")

                    continue

                problems = self.validator.problems(item)

                if problems:
                    result.rejected += 1
                    result.problems.append(f"event {index + 1}: {problems[0]}")

                    log.warning("rejected event %d: %s", index + 1, problems[0])

                    continue

                event = dict(item)
                reported = {
                    str(k): str(v) for k, v in dict(event.get("labels") or {}).items()
                }
                labels = merge_labels(trusted, reported)

                records.append(
                    StoredEvent(
                        event=event,
                        labels=labels,
                        hash=event_hash(event),
                        token_id=token_id,
                        received_at=moment,
                    )
                )

            result.rejected += max(0, len(items) - self.settings.max_batch)

        # Storage and projection share one transaction, so a session's
        # completeness columns are right the moment its events are.
        with wrapture.block("ingest.store"):
            with self.engine.begin() as connection:
                inserted = insert_events(connection, records)
                projected = apply_events(
                    connection, [record.event for record in inserted], token_id
                )

            result.stored = len(inserted)
            result.duplicates = len(records) - len(inserted)

        # Publishing happens after the commit, so a stream never hears
        # about a session the database does not yet hold.
        with wrapture.block("ingest.broadcast"):
            now = utcnow()

            for session in projected:
                row = live_row(session, now, self.settings)

                result.sessions.append(row)
                self.broadcaster.publish(row)

        wrapture.annotate(
            received=result.received,
            stored=result.stored,
            duplicates=result.duplicates,
            rejected=result.rejected,
        )

        return result
