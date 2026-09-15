"""Engine construction, with the SQLite pragmas the service wants."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool


def is_sqlite(url: str) -> bool:
    """Whether a database URL names SQLite."""

    return make_url(url).get_backend_name() == "sqlite"


def sqlite_path(url: str) -> Path | None:
    """The file behind a SQLite URL, or None for an in-memory database."""

    database = make_url(url).database

    if not database or database == ":memory:":
        return None

    return Path(database)


def make_engine(url: str) -> Engine:
    """An engine for the URL, SQLite in WAL mode with NORMAL syncing.

    A file-backed SQLite database has its directory created, so a fresh
    volume is usable at once; an in-memory one is held on a single
    shared connection so every thread sees the same database.
    """

    if not is_sqlite(url):
        return create_engine(url, pool_pre_ping=True)

    path = sqlite_path(url)
    options: dict[str, Any] = {"connect_args": {"check_same_thread": False}}

    if path is None:
        options["poolclass"] = StaticPool
    else:
        path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, **options)

    @event.listens_for(engine, "connect")
    def configure(connection: Any, record: Any) -> None:
        cursor = connection.cursor()

        try:
            if path is not None:
                cursor.execute("PRAGMA journal_mode=WAL")

            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine
