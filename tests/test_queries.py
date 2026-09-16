"""The query layer: every question, asked of a store seeded with every shape."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection

from workshop_analytics import queries
from workshop_analytics.config import Settings
from workshop_analytics.queries import (
    AmbiguousWorkshop,
    EventFilters,
    Filters,
    NotFound,
    QueryError,
    percentiles,
)
from workshop_analytics.selectors import parse_selector
from workshop_analytics.store.writes import utcnow

from .conftest import COLLECTION, Seeded, skipping_gates


@pytest.fixture
def connection(app: Any, seeded: Seeded) -> Iterator[Connection]:
    with app.state.engine.connect() as connection:
        yield connection


@pytest.fixture
def now() -> datetime:
    return utcnow()


HELLO = Filters(name="hello-jupyterlab")


def test_workshops_are_listed_as_name_and_collection_pairs(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    listings = queries.list_workshops(connection, Filters(), now, settings)
    pairs = [(item.name, item.collection) for item in listings]

    assert pairs == [
        ("guided-not-documented", COLLECTION),
        ("hello-jupyterlab", ""),
        ("why-a-workshop", ""),
        ("why-a-workshop", COLLECTION),
    ]

    hello = listings[1]

    assert hello.sessions == 7
    assert hello.journeys == 4
    assert hello.finished == 2
    assert hello.sources == ["local:hello-jupyterlab"]
    assert hello.versions == ["0.1.0"]
    assert hello.identity is True
    assert hello.first_seen == "2026-09-15T03:13:55Z"
    assert hello.last_seen > hello.first_seen


def test_a_name_alone_resolves_only_when_one_collection_has_it(
    connection: Connection,
) -> None:
    assert queries.resolve_workshop(connection, "hello-jupyterlab", None) == ""
    assert queries.resolve_workshop(connection, "guided-not-documented", None) == (
        COLLECTION
    )
    assert queries.resolve_workshop(connection, "why-a-workshop", "") == ""

    with pytest.raises(AmbiguousWorkshop) as ambiguous:
        queries.resolve_workshop(connection, "why-a-workshop", None)

    assert ambiguous.value.collections == ["", COLLECTION]

    with pytest.raises(NotFound):
        queries.resolve_workshop(connection, "nothing", None)


def test_data_quality_leaves_out_gaps_and_missing_heads_unless_asked(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    summary = queries.workshop_summary(connection, HELLO, now, settings)
    quality = summary.data_quality

    assert quality.considered == 7
    assert quality.included == 5
    assert quality.excluded_gaps == 1
    assert quality.excluded_missing_head == 1
    assert quality.open_ended == 1
    assert quality.in_progress == 1
    assert quality.complete == 2
    assert quality.share_complete == round(2 / 7, 4)
    assert quality.include_incomplete is False

    widened = queries.workshop_summary(
        connection, HELLO.replace(include_incomplete=True), now, settings
    )

    assert widened.data_quality.included == 7
    assert widened.data_quality.excluded_gaps == 0
    assert widened.total.sessions == 7


def test_finishes_that_skipped_gates_are_told_apart(
    app: Any, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    app.state.ingest.accept(
        skipping_gates(seeded.hello, "hello-skipped", instance_id="inst-skip"), None
    )

    with app.state.engine.connect() as connection:
        summary = queries.workshop_summary(connection, HELLO, now, settings)
        detail = queries.session_detail(connection, "hello-skipped", now, settings)

    # One more finished journey, and it is the one that skipped a gate;
    # the completion rate still counts it as finished.
    assert summary.total.finished == 3
    assert summary.total.finished_skipping_gates == 1
    assert summary.total.completion_rate == round(3 / 4, 4)
    assert detail.session.gates_skipped == 1


def test_outcomes_chain_resumed_sessions_into_journeys(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    summary = queries.workshop_summary(connection, HELLO, now, settings)
    total = summary.total

    # Five sessions, four journeys: the two-session chain is one.
    assert total.sessions == 5
    assert total.journeys == 4
    assert total.starts == 4
    assert total.resumes == 1
    assert total.finished == 2
    assert total.lost == 1
    assert total.in_progress == 1
    assert total.abandoned == 0
    assert total.completion_rate == round(2 / 3, 4)

    # The chain's duration is its segments summed, not first start to
    # last end, so the two hours between the stop and the resume are
    # not counted.
    assert total.duration_seconds is not None
    assert total.duration_seconds.count == 2
    assert total.duration_seconds.max < 60

    assert summary.group_by == "version"
    assert [group.value for group in summary.groups] == ["0.1.0"]
    assert summary.groups[0].outcomes.journeys == 4


def test_grouping_by_a_field_or_a_label(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    by_label = queries.workshop_summary(
        connection, HELLO, now, settings, group_by="course"
    )
    values = {group.value: group.outcomes.sessions for group in by_label.groups}

    assert values == {"": 4, "intro": 1}

    by_user = queries.workshop_summary(
        connection, HELLO, now, settings, group_by="user"
    )
    users = {group.value: group.outcomes.journeys for group in by_user.groups}

    assert users == {"": 2, "alice": 1, "bob": 1}


def test_filters_narrow_by_columns_labels_status_and_time(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    def count(filters: Filters) -> int:
        return len(queries.load_sessions(connection, filters, now, settings))

    assert count(HELLO.replace(labels=parse_selector("course=intro"))) == 1
    assert (
        count(HELLO.replace(labels=parse_selector("course in (intro,advanced)"))) == 2
    )
    assert count(HELLO.replace(status=("lost",))) == 1
    assert count(HELLO.replace(status=("active", "finished"))) == 5
    assert count(HELLO.replace(user="alice")) == 2
    assert count(HELLO.replace(token_id=seeded.token_id)) == 1
    assert count(HELLO.replace(host="binder")) == 0
    assert count(HELLO.replace(since=now - timedelta(minutes=5))) == 1
    assert count(HELLO.replace(until=now - timedelta(minutes=5))) == 6


def test_the_funnel_counts_entries_and_where_journeys_stop(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    funnel = queries.funnel(connection, HELLO, now, settings)

    assert funnel.journeys == 4
    assert funnel.with_pages == 4
    assert funnel.finished == 2
    assert [step.page.id for step in funnel.steps][:2] == ["01-welcome", "02-notebooks"]
    assert funnel.steps[0].position == 1
    assert funnel.steps[0].entered == 4
    assert funnel.steps[0].left == 4

    # The lost session and the live one stopped short; the finished
    # journeys stopped nowhere.
    assert sum(step.stopped for step in funnel.steps) == 2
    assert funnel.steps[-1].entered == 2
    assert funnel.unlisted_pages == ["06-pip"]


def test_page_timings_come_from_the_leaves(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    timings = queries.page_timings(connection, HELLO, now, settings)
    first = timings.pages[0]

    assert timings.sessions == 5
    assert first.page.id == "01-welcome"
    assert first.sessions == 4
    assert first.entries == 4
    assert first.entries_per_session == 1.0
    assert first.active_seconds is not None
    assert first.active_seconds.count == 4
    assert first.total_active_seconds == pytest.approx(4 * 0.054)

    # The last page is never left, and a page outside the list is
    # appended after the listed ones.
    by_id = {timing.page.id: timing for timing in timings.pages}

    assert by_id["08-finish"].active_seconds is None
    assert timings.pages[-1].page.id == "06-pip"
    assert by_id["06-pip"].page.title == ""


def test_action_usage_counts_triggers_and_outcomes(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    usage = queries.action_usages(connection, HELLO, now, settings)

    assert usage.sessions == 5
    assert usage.runs >= usage.clicked > 0
    assert usage.sessions_clicking == 5

    first = next(action for action in usage.actions if action.id == "01-welcome-1")

    assert first.type == "toast"
    assert first.page == "01-welcome"
    assert first.runs == 4
    assert first.sessions == 4
    assert first.by_trigger == {"click": 4}
    assert first.ok == 4
    assert first.error == 0
    assert first.downgraded == 0


def test_checks_report_pass_rates_attempts_and_hints(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    guided = Filters(name="guided-not-documented")
    result = queries.checks(connection, guided, now, settings)
    by_id = {(check.kind, check.id): check for check in result.checks}

    assert result.sessions == 2
    assert ("quiz", "gating") in by_id
    assert by_id[("quiz", "gating")].passed == 1
    assert by_id[("quiz", "gating")].pass_rate == 1.0

    first_note = by_id[("verify", "first-note")]

    assert first_note.sessions == 2
    assert first_note.passed == 2
    assert first_note.attempts_to_pass is not None
    assert first_note.attempts_to_pass.p50 == 1.0
    assert first_note.by_trigger == {"trigger": 2}
    assert result.hints == []
    assert result.gates_skipped == 0


def test_trends_bucket_by_day_and_week_and_list_every_bucket(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    daily = queries.trends(connection, HELLO, now, settings, bucket="day")

    assert daily.bucket == "day"
    assert daily.buckets[0].start == "2026-09-15T00:00:00Z"
    assert daily.buckets[0].end == "2026-09-16T00:00:00Z"
    assert sum(bucket.outcomes.journeys for bucket in daily.buckets) == 4
    assert daily.buckets[0].outcomes.journeys >= 3

    # Every day between the first and the last is present, empty or not.
    starts = [bucket.start for bucket in daily.buckets]

    assert len(starts) == len(set(starts))
    assert all(
        datetime.fromisoformat(b.replace("Z", "+00:00"))
        - datetime.fromisoformat(a.replace("Z", "+00:00"))
        == timedelta(days=1)
        for a, b in zip(starts, starts[1:], strict=False)
    )

    weekly = queries.trends(
        connection, HELLO, now, settings, bucket="week", group_by="user"
    )

    assert weekly.buckets[0].start == "2026-09-14T00:00:00Z"
    assert [group.value for group in weekly.groups] == ["", "alice", "bob"]

    with pytest.raises(QueryError):
        queries.trends(connection, HELLO, now, settings, bucket="month")


def test_sessions_list_newest_first_and_page_by_cursor(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    first = queries.list_sessions(connection, HELLO, now, settings, limit=3)

    assert first.total == 7
    assert len(first.sessions) == 3
    assert first.sessions[0].session_id == seeded.live
    assert first.next_cursor

    second = queries.list_sessions(
        connection, HELLO, now, settings, limit=3, cursor=first.next_cursor
    )
    third = queries.list_sessions(
        connection, HELLO, now, settings, limit=3, cursor=second.next_cursor
    )
    ids = [s.session_id for page in (first, second, third) for s in page.sessions]

    assert len(ids) == 7
    assert len(set(ids)) == 7
    assert third.next_cursor == ""

    gapped = next(
        s
        for s in first.sessions + second.sessions + third.sessions
        if s.session_id == seeded.gapped
    )

    assert gapped.gaps == [[10, 12]]
    assert gapped.complete is False
    assert gapped.labels == {"course": "advanced"}

    with pytest.raises(QueryError):
        queries.list_sessions(connection, HELLO, now, settings, cursor="???")


def test_a_session_detail_has_pages_a_timeline_and_its_chain(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    detail = queries.session_detail(connection, seeded.part2, now, settings)

    assert detail.session.status == "finished"
    assert detail.session.resumed_from == seeded.part1
    assert detail.chain == [seeded.part1, seeded.part2]
    assert detail.timeline[0].kind == "workshop-resume"
    assert detail.timeline[0].detail["resumed_from"] == seeded.part1
    assert detail.timeline[-1].kind == "workshop-finish"
    assert [visit.position for visit in detail.pages][:3] == [1, 2, 3]
    assert any(visit.active_seconds > 0 for visit in detail.pages)

    earlier = queries.session_detail(connection, seeded.part1, now, settings)

    assert earlier.session.status == "resumed"
    assert earlier.chain == [seeded.part1, seeded.part2]

    gapped = queries.session_detail(connection, seeded.gapped, now, settings)
    seqs = [entry.seq for entry in gapped.timeline]

    assert gapped.gaps == [[10, 12]]
    assert 9 in seqs and 13 in seqs and 10 not in seqs

    with pytest.raises(NotFound):
        queries.session_detail(connection, "nothing", now, settings)


def test_a_sessions_raw_events_carry_the_merged_labels(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    events = queries.session_events(connection, seeded.complete, now, settings)

    assert len(events) == 63
    assert events[0]["kind"] == "workshop-start"
    assert events[0]["labels"] == {"course": "intro"}
    assert events[0]["token_id"] == seeded.token_id
    assert events[0]["received_at"]


def test_raw_events_by_filter_and_cursor(
    connection: Connection, seeded: Seeded
) -> None:
    page = queries.query_events(
        connection,
        Filters(name="hello-jupyterlab"),
        EventFilters(kind="verify-result"),
        limit=3,
    )

    assert len(page.events) == 3
    assert page.next_cursor
    assert all(event["kind"] == "verify-result" for event in page.events)

    rest = queries.query_events(
        connection,
        Filters(name="hello-jupyterlab"),
        EventFilters(kind="verify-result"),
        limit=1000,
        cursor=page.next_cursor,
    )

    assert rest.next_cursor == ""
    assert int(rest.events[0]["seq"]) >= 1

    by_status = queries.query_events(
        connection, Filters(), EventFilters(kind="action-executed", status="error")
    )

    assert by_status.events == []

    labelled = queries.query_events(
        connection,
        Filters(labels=parse_selector("course=intro")),
        EventFilters(kind="workshop-finish"),
    )

    assert [event["session_id"] for event in labelled.events] == [seeded.complete]

    with pytest.raises(QueryError):
        queries.query_events(connection, Filters(), EventFilters(), cursor="x")


def test_an_instance_lists_its_sessions_in_order(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    instance = queries.instance(connection, "inst-coll", now, settings)

    assert instance.workshops == ["why-a-workshop", "guided-not-documented"]
    assert [s.status for s in instance.sessions] == ["finished", "finished"]
    assert instance.host == "local"

    with pytest.raises(NotFound):
        queries.instance(connection, "nothing", now, settings)


def test_collections_order_their_workshops_and_report_progress(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    listings = queries.list_collections(connection, Filters(), now, settings)

    assert [listing.collection for listing in listings] == [COLLECTION]
    assert [w.name for w in listings[0].workshops] == [
        "why-a-workshop",
        "guided-not-documented",
    ]
    assert listings[0].instances == 2
    assert listings[0].sessions == 3

    progress = queries.collection_progress(
        connection, Filters(collection=COLLECTION), now, settings
    )

    assert progress.instances == 2
    assert progress.in_order == 2
    assert progress.completed_all == 1
    assert [
        (step.name, step.instances, step.finished, step.stopped)
        for step in progress.steps
    ] == [
        ("why-a-workshop", 1, 1, 0),
        ("guided-not-documented", 2, 1, 1),
    ]

    with pytest.raises(QueryError):
        queries.collection_progress(connection, Filters(), now, settings)

    with pytest.raises(NotFound):
        queries.collection_progress(
            connection, Filters(collection="https://nowhere"), now, settings
        )


def test_learners_group_sessions_by_user(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    result = queries.learners(connection, HELLO, now, settings)
    by_user = {learner.user: learner for learner in result.learners}

    assert set(by_user) == {"alice", "bob"}
    assert result.anonymous_sessions == 4
    assert by_user["alice"].sessions == 2
    assert by_user["alice"].journeys == 1
    assert by_user["alice"].finished is True
    assert by_user["alice"].best_progress == 1.0
    assert by_user["bob"].finished is False
    assert by_user["bob"].last_status == "lost"
    assert 0 < by_user["bob"].best_progress < 1


def test_describe_reports_what_the_store_holds(
    connection: Connection, now: datetime, settings: Settings, seeded: Seeded
) -> None:
    description = queries.describe(connection, now, settings)

    assert description.service["dialect"] == "sqlite"
    assert description.service["schema_version"] == "0.2.0"
    assert "workshop-start" in description.events["kinds"]
    assert "active_ms" in description.events["kinds"]["page-leave"]["fields"]
    assert "seq" in description.events["base_fields"]
    assert set(description.sessions["statuses"]) == {
        "active",
        "away",
        "silent",
        "lost",
        "resumed",
        "finished",
        "abandoned",
    }
    assert "150" in description.sessions["statuses"]["active"]
    assert description.sessions["thresholds"]["silent_limit"] == 1800.0
    assert "completion_rate" in description.metrics
    assert "excluded_gaps" in description.data_quality
    assert "labels" in description.filters
    assert description.data["sessions"] == 11
    assert description.data["workshops"] == 4
    assert description.data["events"] > 200
    assert description.data["tokens"] == [seeded.token_id]
    assert description.collections == [COLLECTION]

    # The course label came from a token on one session and from an
    # import on another; the token's use is the bound one.
    course = next(label for label in description.labels if label.key == "course")

    assert course.values == ["advanced", "intro"]
    assert course.sessions == 2
    assert course.tokens == [seeded.token_id]
    assert course.bound is True

    identity = description.identity

    assert identity["by_host"]["local"] == round(3 / 11, 4)
    assert identity["by_token"][seeded.token_id] == 0.0


def test_percentiles_are_nearest_rank() -> None:
    assert percentiles([]) is None

    result = percentiles([5.0, 1.0, 3.0, 2.0, 4.0])

    assert result is not None
    assert (result.count, result.min, result.max) == (5, 1.0, 5.0)
    assert result.p50 == 3.0
    assert result.p90 == 5.0
    assert result.mean == 3.0


def test_moments_parse_from_dates_and_offsets() -> None:
    assert queries.parse_moment("2026-09-15") == datetime(2026, 9, 15)
    assert queries.parse_moment("2026-09-15T03:13:47Z") == datetime(
        2026, 9, 15, 3, 13, 47
    )
    assert queries.parse_moment("2026-09-15T13:13:47+10:00") == datetime(
        2026, 9, 15, 3, 13, 47
    )

    with pytest.raises(ValueError):
        queries.parse_moment("yesterday")
