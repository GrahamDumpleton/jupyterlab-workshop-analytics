"""What the dashboard's report pages compute on top of the queries.

The pages render from `queries` directly, so every number on them is
one the API answers. This module holds the little that is the page's
own: the period picker and the bucket it implies, the geometry of the
inline SVG charts, the sparklines, the "needs attention" card derived
from the reports, the CSV rows of a session listing, and the query
strings the pages link each other with.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from .queries import (
    CheckOutcome,
    Checks,
    Coverage,
    Funnel,
    FunnelStep,
    Outcomes,
    SessionSummary,
    TrendBucket,
    Trends,
    bucket_length,
    bucket_start,
    iso,
    parse_moment,
)

PERIODS: dict[str, int] = {"7": 7, "30": 30, "90": 90, "365": 365, "all": 0}

PERIOD_LABELS: dict[str, str] = {
    "7": "7 days",
    "30": "30 days",
    "90": "90 days",
    "365": "A year",
    "all": "All time",
}

OUTCOME_SERIES: tuple[tuple[str, str], ...] = (
    ("finished", "Finished"),
    ("abandoned", "Abandoned"),
    ("lost", "Lost"),
    ("in_progress", "In progress"),
)

SPARKLINE_DAYS = 30

CHART_WIDTH = 960

CHART_HEIGHT = 240

PALETTE_SIZE = 8

MAX_SLOT_WIDTH = 96.0


# Periods and buckets


@dataclass(frozen=True)
class Period:
    """The span a page reports on: a preset of days, everything, or a custom range.

    `since` and `until` are what the filters get; `days` is the
    preset's length, zero for all time and for a custom range.
    """

    key: str
    label: str
    since: datetime | None
    until: datetime | None
    days: int

    @property
    def custom(self) -> bool:
        """Whether the range came from explicit `since` or `until` values."""

        return self.key == "custom"


def period_of(params: Mapping[str, str], now: datetime, default: str = "30") -> Period:
    """The period the query parameters ask for.

    An explicit `since` or `until` makes a custom range and wins over
    `period`; a preset counts back whole days so the first bucket is a
    full day and today is the last. A value that is not a preset falls
    back to the default.
    """

    since_text = params.get("since", "").strip()
    until_text = params.get("until", "").strip()

    if since_text or until_text:
        since = parse_moment(since_text) if since_text else None
        until = parse_moment(until_text) if until_text else None

        return Period("custom", "Custom", since, until, 0)

    key = params.get("period", "").strip() or default

    if key not in PERIODS:
        key = default

    days = PERIODS[key]
    since = bucket_start(now - timedelta(days=days - 1), "day") if days else None

    return Period(key, PERIOD_LABELS[key], since, None, days)


def bucket_for(period: Period, params: Mapping[str, str], now: datetime) -> str:
    """Days for a span up to ninety days and weeks beyond, unless forced."""

    forced = params.get("bucket", "").strip()

    if forced in ("day", "week"):
        return forced

    if period.days:
        span = period.days
    elif period.since is not None:
        span = ((period.until or now) - period.since).days
    else:
        span = 0

    return "day" if 0 < span <= 90 else "week"


def query_string(params: Mapping[str, str], **changes: str | None) -> str:
    """A query string of the parameters with some changed, empty ones dropped.

    A change to None removes the parameter; the page's own `cursor`
    never carries over, since a changed selection starts from its
    first page, though a change may set one. `collection` is kept when
    empty because an empty collection means the sessions opened
    outside any.
    """

    merged: dict[str, str] = dict(params)

    merged.pop("cursor", None)

    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value

    pairs = [
        (key, value)
        for key, value in merged.items()
        if value or key == "collection" and value is not None
    ]

    return "?" + urlencode(pairs) if pairs else ""


# Charts


@dataclass
class Segment:
    """One stacked piece of a bar: a series key, its count and its geometry."""

    key: str
    label: str
    value: int
    y: float
    height: float


@dataclass
class Bar:
    """One bar of a chart, with the segments stacked bottom up."""

    label: str
    title: str
    x: float
    width: float
    total: int
    segments: list[Segment]


@dataclass
class Tick:
    """A horizontal gridline and its value."""

    y: float
    label: str


@dataclass
class AxisLabel:
    """A label under the x axis."""

    x: float
    text: str


@dataclass
class Legend:
    """One entry of a chart's legend: the series class and its name."""

    key: str
    label: str


