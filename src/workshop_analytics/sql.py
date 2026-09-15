"""The read-only SQL tool: one SELECT at a time, bounded, on a second engine.

The curated queries answer the common questions portably; this tool
answers the ones nobody wrote an endpoint for, joins and sequences and
cohorts, in the store's own dialect. It is safe because of what it
cannot do: the engine is opened read-only, a statement must be a
single SELECT or WITH, it is interrupted at a deadline, and its rows
are capped. The data holds no secrets, since events never carry file
contents, command output, form answers or variable values.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from .config import Settings
from .store.engine import is_sqlite, sqlite_path
from .store.tables import metadata

ALLOWED_FIRST_WORDS = frozenset({"SELECT", "WITH", "EXPLAIN"})

PROGRESS_EVERY = 10_000

FIRST_WORD = re.compile(r"^(?:\s|--[^\n]*\n?|/\*.*?\*/)*(\w+)", re.S)


class SqlError(ValueError):
    """A statement the tool refuses, or one that failed."""


class SqlDisabled(SqlError):
    """The tool is off for this deployment."""


class SqlTimeout(SqlError):
    """The statement ran past the deadline and was interrupted."""


@dataclass
class SqlResult:
    """The rows a statement returned, with what was done to them."""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float


def check_statement(sql: str) -> str:
    """The statement if it is a single SELECT, or a `SqlError` saying why not.

    A trailing semicolon is allowed and removed; any other semicolon is
    refused, since one statement is all the tool runs. `WITH` is allowed
    because the interesting questions are recursive, and the read-only
    engine refuses a `WITH` that writes.
    """

    stripped = sql.strip()

    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()

    if not stripped:
        raise SqlError("the statement is empty")

    if ";" in stripped:
        raise SqlError("one statement at a time: a semicolon may only end it")

    match = FIRST_WORD.match(stripped)
    word = match.group(1).upper() if match else ""

    if word not in ALLOWED_FIRST_WORDS:
        raise SqlError(
            f"only SELECT, WITH or EXPLAIN statements are run, not {word or 'that'}"
        )

    return stripped


def plain_value(value: Any) -> Any:
    """A cell as JSON can carry it."""

    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat() + "Z"

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, memoryview):
        value = value.tobytes()

    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"

    return value


def read_only_url(url: str) -> str:
    """The URL of a read-only engine on the same database.

    SQLite is opened with `mode=ro` through a URI; every other dialect
    keeps its URL and relies on the transaction being read-only and on
    the role the deployment gives the URL's user.
    """

    if not is_sqlite(url):
        return url

    path = sqlite_path(url)

    if path is None:
        raise SqlDisabled("the SQL tool needs a file-backed database")

    return f"sqlite:///file:{path.resolve()}?mode=ro&uri=true"


class SqlTool:
    """The tool bound to a deployment's settings and store."""

    def __init__(self, settings: Settings) -> None:
        self.timeout = float(settings.sql_timeout)
        self.max_rows = int(settings.sql_max_rows)
        self.engine: Engine | None = None
        self.reason = ""

        if not settings.sql_tool:
            self.reason = "the SQL tool is turned off by SQL_TOOL"

            return

        try:
            url = read_only_url(settings.database_url)
        except SqlDisabled as error:
            self.reason = str(error)

            return

        self.engine = make_read_only_engine(url)

    @property
    def enabled(self) -> bool:
        """Whether statements can be run."""

        return self.engine is not None

    def run(
        self,
        sql: str,
        params: Mapping[str, Any] | None = None,
        limit: int | None = None,
    ) -> SqlResult:
        """Run one statement and return its rows, capped and converted.

        `limit` caps the rows below the deployment's cap; one more row
        than the cap is fetched so `truncated` can be said honestly.
        """

        if self.engine is None:
            raise SqlDisabled(self.reason)

        statement = check_statement(sql)
        cap = self.max_rows if limit is None else max(1, min(limit, self.max_rows))
        started = time.monotonic()

        try:
            with self.engine.connect() as connection:
                rows, columns = self._execute(
                    connection, statement, dict(params or {}), cap + 1
                )
        except SqlError:
            raise
        except DBAPIError as error:
            if interrupted(error):
                raise SqlTimeout(
                    f"the statement was stopped after {self.timeout:g} seconds"
                ) from error

            raise SqlError(str(error.orig or error)) from error
        except SQLAlchemyError as error:
            raise SqlError(str(error)) from error

        truncated = len(rows) > cap

        return SqlResult(
            columns=columns,
            rows=[[plain_value(cell) for cell in row] for row in rows[:cap]],
            row_count=min(len(rows), cap),
            truncated=truncated,
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        )

    def _execute(
        self,
        connection: Connection,
        statement: str,
        params: dict[str, Any],
        fetch: int,
    ) -> tuple[list[Any], list[str]]:
        """Run the statement under the dialect's deadline and read-only guard."""

        if connection.dialect.name == "sqlite":
            with sqlite_deadline(connection, self.timeout):
                result = connection.execute(text(statement), params)

                return list(result.fetchmany(fetch)), list(result.keys())

        # PostgreSQL and the rest: a read-only transaction with the
        # server's own statement timeout, both scoped to this transaction.
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(
                text(f"SET LOCAL statement_timeout = {int(self.timeout * 1000)}")
            )

            result = connection.execute(text(statement), params)

            return list(result.fetchmany(fetch)), list(result.keys())


