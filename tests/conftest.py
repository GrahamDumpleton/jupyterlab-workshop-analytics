"""Fixtures shared by the suite.

The wrapture pytest plugin gives every test the leak sweep and the
`tape` fixture. The store fixtures give each test a fresh, migrated
SQLite database in its own temporary directory, and the app fixtures
build the application through the factory with tokens minted by the
same code the CLI uses.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from workshop_analytics.app import create_app
from workshop_analytics.config import Settings
from workshop_analytics.store import make_engine, migrate
from workshop_analytics.tokens import (
    Claims,
    decode_key,
    generate_key,
    issue,
    now,
    parse_expiry,
)

pytest_plugins = ["wrapture.pytest_plugin"]

FIXTURES = Path(__file__).with_name("fixtures")

FIXTURE_EXTENSION_VERSION = "0.2.0"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run async tests on asyncio only."""

    return "asyncio"


@pytest.fixture
def key_text() -> str:
    """A fresh base64 signing key, as the environment would carry it."""

    return generate_key()


@pytest.fixture
def key(key_text: str) -> bytes:
    """The decoded signing key."""

    return decode_key(key_text)


@pytest.fixture
def settings(tmp_path: Path, key_text: str) -> Settings:
    """Settings for a temporary SQLite store and the test key."""

    return Settings(
        database_url=f"sqlite:///{tmp_path / 'analytics.db'}",
        signing_key=key_text,
        denied_tokens_file=tmp_path / "denied.txt",
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    """A migrated engine on the temporary store."""

    engine = make_engine(settings.database_url)

    migrate(engine)

    yield engine

    engine.dispose()


@pytest.fixture
def app(settings: Settings) -> Iterator[Any]:
    """The application built by the factory."""

    app = create_app(settings)

    yield app

    app.state.engine.dispose()


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """An async HTTP client speaking ASGI to the app in the same thread.

    In-process ASGI keeps the test's recording context flowing into the
    request, which a threaded test client would not.
    """

    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def mint(
    key: bytes,
    scopes: tuple[str, ...] = ("ingest",),
    *,
    name: str = "test",
    labels: dict[str, str] | None = None,
    origins: tuple[str, ...] = (),
    expires: str = "1d",
    not_before: int | None = None,
) -> tuple[str, Claims]:
    """Sign a token with the CLI's own issuing code."""

    return issue(
        key,
        name=name,
        expires=parse_expiry(expires, now()),
        labels=labels,
        origins=origins,
        scopes=scopes,
        not_before=not_before,
    )


@pytest.fixture
def ingest_token(key: bytes) -> str:
    """A token with the ingest scope and a label."""

    return mint(key, labels={"deployment": "test"})[0]


@pytest.fixture
def api_token(key: bytes) -> str:
    """A token with the api scope."""

    return mint(key, ("api",), name="analyst")[0]


@pytest.fixture
def dashboard_token(key: bytes) -> str:
    """A token with the dashboard scope."""

    return mint(key, ("dashboard",), name="supervisor")[0]


def load_fixture(name: str) -> list[dict[str, Any]]:
    """The events of a fixture file, recorded by extension 0.2.0."""

    path = FIXTURES / f"{name}.jsonl"
    events: list[dict[str, Any]] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))

    return events


def shifted_to_now(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copies of the events with their timestamps moved so the last is now.

    Fixture files are frozen at the moment they were recorded; a test of
    the live view needs a session that reads as current whenever it runs.
    """

    last = datetime.fromisoformat(events[-1]["ts"].replace("Z", "+00:00"))
    offset = datetime.now(UTC) - last
    shifted: list[dict[str, Any]] = []

    for event in events:
        moment = datetime.fromisoformat(event["ts"].replace("Z", "+00:00")) + offset
        text = (
            moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
        )

        shifted.append(dict(event, ts=text))

    return shifted


def ndjson(events: list[Any]) -> bytes:
    """Events as the JSON lines body the extension posts."""

    return "".join(
        json.dumps(event, separators=(",", ":")) + "\n" for event in events
    ).encode("utf-8")


def bearer(token: str) -> dict[str, str]:
    """The headers for a bearer token and an NDJSON body."""

    return {
        "authorization": f"Bearer {token}",
        "content-type": "application/x-ndjson",
    }