@dataclass
class Chart:
    """The geometry of a stacked or grouped bar chart, ready to draw as SVG."""

    width: int
    height: int
    plot_left: float
    plot_top: float
    plot_right: float
    plot_bottom: float
    bars: list[Bar]
    ticks: list[Tick]
    labels: list[AxisLabel]
    legend: list[Legend]
    total: int

    @property
    def empty(self) -> bool:
        """Whether nothing was counted in the range."""

        return self.total == 0


def nice_step(maximum: int, ticks: int = 4) -> int:
    """A round tick step so the axis reads 1, 2, 5, 10, 20, 50, and so on."""

    if maximum <= ticks:
        return 1

    rough = maximum / ticks
    magnitude = 10 ** math.floor(math.log10(rough))

    for factor in (1, 2, 5, 10):
        if factor * magnitude >= rough:
            return int(factor * magnitude)

    return int(10 * magnitude)


def slots(
    period: Period, buckets: Sequence[TrendBucket], bucket: str, now: datetime
) -> list[datetime]:
    """The bucket starts a chart shows: the whole period, empty buckets included.

    A preset or a custom `since` starts the axis there; all time starts
    at the first bucket with data. The axis runs to the bucket holding
    `until`, or the current one.
    """

    step = bucket_length(bucket)
    first: datetime | None = None

    if period.since is not None:
        first = bucket_start(period.since, bucket)
    elif buckets:
        first = bucket_start(parse_moment(buckets[0].start), bucket)

    if first is None:
        return []

    last = bucket_start(period.until or now, bucket)
    starts: list[datetime] = []
    moment = first

    while moment <= last:
        starts.append(moment)

        moment += step

    return starts


def axis_label(moment: datetime) -> str:
    """A bucket's start as the short date the axis shows."""

    return f"{moment.day} {moment.strftime('%b')}"


def layout(width: int, height: int) -> tuple[float, float, float, float]:
    """The plot area's left, top, right and bottom within the chart."""

    return 40.0, 12.0, width - 8.0, height - 28.0


def chart_of(
    series: Sequence[tuple[str, str]],
    starts: Sequence[datetime],
    bucket: str,
    values: Mapping[tuple[datetime, str], int],
    grouped: bool,
    width: int = CHART_WIDTH,
    height: int = CHART_HEIGHT,
) -> Chart:
    """Lay bars over the buckets, stacked per slot or one per series side by side."""

    left, top, right, bottom = layout(width, height)
    plot_height = bottom - top
    slot_count = max(1, len(starts))
    slot_width = min((right - left) / slot_count, MAX_SLOT_WIDTH)
    step = bucket_length(bucket)

    # The axis scales to the tallest stack, or the tallest single bar
    # when the series stand side by side.
    if grouped:
        maximum = max(
            (values.get((start, key), 0) for start in starts for key, _ in series),
            default=0,
        )
    else:
        maximum = max(
            (sum(values.get((start, key), 0) for key, _ in series) for start in starts),
            default=0,
        )

    tick_step = nice_step(maximum)
    top_value = max(tick_step, math.ceil(maximum / tick_step) * tick_step)
    scale = plot_height / top_value
    bars: list[Bar] = []
    total = 0

    for index, start in enumerate(starts):
        end = start + step
        range_text = f"{iso(start)[:10]} to {iso(end - timedelta(days=1))[:10]}"
        slot_x = left + index * slot_width

        if grouped:
            inner = slot_width * 0.8 / max(1, len(series))
            gap = slot_width * 0.1

            for place, (key, label) in enumerate(series):
                value = values.get((start, key), 0)
                total += value
                height_px = value * scale

                bars.append(
                    Bar(
                        label=axis_label(start),
                        title=f"{label}: {value} ({range_text})",
                        x=slot_x + gap + place * inner,
                        width=max(1.0, inner - 1.0),
                        total=value,
                        segments=[
                            Segment(
                                key=key,
                                label=label,
                                value=value,
                                y=bottom - height_px,
                                height=height_px,
                            )
                        ],
                    )
                )

            continue

        segments: list[Segment] = []
        stack = 0.0
        stack_total = 0

        for key, label in series:
            value = values.get((start, key), 0)

            if not value:
                continue

            height_px = value * scale
            stack += height_px
            stack_total += value

            segments.append(
                Segment(
                    key=key,
                    label=label,
                    value=value,
                    y=bottom - stack,
                    height=height_px,
                )
            )

        total += stack_total
        parts = ", ".join(
            f"{segment.label.lower()} {segment.value}" for segment in segments
        )

        bars.append(
            Bar(
                label=axis_label(start),
                title=f"{range_text}: {stack_total} journeys"
                + (f" ({parts})" if parts else ""),
                x=slot_x + slot_width * 0.15,
                width=max(1.0, slot_width * 0.7),
                total=stack_total,
                segments=segments,
            )
        )

    ticks = [
        Tick(y=bottom - value * scale, label=str(value))
        for value in range(0, top_value + 1, tick_step)
    ]
    every = max(1, math.ceil(len(starts) / 12))
    labels = [
        AxisLabel(x=left + (index + 0.5) * slot_width, text=axis_label(start))
        for index, start in enumerate(starts)
        if index % every == 0
    ]

    return Chart(
        width=width,
        height=height,
        plot_left=left,
        plot_top=top,
        plot_right=right,
        plot_bottom=bottom,
        bars=bars,
        ticks=ticks,
        labels=labels,
        legend=[Legend(key, label) for key, label in series],
        total=total,
    )