class sqlite_deadline:
    """Interrupt a SQLite connection's statement once a deadline passes.

    SQLite calls the progress handler every so many virtual machine
    steps; returning non-zero from it aborts the statement with
    "interrupted", which the caller turns into a timeout.
    """

    def __init__(self, connection: Connection, timeout: float) -> None:
        self.raw: sqlite3.Connection = connection.connection.dbapi_connection  # type: ignore[assignment]
        self.deadline = time.monotonic() + timeout

    def __enter__(self) -> None:
        deadline = self.deadline

        def interrupt() -> int:
            return 1 if time.monotonic() > deadline else 0

        self.raw.set_progress_handler(interrupt, PROGRESS_EVERY)

    def __exit__(self, *exc: object) -> None:
        self.raw.set_progress_handler(None, 0)


def interrupted(error: DBAPIError) -> bool:
    """Whether a driver error is the deadline firing."""

    message = str(error.orig or error).lower()

    return "interrupted" in message or "statement timeout" in message


def make_read_only_engine(url: str) -> Engine:
    """An engine that can only read.

    On SQLite the URI already says `mode=ro`; `query_only` is set as
    well so a statement that slips past the first guard still cannot
    write, and a busy timeout lets a reader wait out a checkpoint.
    """

    if not is_sqlite(url):
        return create_engine(url, pool_pre_ping=True)

    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def configure(connection: Any, record: Any) -> None:
        cursor = connection.cursor()

        try:
            cursor.execute("PRAGMA query_only=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    return engine


SQL_NOTES = [
    "One statement at a time, a SELECT, a WITH or an EXPLAIN; a semicolon "
    "may only end it.",
    "Named parameters are written :name in the statement and given in params.",
    "labels and payload are JSON columns: on SQLite read a field with "
    "json_extract(payload, '$.field') or payload ->> '$.field'; on "
    "PostgreSQL with payload ->> 'field'.",
    "Timestamps (ts, received_at, started_at, last_seen, last_heartbeat, "
    "finished_at, abandoned_at) are naive UTC; on SQLite they are stored "
    "and returned as text like 2026-09-15 03:13:55.747000, which sorts and "
    "compares as strings of that form and which datetime() and strftime() "
    "read.",
    "The extracted columns on events (kind, session_id, seq, page, "
    "action_id, status, user and the identity fields) answer most "
    "questions without touching payload.",
    "Session status is not stored; derive it from finished_at, "
    "abandoned_at, resumed_by, last_seen and last_hidden, or ask the "
    "curated queries, which apply the deployment's thresholds.",
    "Rows are capped and the statement is stopped at the deadline; "
    "aggregate in SQL rather than fetching events to count them.",
]


def describe_sql(settings: Settings, dialect: str) -> dict[str, Any]:
    """What `describe` says about the tool: its state, limits and tables."""

    enabled = bool(settings.sql_tool)
    reason = "" if enabled else "the SQL tool is turned off by SQL_TOOL"

    if enabled and is_sqlite(settings.database_url):
        if sqlite_path(settings.database_url) is None:
            enabled = False
            reason = "the SQL tool needs a file-backed database"

    tables = {
        table.name: {
            "columns": {column.name: str(column.type) for column in table.columns},
            "primary_key": [column.name for column in table.primary_key.columns],
            "indexes": [
                [column.name for column in index.columns] for index in table.indexes
            ],
        }
        for table in metadata.sorted_tables
    }

    return {
        "enabled": enabled,
        "reason": reason,
        "dialect": dialect,
        "route": "POST /api/sql",
        "tool": "sql",
        "timeout_seconds": settings.sql_timeout,
        "max_rows": settings.sql_max_rows,
        "tables": tables,
        "notes": list(SQL_NOTES),
    }
