"""The sessions projection: folding, completeness, status and rebuild."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import wrapture
from sqlalchemy import Engine, select

from workshop_analytics import projection
from workshop_analytics.config import Settings
from workshop_analytics.ingest import Ingest
from workshop_analytics.live import Broadcaster
from workshop_analytics.projection import (
    completeness,
    live_rows,
    rebuild,
    status_of,
)
from workshop_analytics.schema import EventValidator
from workshop_analytics.store.tables import sessions
from workshop_analytics.store.writes import parse_timestamp

from .conftest import load_fixture

GOLDEN = Path(__file__).with_name("golden") / "projection.txt"


@pytest.fixture
def ingest(engine: Engine, settings: Settings) -> Ingest:
    return Ingest(
        engine=engine,
        settings=settings,
        validator=EventValidator(),
        broadcaster=Broadcaster(),
    )


def session_row(engine: Engine, session_id: str) -> Any:
    with engine.connect() as connection:
        return connection.execute(
            select(sessions).where(sessions.c.session_id == session_id)
        ).one()


def all_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row._mapping)
            for row in connection.execute(
                select(sessions).order_by(sessions.c.session_id)
            )
        ]


def test_a_complete_session_projects_whole(ingest: Ingest, engine: Engine) -> None:
    events = load_fixture("hello-jupyterlab")
    result = ingest.accept(events, None)
    row = session_row(engine, events[0]["session_id"])

    assert result.stored == 63
    assert row.name == "hello-jupyterlab"
    assert row.frontend == "jupyterlab"
    assert row.frontend_version == "0.2.0"
    assert row.host == "local"
    entered = list(
        dict.fromkeys(e["page"] for e in events if e["kind"] == "page-enter")
    )

    assert len(row.pages) == 7
    assert row.pages_entered == entered
    assert len(entered) == 8
    assert row.pages_done == 7
    assert row.finished_at is not None
    assert row.terminal is True
    assert row.events_received == 63
    assert row.events_expected == 63
    assert row.gaps == []
    assert row.complete is True
    assert [entry["kind"] for entry in row.recent][-1] == "action-executed"
    assert len(row.recent) == 5


def test_a_gap_in_the_middle_is_a_delivery_failure(
    ingest: Ingest, engine: Engine
) -> None:
    events = load_fixture("hello-jupyterlab")
    missing = {seq for seq in range(10, 13)}

    ingest.accept([e for e in events if e["seq"] not in missing], None)

    row = session_row(engine, events[0]["session_id"])

    assert row.gaps == [[10, 12]]
    assert row.events_received == 60
    assert row.events_expected == 63
    assert row.complete is False
    assert row.terminal is True

    # Resending the whole file closes the gap through dedupe.
    result = ingest.accept(events, None)
    row = session_row(engine, events[0]["session_id"])

    assert result.stored == 3
    assert result.duplicates == 60
    assert row.gaps == []
    assert row.complete is True


def test_a_missing_tail_is_open_ended(
    ingest: Ingest, engine: Engine, settings: Settings
) -> None:
    events = load_fixture("hello-jupyterlab")

    ingest.accept(events[:-5], None)

    row = session_row(engine, events[0]["session_id"])

    assert row.terminal is False
    assert row.finished_at is None
    assert row.events_expected == 58
    assert row.gaps == []
    assert row.complete is False

    later = row.last_seen + timedelta(seconds=settings.silent_limit + 1)

    assert status_of(row, later, settings) == "lost"


def test_a_missing_head_leaves_no_page_list(ingest: Ingest, engine: Engine) -> None:
    events = load_fixture("why-a-workshop")

    ingest.accept(events[3:], None)

    row = session_row(engine, events[0]["session_id"])

    assert row.pages == []
    assert row.gaps == [[1, 3]]
    assert row.events_received == 20
    assert row.events_expected == 23
    assert row.complete is False
    assert row.terminal is True
    assert row.name == "why-a-workshop"


def test_completeness_rules() -> None:
    assert completeness([], None) == (0, 0, [], False)
    assert completeness([1, 2, 3], 3) == (3, 3, [], True)
    assert completeness([1, 2, 3], None) == (3, 3, [], False)
    assert completeness([1, 3, 6, 7], 7) == (4, 7, [[2, 2], [4, 5]], False)
    assert completeness([2, 3], None) == (2, 3, [[1, 1]], False)


def test_a_resume_links_the_chain_and_closes_the_old_session(
    ingest: Ingest, engine: Engine, settings: Settings
) -> None:
    events = load_fixture("guided-not-documented")
    first = events[0]["session_id"]

    ingest.accept(events[:10], None)

    resumed = [dict(e) for e in events[10:]]
    resume = dict(events[0])

    resume.update(
        kind="workshop-resume",
        session_id="resumed-1",
        instance_id="instance-2",
        seq=1,
        resumed_from=first,
        ts="2026-09-16T09:00:00.000Z",
    )

    for index, event in enumerate(resumed, start=2):
        event["session_id"] = "resumed-1"
        event["instance_id"] = "instance-2"
        event["seq"] = index

    ingest.accept([resume, *resumed], None)

    old = session_row(engine, first)
    new = session_row(engine, "resumed-1")
    now = parse_timestamp("2026-09-16T09:00:05.000Z")

    assert old.resumed_by == "resumed-1"
    assert status_of(old, now, settings) == "resumed"
    assert new.resumed_from == first
    assert new.finished_at is not None
    assert new.pages == old.pages


def test_status_follows_heartbeats_and_silence(
    ingest: Ingest, engine: Engine, settings: Settings
) -> None:
    events = load_fixture("why-a-workshop")
    session_id = events[0]["session_id"]
    base = next(event for event in events if event["kind"] == "page-enter")
    beat = dict(base)

    beat.update(
        kind="heartbeat",
        seq=6,
        hidden=False,
        ts="2026-09-15T04:00:00.000Z",
    )

    ingest.accept(events[:5] + [beat], None)

    row = session_row(engine, session_id)
    at = parse_timestamp("2026-09-15T04:00:00.000Z")

    assert status_of(row, at + timedelta(seconds=60), settings) == "active"
    assert status_of(row, at + timedelta(seconds=200), settings) == "silent"
    assert status_of(row, at + timedelta(seconds=1900), settings) == "lost"

    hidden = dict(beat, seq=7, hidden=True, ts="2026-09-15T04:01:00.000Z")

    ingest.accept([hidden], None)

    row = session_row(engine, session_id)
    at = parse_timestamp("2026-09-15T04:01:00.000Z")

    assert status_of(row, at + timedelta(seconds=600), settings) == "away"
    assert status_of(row, at + timedelta(seconds=800), settings) == "silent"

    # Progress made after a hidden beat reads as active again.
    enter = dict(base, kind="page-enter", seq=8, ts="2026-09-15T04:02:00.000Z")

    ingest.accept([enter], None)

    row = session_row(engine, session_id)
    at = parse_timestamp("2026-09-15T04:02:00.000Z")

    assert status_of(row, at + timedelta(seconds=30), settings) == "active"


def test_the_live_view_shows_live_and_recently_ended_sessions(
    ingest: Ingest, engine: Engine, settings: Settings
) -> None:
    events = load_fixture("why-a-workshop")

    ingest.accept(events, None)

    finished_at = parse_timestamp(events[-1]["ts"])
    soon = finished_at + timedelta(seconds=settings.linger - 1)
    late = finished_at + timedelta(seconds=settings.linger + 1)

    shown = live_rows(engine, soon, settings)

    assert [row["status"] for row in shown] == ["finished"]
    assert shown[0]["page_count"] == 5
    assert shown[0]["page"]["file"].endswith(".md")
    assert live_rows(engine, late, settings) == []

    # Elapsed time runs from the start to the end once there is one,
    # so it reads the same however long the row lingers.
    started_at = parse_timestamp(events[0]["ts"])
    spent = int((finished_at - started_at).total_seconds())

    assert shown[0]["elapsed_seconds"] == spent
    assert live_rows(engine, finished_at, settings)[0]["elapsed_seconds"] == spent


def test_rebuild_reproduces_the_projection(ingest: Ingest, engine: Engine) -> None:
    for name in ("why-a-workshop", "guided-not-documented", "hello-jupyterlab"):
        ingest.accept(load_fixture(name), None, {"course": name})

    before = all_rows(engine)

    assert rebuild(engine) == 3
    assert all_rows(engine) == before
    assert all(row["labels"] == {"course": row["name"]} for row in before)


def test_the_projection_call_tree_matches_the_golden_file(
    ingest: Ingest, engine: Engine
) -> None:
    group = wrapture.discover(projection, "*", exclude="_*")
    events = load_fixture("why-a-workshop")

    with wrapture.timeline(group) as tape:
        ingest.accept(events, None)

    rendered = wrapture.canonical(tape)

    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(rendered)

    assert rendered == GOLDEN.read_text()


def test_timestamps_parse_to_naive_utc() -> None:
    assert parse_timestamp("2026-09-15T03:13:38.651Z") == datetime(
        2026, 9, 15, 3, 13, 38, 651000
    )
    assert parse_timestamp("2026-09-15T13:13:38+10:00") == datetime(
        2026, 9, 15, 3, 13, 38
    )

    with pytest.raises(ValueError):
        parse_timestamp(12)