def outcome_chart(trends: Trends, period: Period, now: datetime) -> Chart:
    """The journeys per bucket, stacked by outcome, or one bar per group value.

    Without a grouping the stack is finished, abandoned, lost and in
    progress; with one, each value of the dimension gets a bar of its
    own per bucket, coloured from the palette, so two hosts or two
    versions are read against each other.
    """

    starts = slots(period, trends.buckets, trends.bucket, now)
    values: dict[tuple[datetime, str], int] = {}

    if trends.group_by:
        series = [
            (f"group-{index % PALETTE_SIZE}", group.value or "(none)")
            for index, group in enumerate(trends.groups)
        ]

        for (key, _), group in zip(series, trends.groups, strict=True):
            for bucket in group.buckets:
                values[(parse_moment(bucket.start), key)] = bucket.outcomes.journeys

        return chart_of(series, starts, trends.bucket, values, grouped=True)

    for bucket in trends.buckets:
        start = parse_moment(bucket.start)

        for key, _ in OUTCOME_SERIES:
            values[(start, key)] = int(getattr(bucket.outcomes, key))

    return chart_of(list(OUTCOME_SERIES), starts, trends.bucket, values, grouped=False)


def sparkline(trends: Trends, now: datetime, days: int = SPARKLINE_DAYS) -> list[int]:
    """Journeys per day over the last so many days, today last, from daily buckets."""

    by_start = {
        parse_moment(bucket.start): bucket.outcomes.journeys
        for bucket in trends.buckets
    }
    first = bucket_start(now - timedelta(days=days - 1), "day")

    return [by_start.get(first + timedelta(days=offset), 0) for offset in range(days)]


@dataclass
class Totals:
    """Outcomes summed over a chart's buckets, for the card above it."""

    journeys: int
    finished: int
    abandoned: int
    lost: int
    in_progress: int
    completion_rate: float | None


def totals_of(buckets: Iterable[TrendBucket]) -> Totals:
    """Sum the outcomes of some buckets, with the completion rate over the settled."""

    outcomes: list[Outcomes] = [bucket.outcomes for bucket in buckets]
    finished = sum(item.finished for item in outcomes)
    abandoned = sum(item.abandoned for item in outcomes)
    lost = sum(item.lost for item in outcomes)
    settled = finished + abandoned + lost

    return Totals(
        journeys=sum(item.journeys for item in outcomes),
        finished=finished,
        abandoned=abandoned,
        lost=lost,
        in_progress=sum(item.in_progress for item in outcomes),
        completion_rate=round(finished / settled, 4) if settled else None,
    )


