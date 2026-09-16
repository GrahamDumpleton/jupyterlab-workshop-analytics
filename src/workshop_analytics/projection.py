"""The sessions projection: one row per session, derived from events.

The projection is applied to newly stored events in the same
transaction that stores them, and can be rebuilt from `events` at any
time, so its shape is free to change. Status is not stored: it is
derived at read time from the row's timestamps and the service's
thresholds, so a quiet service needs no timer to age sessions out.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, delete, select, update

from .config import Settings
from .store.tables import events, sessions
from .store.writes import collection_identity, parse_timestamp, utcnow

STATUSES = ("active", "away", "silent", "lost", "resumed", "finished", "abandoned")

LIVE_STATUSES = ("active", "away", "silent")

RECENT_KINDS = ("action-executed", "verify-result", "quiz-answered")

RECENT_LIMIT = 5

# The kinds that name a directive by its id, which the coverage of a
# session's inventory is read from.
DIRECTIVE_KINDS = (
    "action-executed",
    "verify-result",
    "quiz-answered",
    "form-submitted",
    "hint-opened",
)

DIRECTIVE_FIELDS = ("id", "type", "trigger")


@dataclass
class Session:
    """A mutable working copy of one sessions row."""

    session_id: str
    instance_id: str = ""
    name: str = ""
    source: str = ""
    collection: str = ""
    collection_title: str = ""
    workshop: str = ""
    version: str = ""
    frontend: str = ""
    frontend_version: str = ""
    host: str = ""
    platform: str = ""
    trust: str = ""
    user: str = ""
    token_id: str = ""
    labels: dict[str, str] | None = None
    started_at: datetime | None = None
    last_seen: datetime | None = None
    last_heartbeat: datetime | None = None
    last_hidden: bool = False
    current_page: str = ""
    pages: list[dict[str, Any]] | None = None
    pages_entered: list[str] | None = None
    pages_left: list[str] | None = None
    pages_done: int = 0
    gates_skipped: int = 0
    directives_run: list[str] | None = None
    finished_at: datetime | None = None
    abandoned_at: datetime | None = None
    resumed_from: str = ""
    restarted_from: str = ""
    resumed_by: str = ""
    recent: list[dict[str, Any]] | None = None
    events_received: int = 0
    events_expected: int = 0
    gaps: list[list[int]] | None = None
    terminal: bool = False
    complete: bool = False

    def __post_init__(self) -> None:
        self.labels = dict(self.labels or {})
        self.pages = list(self.pages or [])
        self.pages_entered = list(self.pages_entered or [])
        self.pages_left = list(self.pages_left or [])
        self.directives_run = list(self.directives_run or [])
        self.recent = list(self.recent or [])
        self.gaps = list(self.gaps or [])

    @classmethod
    def from_row(cls, row: Any) -> Session:
        """A working copy from a sessions row."""

        return cls(**dict(row._mapping))

    def as_row(self) -> dict[str, Any]:
        """The values to write back."""

        return dict(self.__dict__)


def fold(session: Session, event: dict[str, Any], labels: dict[str, str]) -> None:
    """Apply one event to a session, in the event's `seq` order."""

    ts = parse_timestamp(event.get("ts"))
    kind = str(event.get("kind", ""))

    # The first event settles who the session is; the identity fields
    # never change within one session, so nothing later overwrites them.
    if session.started_at is None:
        session.instance_id = str(event.get("instance_id", ""))
        session.name = str(event.get("name", ""))
        session.source = str(event.get("source", ""))
        session.collection = collection_identity(event)
        session.collection_title = str(event.get("collection_title", ""))
        session.workshop = str(event.get("workshop", ""))
        session.version = str(event.get("version", ""))
        session.frontend = str(event.get("frontend", ""))
        session.frontend_version = str(event.get("frontend_version", ""))
        session.host = str(event.get("host", ""))
        session.platform = str(event.get("platform", ""))
        session.trust = str(event.get("trust", ""))
        session.labels = dict(labels)
        session.started_at = ts
        session.last_seen = ts

    if not session.user and event.get("user"):
        session.user = str(event["user"])

    if session.last_seen is None or ts > session.last_seen:
        session.last_seen = ts

    page = str(event.get("page", ""))

    if kind in {"workshop-start", "workshop-resume"}:
        pages = event.get("pages")

        if isinstance(pages, list):
            session.pages = [
                page_entry(entry) for entry in pages if isinstance(entry, dict)
            ]

        session.current_page = page
        session.started_at = min(session.started_at or ts, ts)

        if kind == "workshop-start":
            session.restarted_from = str(event.get("restarted_from", ""))
        else:
            session.resumed_from = str(event.get("resumed_from", ""))

    elif kind == "page-enter":
        session.current_page = page

        if page and page not in (session.pages_entered or []):
            assert session.pages_entered is not None

            session.pages_entered.append(page)

    elif kind == "page-leave":
        if page and page not in (session.pages_left or []):
            assert session.pages_left is not None

            session.pages_left.append(page)

        session.pages_done = len(session.pages_left or [])

    elif kind == "heartbeat":
        session.last_heartbeat = ts
        session.last_hidden = bool(event.get("hidden", False))

        if page:
            session.current_page = page

    elif kind == "workshop-finish":
        session.finished_at = ts
        session.terminal = True

    elif kind == "workshop-abandon":
        session.abandoned_at = ts
        session.terminal = True

        if page:
            session.current_page = page

    elif kind == "gate-skipped":
        session.gates_skipped += 1

    # The directives the session ran, by id, against which its inventory
    # says what was never run.
    if kind in DIRECTIVE_KINDS:
        assert session.directives_run is not None

        directive = str(event.get("id", ""))

        if directive and directive not in session.directives_run:
            session.directives_run.append(directive)

    if kind in RECENT_KINDS:
        assert session.recent is not None

        entry: dict[str, Any] = {
            "kind": kind,
            "id": str(event.get("id", "")),
            "ts": str(event.get("ts", "")),
        }

        if kind == "quiz-answered":
            entry["status"] = "correct" if event.get("correct") else "incorrect"
        else:
            entry["status"] = str(event.get("status", ""))

        session.recent.append(entry)
        del session.recent[:-RECENT_LIMIT]


