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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


def variant(
    events: list[dict[str, Any]],
    session_id: str,
    *,
    instance_id: str = "",
    collection: str | None = None,
    user: str = "",
    drop: tuple[int, ...] = (),
    keep: int | None = None,
    shift: timedelta | None = None,
) -> list[dict[str, Any]]:
    """Copies of a fixture's events as another session, altered as asked.

    `drop` leaves out events by `seq`, `keep` keeps only the first so
    many, `shift` moves every timestamp, and the rest re-identify the
    session; together they make the gapped, headless, lost, live and
    collected sessions the query tests need from the three recordings.
    """

    copies: list[dict[str, Any]] = []

    for event in events[:keep] if keep is not None else events:
        if event["seq"] in drop:
            continue

        copy = dict(event, session_id=session_id)

        if instance_id:
            copy["instance_id"] = instance_id

        if collection is not None:
            copy["collection"] = collection

        if user:
            copy["user"] = user

        if shift is not None:
            moment = datetime.fromisoformat(copy["ts"].replace("Z", "+00:00")) + shift
            copy["ts"] = (
                moment.strftime("%Y-%m-%dT%H:%M:%S.")
                + f"{moment.microsecond // 1000:03d}Z"
            )

        copies.append(copy)

    return copies


def resumed(
    events: list[dict[str, Any]],
    session_id: str,
    resumed_from: str,
    *,
    after: int,
    shift: timedelta,
    user: str = "",
) -> list[dict[str, Any]]:
    """The tail of a recording as a session resuming another.

    The events after `seq` `after` are renumbered from 2 behind a
    `workshop-resume` built from the recording's start event, so the
    chain reads as the recording did: a stop, and a later resume from
    where it left off.
    """

    start = next(event for event in events if event["kind"] == "workshop-start")
    tail = [event for event in events if event["seq"] > after]
    first_page = next(
        (event["page"] for event in tail if event.get("page")), start["page"]
    )
    resume = dict(
        start,
        kind="workshop-resume",
        page=first_page,
        resumed_from=resumed_from,
        seq=1,
        ts=tail[0]["ts"],
    )

    resume.pop("restarted_from", None)

    renumbered = [resume] + [
        dict(event, seq=index) for index, event in enumerate(tail, start=2)
    ]

    return variant(renumbered, session_id, user=user, shift=shift)


COLLECTION = "https://example.org/collection.json"


@dataclass
class Seeded:
    """What `seed()` put in a store, by the ids the tests refer to."""

    ingest_token: str
    token_id: str
    hello: list[dict[str, Any]]
    why: list[dict[str, Any]]
    guided: list[dict[str, Any]]
    complete: str
    gapped: str
    headless: str
    lost: str
    live: str
    part1: str
    part2: str


def seed(app: Any, key: bytes) -> Seeded:
    """Fill the application's store with sessions of every shape.

    From the three recordings: a complete session posted under a token
    with a label, a gapped one, a headless one, a lost one, a live one,
    a two-session chain with an identified learner, the same workshop
    under two collections, and a collection two instances took.
    """

    token, claims = mint(key, labels={"course": "intro"}, name="class")
    ingest = app.state.ingest
    hello = load_fixture("hello-jupyterlab")
    why = load_fixture("why-a-workshop")
    guided = load_fixture("guided-not-documented")
    hour = timedelta(hours=1)

    ingest.accept(hello, claims)
    ingest.accept(
        variant(hello, "hello-gap", instance_id="inst-gap", drop=(10, 11, 12)),
        None,
        extra_labels={"course": "advanced"},
    )
    ingest.accept(
        variant(hello, "hello-headless", instance_id="inst-head", drop=(1, 2, 3)), None
    )
    ingest.accept(
        variant(hello, "hello-lost", instance_id="inst-lost", user="bob", keep=30), None
    )
    ingest.accept(
        variant(shifted_to_now(hello), "hello-live", instance_id="inst-live", keep=20),
        None,
    )
    ingest.accept(
        variant(hello, "hello-part1", instance_id="inst-chain", user="alice", keep=25),
        None,
    )
    ingest.accept(
        resumed(
            hello, "hello-part2", "hello-part1", after=25, shift=2 * hour, user="alice"
        ),
        None,
    )
    ingest.accept(why, None)
    ingest.accept(
        variant(why, "why-coll", instance_id="inst-coll", collection=COLLECTION), None
    )
    ingest.accept(
        variant(
            guided,
            "guided-coll",
            instance_id="inst-coll",
            collection=COLLECTION,
            shift=hour,
        ),
        None,
    )
    ingest.accept(
        variant(
            guided,
            "guided-coll-2",
            instance_id="inst-coll-2",
            collection=COLLECTION,
            keep=8,
        ),
        None,
    )

    return Seeded(
        ingest_token=token,
        token_id=claims.jti,
        hello=hello,
        why=why,
        guided=guided,
        complete=hello[0]["session_id"],
        gapped="hello-gap",
        headless="hello-headless",
        lost="hello-lost",
        live="hello-live",
        part1="hello-part1",
        part2="hello-part2",
    )


@pytest.fixture
def seeded(app: Any, key: bytes) -> Seeded:
    """The application's store filled by `seed()`."""

    return seed(app, key)