# The workshop page's attention card


@dataclass
class Attention:
    """One thing about a workshop worth a look, and the section that shows it."""

    text: str
    anchor: str


def attention(funnel: Funnel, checks: Checks, coverage: Coverage) -> list[Attention]:
    """What stands out: where journeys stop, the hardest check, what nobody ran.

    Derived from the reports the page already shows, not a query of its
    own, so it says nothing the sections beneath do not.
    """

    items: list[Attention] = []
    stops = [step for step in funnel.steps if step.stopped]

    if stops:
        step: FunnelStep = max(stops, key=lambda item: item.stopped)
        share = step.stopped / funnel.journeys if funnel.journeys else 0.0

        items.append(
            Attention(
                f"{step.stopped} of {funnel.journeys} journeys stopped on "
                f"{step.page.path.rsplit('/', 1)[-1]} ({share:.0%}), more than on "
                "any other page.",
                "funnel",
            )
        )

    # A check is worth a mention when someone failed it or needed more
    # than one attempt; a check everyone passed first time is not.
    attempted = [
        check
        for check in checks.checks
        if check.sessions
        and (
            (check.pass_rate is not None and check.pass_rate < 1.0)
            or (check.attempts_to_pass is not None and check.attempts_to_pass.p50 > 1)
        )
    ]

    if attempted:
        hardest: CheckOutcome = min(
            attempted,
            key=lambda item: (
                item.pass_rate if item.pass_rate is not None else 2.0,
                -(item.attempts_to_pass.p50 if item.attempts_to_pass else 0.0),
            ),
        )
        rate = f"{hardest.pass_rate:.0%}" if hardest.pass_rate is not None else "no"
        tries = (
            f", a median of {hardest.attempts_to_pass.p50:g} attempts to pass"
            if hardest.attempts_to_pass
            else ""
        )

        items.append(
            Attention(
                f"{hardest.kind} {hardest.id} has the lowest pass rate: {rate} of "
                f"{hardest.sessions} sessions{tries}.",
                "checks",
            )
        )

    if coverage.with_inventory:
        count = len(coverage.never_run)
        noun = "directive" if count == 1 else "directives"

        items.append(
            Attention(
                f"{count} of {coverage.directives} {noun} nobody ran, across "
                f"{coverage.with_inventory} sessions with an inventory."
                if count
                else f"Every one of {coverage.directives} directives ran at least "
                f"once, across {coverage.with_inventory} sessions with an inventory.",
                "coverage",
            )
        )

    return items


# Downloads

CSV_FIELDS: tuple[str, ...] = (
    "session_id",
    "instance_id",
    "user",
    "name",
    "collection",
    "workshop",
    "version",
    "source",
    "host",
    "frontend",
    "frontend_version",
    "platform",
    "trust",
    "status",
    "started_at",
    "ended_at",
    "last_seen",
    "duration_seconds",
    "current_page",
    "page_position",
    "page_count",
    "pages_done",
    "gates_skipped",
    "coverage",
    "events_received",
    "events_expected",
    "complete",
    "gaps",
    "labels",
    "token_id",
    "resumed_from",
    "resumed_by",
    "restarted_from",
)


def csv_lines(summaries: Iterable[SessionSummary]) -> Iterable[str]:
    """The sessions as CSV, a header then a line each, labels as `key=value` pairs."""

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")

    writer.writeheader()

    yield buffer.getvalue()

    for summary in summaries:
        buffer.seek(0)
        buffer.truncate()

        row: dict[str, Any] = {field: getattr(summary, field) for field in CSV_FIELDS}
        row["gaps"] = " ".join(
            f"{first}-{last}" if first != last else str(first)
            for first, last in summary.gaps
        )
        row["labels"] = " ".join(
            f"{key}={value}" for key, value in sorted(summary.labels.items())
        )
        row["coverage"] = "" if summary.coverage is None else summary.coverage
        row["complete"] = "true" if summary.complete else "false"

        writer.writerow(row)

        yield buffer.getvalue()