def page_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A page of the page list as the session row keeps it.

    The id, path and title always; the directive inventory when the
    extension sent one (0.2.1 and later), each directive reduced to its
    id, type, trigger and the conditional flag. A page without the key
    is one whose session carried no inventory, which the reports say
    rather than reading as nothing to run.
    """

    page: dict[str, Any] = {
        "id": str(entry.get("id", "")),
        "path": str(entry.get("path", "")),
        "title": str(entry.get("title", "")),
    }
    directives = entry.get("directives")

    if isinstance(directives, list):
        listed: list[dict[str, Any]] = []

        for item in directives:
            if not isinstance(item, dict) or not item.get("id"):
                continue

            directive: dict[str, Any] = {
                name: str(item.get(name, "")) for name in DIRECTIVE_FIELDS
            }

            if item.get("conditional"):
                directive["conditional"] = True

            listed.append(directive)

        page["directives"] = listed

    return page


def completeness(
    seqs: Iterable[int], terminal_seq: int | None
) -> tuple[int, int, list[list[int]], bool]:
    """Received count, expected count, the missing ranges and completeness.

    Expected is the terminal event's `seq` when a finish or abandon
    arrived, since nothing follows those, and otherwise the highest
    `seq` seen. Complete means a terminal event arrived and nothing
    below it is missing.
    """

    seen = sorted(set(seqs))
    received = len(seen)

    if not seen:
        return 0, 0, [], False

    expected = terminal_seq if terminal_seq is not None else seen[-1]
    expected = max(expected, seen[-1])
    missing = sorted(set(range(1, expected + 1)) - set(seen))
    gaps: list[list[int]] = []

    for seq in missing:
        if gaps and gaps[-1][1] == seq - 1:
            gaps[-1][1] = seq
        else:
            gaps.append([seq, seq])

    return received, expected, gaps, terminal_seq is not None and not gaps


def _load_session(connection: Connection, session_id: str) -> Session | None:
    row = connection.execute(
        select(sessions).where(sessions.c.session_id == session_id)
    ).first()

    return None if row is None else Session.from_row(row)


def _session_events(connection: Connection, session_id: str) -> list[Any]:
    return list(
        connection.execute(
            select(events.c.seq, events.c.kind, events.c.payload, events.c.labels)
            .where(events.c.session_id == session_id)
            .order_by(events.c.seq, events.c.id)
        )
    )


def _write_session(connection: Connection, session: Session, existed: bool) -> None:
    values = session.as_row()

    if existed:
        session_id = values.pop("session_id")

        connection.execute(
            update(sessions).where(sessions.c.session_id == session_id).values(**values)
        )
    else:
        connection.execute(sessions.insert().values(**values))


def _link_resume(connection: Connection, session: Session) -> None:
    if session.resumed_from and session.resumed_from != session.session_id:
        connection.execute(
            update(sessions)
            .where(sessions.c.session_id == session.resumed_from)
            .values(resumed_by=session.session_id)
        )


def project_session(
    connection: Connection, session_id: str, token_id: str = ""
) -> Session:
    """Recompute one session from every event stored for it and write it."""

    stored = _session_events(connection, session_id)
    session = Session(session_id=session_id, token_id=token_id)
    terminal_seq: int | None = None

    for row in stored:
        payload = dict(row.payload)
        labels = dict(row.labels or {})

        fold(session, payload, labels)

        if row.kind in {"workshop-finish", "workshop-abandon"}:
            terminal_seq = int(row.seq)

    (
        session.events_received,
        session.events_expected,
        session.gaps,
        session.complete,
    ) = completeness((int(row.seq) for row in stored), terminal_seq)

    existing = _load_session(connection, session_id)

    if existing is not None:
        session.resumed_by = existing.resumed_by

        if not session.token_id:
            session.token_id = existing.token_id

    if session.started_at is None:
        session.started_at = utcnow()
        session.last_seen = session.started_at

    _write_session(connection, session, existing is not None)
    _link_resume(connection, session)

    return session


def apply_events(
    connection: Connection, stored: Sequence[dict[str, Any]], token_id: str = ""
) -> list[Session]:
    """Project every session the given newly stored events belong to."""

    touched = list(dict.fromkeys(str(event.get("session_id", "")) for event in stored))

    return [
        project_session(connection, session_id, token_id)
        for session_id in touched
        if session_id
    ]


def rebuild(engine: Engine) -> int:
    """Drop and recompute every sessions row from the events table."""

    with engine.begin() as connection:
        connection.execute(delete(sessions))

        ids = list(
            connection.execute(
                select(events.c.session_id).distinct().order_by(events.c.session_id)
            ).scalars()
        )

        for session_id in ids:
            token_id = connection.execute(
                select(events.c.token_id)
                .where(events.c.session_id == session_id)
                .order_by(events.c.id)
                .limit(1)
            ).scalar_one_or_none()

            project_session(connection, session_id, str(token_id or ""))

    return len(ids)


def status_of(row: Any, now: datetime, settings: Settings) -> str:
    """The status of a session at a moment, by the service's thresholds."""

    if row.resumed_by:
        return "resumed"

    if row.finished_at is not None:
        return "finished"

    if row.abandoned_at is not None:
        return "abandoned"

    quiet = (now - row.last_seen).total_seconds()
    hidden = bool(row.last_hidden) and row.last_heartbeat == row.last_seen

    if hidden:
        if quiet <= settings.away_allowance:
            return "away"
    elif quiet <= settings.active_allowance:
        return "active"

    if quiet < settings.silent_limit:
        return "silent"

    return "lost"


def page_position(row: Any) -> tuple[int, int]:
    """The current page's one-based position and the page count."""

    pages = list(row.pages or [])
    ids = [str(page.get("id", "")) for page in pages]

    if row.current_page in ids:
        return ids.index(row.current_page) + 1, len(ids)

    return 0, len(ids)


def page_details(row: Any) -> dict[str, str]:
    """The current page's file name and title, for display."""

    for page in row.pages or []:
        if str(page.get("id", "")) == row.current_page:
            path = str(page.get("path", ""))

            return {
                "id": row.current_page,
                "file": path.rsplit("/", 1)[-1],
                "title": str(page.get("title", "")),
            }

    return {"id": row.current_page, "file": row.current_page, "title": ""}


def live_row(row: Any, now: datetime, settings: Settings) -> dict[str, Any]:
    """The dashboard's view of one session."""

    position, count = page_position(row)
    page = page_details(row)
    ended = row.finished_at or row.abandoned_at

    # Time in the workshop: to now while it runs, to its end once it
    # has one. A resumed session counts from its own start, the resume.
    elapsed = max(0, int(((ended or now) - row.started_at).total_seconds()))

    return {
        "session_id": row.session_id,
        "short_id": row.session_id[-8:],
        "instance_id": row.instance_id,
        "name": row.name,
        "collection": row.collection,
        "collection_title": row.collection_title,
        "workshop": row.workshop,
        "version": row.version,
        "frontend": row.frontend,
        "host": row.host,
        "platform": row.platform,
        "user": row.user,
        "labels": dict(row.labels or {}),
        "status": status_of(row, now, settings),
        "started_at": _iso(row.started_at),
        "last_seen": _iso(row.last_seen),
        "quiet_seconds": int((now - row.last_seen).total_seconds()),
        "elapsed_seconds": elapsed,
        "ended_at": _iso(ended) if ended else "",
        "page": page,
        "page_position": position,
        "page_count": count,
        "pages_done": int(row.pages_done or 0),
        "recent": list(row.recent or []),
        "events_received": int(row.events_received or 0),
        "events_expected": int(row.events_expected or 0),
        "gaps": list(row.gaps or []),
        "complete": bool(row.complete),
        "resumed_from": row.resumed_from,
        "restarted_from": row.restarted_from,
    }


def live_rows(
    engine: Engine, now: datetime, settings: Settings
) -> list[dict[str, Any]]:
    """The sessions the live view shows: live ones and recently ended ones."""

    horizon = now - timedelta(seconds=max(settings.silent_limit, settings.linger))

    with engine.connect() as connection:
        rows = list(
            connection.execute(
                select(sessions)
                .where(sessions.c.last_seen >= horizon)
                .order_by(sessions.c.started_at)
            )
        )

    shown: list[dict[str, Any]] = []

    for row in rows:
        status = status_of(row, now, settings)

        if status in LIVE_STATUSES:
            shown.append(live_row(row, now, settings))
        elif status in {"finished", "abandoned"}:
            ended = row.finished_at or row.abandoned_at

            if ended is not None and (now - ended).total_seconds() <= settings.linger:
                shown.append(live_row(row, now, settings))

    return shown


def _iso(moment: datetime | None) -> str:
    if moment is None:
        return ""

    return moment.replace(microsecond=0).isoformat() + "Z"
