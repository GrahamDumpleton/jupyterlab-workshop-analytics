"""Every question the service answers, as functions over the store.

The REST routes are thin wrappers over these functions, and the MCP
tools will be the same wrappers, so the two can never answer
differently. Each takes a connection, the typed `Filters` and the
service's settings, and returns plain dataclasses, which FastAPI turns
into the response body and the OpenAPI document.

Sessions are narrowed by the indexed columns in SQL and by what only
Python knows afterwards: the label selector (labels are JSON) and the
status, which is derived from timestamps at read time. Data quality is
part of every answer: rates and timings leave out sessions whose
events are known to be missing unless the caller widens the net.
"""

from __future__ import annotations

import base64
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, func, select

from . import __version__
from .config import Settings
from .projection import LIVE_STATUSES, STATUSES, Session, status_of
from .schema import SCHEMA_VERSION, load_schema
from .selectors import SelectorError, Term, matches, parse_selector
from .sql import describe_sql
from .store.tables import events, sessions

SESSION_FIELDS = (
    "source",
    "version",
    "frontend",
    "host",
    "platform",
    "token_id",
    "user",
)

GROUP_FIELDS = ("collection", "version", "frontend", "host", "platform", "user")

BUCKETS = ("day", "week")

DEFAULT_LIMIT = 100

MAX_LIMIT = 1000

IN_CHUNK = 500


class QueryError(ValueError):
    """A request the query layer cannot answer as asked."""


class NotFound(QueryError):
    """Nothing in the store matches what was named."""


class AmbiguousWorkshop(QueryError):
    """A workshop name that more than one collection has sessions under."""

    def __init__(self, name: str, collections: Sequence[str]) -> None:
        self.name = name
        self.collections = list(collections)

        super().__init__(
            f'the workshop "{name}" has sessions under more than one collection; '
            f"add collection= to choose one"
        )


# Filters and selection


@dataclass(frozen=True)
class Filters:
    """The common filters every question takes.

    `collection` is `None` for any collection and the empty string for
    sessions opened outside any collection. `since` and `until` are
    naive UTC and apply to when a session started. `status` is matched
    after the status is derived, so it costs a pass over the rows.
    """

    labels: tuple[Term, ...] = ()
    name: str = ""
    collection: str | None = None
    source: str = ""
    version: str = ""
    frontend: str = ""
    host: str = ""
    platform: str = ""
    token_id: str = ""
    user: str = ""
    since: datetime | None = None
    until: datetime | None = None
    status: tuple[str, ...] = ()
    include_incomplete: bool = False

    def replace(self, **changes: Any) -> Filters:
        """A copy with some fields changed."""

        values = dict(self.__dict__)

        values.update(changes)

        return Filters(**values)


def parse_filters(
    *,
    labels: str = "",
    name: str = "",
    collection: str | None = None,
    source: str = "",
    version: str = "",
    frontend: str = "",
    host: str = "",
    platform: str = "",
    token_id: str = "",
    user: str = "",
    since: str = "",
    until: str = "",
    status: str = "",
    include_incomplete: bool = False,
) -> Filters:
    """The filters from their textual form, as a query string or a tool call.

    A selector that cannot be read, a timestamp that is not ISO 8601 or
    a status that is not one of the seven is a `QueryError`.
    """

    try:
        terms = parse_selector(labels) if labels.strip() else ()
    except SelectorError as error:
        raise QueryError(str(error)) from error

    statuses = tuple(part.strip() for part in status.split(",") if part.strip())
    unknown = [part for part in statuses if part not in STATUSES]

    if unknown:
        raise QueryError(f"unknown status {', '.join(unknown)}")

    def when(value: str, field: str) -> datetime | None:
        if not value.strip():
            return None

        try:
            return parse_moment(value)
        except ValueError as error:
            raise QueryError(f"{field} must be an ISO 8601 timestamp") from error

    return Filters(
        labels=terms,
        name=name.strip(),
        collection=None if collection is None else collection.strip(),
        source=source.strip(),
        version=version.strip(),
        frontend=frontend.strip(),
        host=host.strip(),
        platform=platform.strip(),
        token_id=token_id.strip(),
        user=user.strip(),
        since=when(since, "since"),
        until=when(until, "until"),
        status=statuses,
        include_incomplete=include_incomplete,
    )


@dataclass
class Loaded:
    """A session as loaded for a question: the row and its status now."""

    session: Session
    status: str

    @property
    def missing_head(self) -> bool:
        """Whether the first events never arrived."""

        gaps = self.session.gaps or []

        return bool(gaps) and gaps[0][0] == 1

    @property
    def gapped(self) -> bool:
        """Whether events are missing somewhere after the first."""

        return any(gap[0] != 1 for gap in self.session.gaps or [])

    @property
    def excluded(self) -> bool:
        """Whether a rate or timing leaves this session out by default."""

        return self.missing_head or self.gapped

    @property
    def ended_at(self) -> datetime | None:
        """When the session ended by an explicit event, if it did."""

        return self.session.finished_at or self.session.abandoned_at

    @property
    def duration_seconds(self) -> float:
        """Seconds from the session's start to its end or last event."""

        session = self.session
        start = session.started_at or session.last_seen
        end = self.ended_at or session.last_seen

        if start is None or end is None:
            return 0.0

        return max(0.0, (end - start).total_seconds())


def load_sessions(
    connection: Connection, filters: Filters, now: datetime, settings: Settings
) -> list[Loaded]:
    """The sessions matching the filters, oldest first, with their status."""

    statement = select(sessions).order_by(sessions.c.started_at, sessions.c.session_id)

    if filters.name:
        statement = statement.where(sessions.c.name == filters.name)

    if filters.collection is not None:
        statement = statement.where(sessions.c.collection == filters.collection)

    for name in SESSION_FIELDS:
        value = getattr(filters, name)

        if value:
            statement = statement.where(getattr(sessions.c, name) == value)

    if filters.since is not None:
        statement = statement.where(sessions.c.started_at >= filters.since)

    if filters.until is not None:
        statement = statement.where(sessions.c.started_at < filters.until)

    loaded: list[Loaded] = []

    for row in connection.execute(statement):
        session = Session.from_row(row)

        if not matches(session.labels or {}, filters.labels):
            continue

        status = status_of(session, now, settings)

        if filters.status and status not in filters.status:
            continue

        loaded.append(Loaded(session, status))

    return loaded


def resolve_workshop(connection: Connection, name: str, collection: str | None) -> str:
    """The collection a workshop name means, or a `NotFound` or ambiguity.

    A name alone succeeds when exactly one collection has sessions under
    it, or when only sessions opened outside any collection do. The
    empty string selects the uncollected sessions explicitly.
    """

    if collection is not None:
        return collection

    seen = list(
        connection.execute(
            select(sessions.c.collection)
            .where(sessions.c.name == name)
            .distinct()
            .order_by(sessions.c.collection)
        ).scalars()
    )

    if not seen:
        raise NotFound(f'no sessions of a workshop named "{name}"')

    if len(seen) > 1:
        raise AmbiguousWorkshop(name, seen)

    return str(seen[0])


# Data quality


@dataclass
class DataQuality:
    """How much of the data a question was answered from, and why.

    `considered` is what the filters matched; `included` is what the
    answer used. A session with a gap after its first event, or with
    its first events missing, is left out unless `include_incomplete`
    was asked for. `open_ended` sessions stopped without a finish or
    abandon and are counted, since silence is an outcome on its own.
    """

    considered: int
    included: int
    excluded_gaps: int
    excluded_missing_head: int
    open_ended: int
    in_progress: int
    complete: int
    share_complete: float
    include_incomplete: bool


def include(
    loaded: Sequence[Loaded], include_incomplete: bool
) -> tuple[list[Loaded], DataQuality]:
    """Split the sessions into the ones an answer uses, with the quality note."""

    kept: list[Loaded] = []
    gaps = 0
    heads = 0

    for item in loaded:
        if item.excluded and not include_incomplete:
            if item.missing_head:
                heads += 1
            else:
                gaps += 1

            continue

        kept.append(item)

    complete = sum(1 for item in loaded if item.session.complete)
    quality = DataQuality(
        considered=len(loaded),
        included=len(kept),
        excluded_gaps=gaps,
        excluded_missing_head=heads,
        open_ended=sum(1 for item in kept if item.status == "lost"),
        in_progress=sum(1 for item in kept if item.status in LIVE_STATUSES),
        complete=complete,
        share_complete=round(complete / len(loaded), 4) if loaded else 0.0,
        include_incomplete=include_incomplete,
    )

    return kept, quality


# Journeys: chains of sessions linked by resumes


@dataclass
class Journey:
    """One learner's passage through a workshop: a chain of sessions."""

    segments: list[Loaded]

    @property
    def first(self) -> Loaded:
        return self.segments[0]

    @property
    def last(self) -> Loaded:
        return self.segments[-1]

    @property
    def outcome(self) -> str:
        """finished, abandoned, lost or in_progress, from the last segment."""

        status = self.last.status

        if status in {"finished", "abandoned", "lost"}:
            return status

        return "in_progress"

    @property
    def duration_seconds(self) -> float:
        """Time spent, the segments summed, so a night between two is not."""

        return sum(segment.duration_seconds for segment in self.segments)

    @property
    def pages_done(self) -> int:
        """The distinct pages left across the chain."""

        left: set[str] = set()

        for segment in self.segments:
            left.update(segment.session.pages_left or [])

        return len(left)

    @property
    def page_count(self) -> int:
        """The longest page list any segment started with."""

        return max(len(segment.session.pages or []) for segment in self.segments)

    @property
    def gates_skipped(self) -> int:
        """How many times a session in the chain moved past an unmet gate."""

        return sum(int(segment.session.gates_skipped or 0) for segment in self.segments)


def journeys(loaded: Sequence[Loaded]) -> list[Journey]:
    """Chain the sessions by their resume links, oldest first.

    A session resuming one that is not among the given sessions starts
    a chain of its own, so a filter that cuts a chain still counts what
    it matched.
    """

    by_id = {item.session.session_id: item for item in loaded}
    successor: dict[str, str] = {}

    for item in loaded:
        previous = item.session.resumed_from

        if previous in by_id and previous != item.session.session_id:
            successor[previous] = item.session.session_id

    heads = [
        item
        for item in loaded
        if item.session.resumed_from not in by_id
        or item.session.resumed_from == item.session.session_id
    ]
    chains: list[Journey] = []

    for head in heads:
        segments = [head]
        seen = {head.session.session_id}

        while segments[-1].session.session_id in successor:
            following = successor[segments[-1].session.session_id]

            if following in seen:
                break

            seen.add(following)
            segments.append(by_id[following])

        chains.append(Journey(segments))

    return chains


# Percentiles


@dataclass
class Percentiles:
    """A distribution in the few numbers a report needs."""

    count: int
    min: float
    p50: float
    p90: float
    max: float
    mean: float


def percentiles(values: Iterable[float]) -> Percentiles | None:
    """The percentiles of some values, or None for none."""

    ordered = sorted(values)

    if not ordered:
        return None

    def rank(share: float) -> float:
        index = max(0, min(len(ordered) - 1, round(share * len(ordered) + 0.5) - 1))

        return ordered[index]

    return Percentiles(
        count=len(ordered),
        min=round(ordered[0], 3),
        p50=round(rank(0.5), 3),
        p90=round(rank(0.9), 3),
        max=round(ordered[-1], 3),
        mean=round(statistics.fmean(ordered), 3),
    )


# Summaries


@dataclass
class WorkshopRef:
    """The identity of a workshop in the data: its name and collection."""

    name: str
    collection: str


@dataclass
class Outcomes:
    """What became of the journeys through a workshop."""

    sessions: int
    journeys: int
    starts: int
    restarts: int
    resumes: int
    finished: int
    finished_skipping_gates: int
    abandoned: int
    lost: int
    in_progress: int
    completion_rate: float | None
    duration_seconds: Percentiles | None
    pages_done: Percentiles | None


def outcomes(loaded: Sequence[Loaded]) -> Outcomes:
    """The outcomes of the sessions, chained into journeys first."""

    chains = journeys(loaded)
    counts = Counter(chain.outcome for chain in chains)
    settled = counts["finished"] + counts["abandoned"] + counts["lost"]
    finished = [chain for chain in chains if chain.outcome == "finished"]

    return Outcomes(
        sessions=len(loaded),
        journeys=len(chains),
        starts=sum(1 for item in loaded if not item.session.resumed_from),
        restarts=sum(1 for item in loaded if item.session.restarted_from),
        resumes=sum(1 for item in loaded if item.session.resumed_from),
        finished=counts["finished"],
        finished_skipping_gates=sum(1 for chain in finished if chain.gates_skipped),
        abandoned=counts["abandoned"],
        lost=counts["lost"],
        in_progress=counts["in_progress"],
        completion_rate=round(counts["finished"] / settled, 4) if settled else None,
        duration_seconds=percentiles(chain.duration_seconds for chain in finished),
        pages_done=percentiles(float(chain.pages_done) for chain in chains),
    )


@dataclass
class Group:
    """One value of a grouping dimension and its outcomes."""

    value: str
    outcomes: Outcomes


def group_value(item: Loaded, dimension: str) -> str:
    """The value a session has for a grouping dimension: a field or a label."""

    if dimension in GROUP_FIELDS:
        return str(getattr(item.session, dimension) or "")

    return str((item.session.labels or {}).get(dimension, ""))


def grouped(loaded: Sequence[Loaded], dimension: str) -> list[Group]:
    """The outcomes by each value of a dimension, most sessions first.

    A journey is grouped by its first session's value, so a chain whose
    segments differ in the dimension is not split.
    """

    buckets: dict[str, list[Loaded]] = defaultdict(list)

    for chain in journeys(loaded):
        buckets[group_value(chain.first, dimension)].extend(chain.segments)

    groups = [Group(value, outcomes(items)) for value, items in buckets.items()]

    groups.sort(key=lambda group: (-group.outcomes.sessions, group.value))

    return groups


@dataclass
class WorkshopSummary:
    """A workshop's outcomes, in total and by a dimension."""

    workshop: WorkshopRef
    data_quality: DataQuality
    total: Outcomes
    group_by: str
    groups: list[Group]


def workshop_summary(
    connection: Connection,
    filters: Filters,
    now: datetime,
    settings: Settings,
    group_by: str = "version",
) -> WorkshopSummary:
    """Outcomes for one workshop, by version unless another dimension is asked."""

    collection = resolve_workshop(connection, filters.name, filters.collection)
    narrowed = filters.replace(collection=collection)
    loaded = load_sessions(connection, narrowed, now, settings)
    kept, quality = include(loaded, filters.include_incomplete)

    return WorkshopSummary(
        workshop=WorkshopRef(filters.name, collection),
        data_quality=quality,
        total=outcomes(kept),
        group_by=group_by,
        groups=grouped(kept, group_by),
    )


# Discovery


@dataclass
class WorkshopListing:
    """One workshop as seen in the data."""

    name: str
    collection: str
    sources: list[str]
    versions: list[str]
    sessions: int
    journeys: int
    finished: int
    completion_rate: float | None
    share_complete: float
    first_seen: str
    last_seen: str
    identity: bool


def list_workshops(
    connection: Connection, filters: Filters, now: datetime, settings: Settings
) -> list[WorkshopListing]:
    """Every name and collection pair with sessions, with what was seen."""

    loaded = load_sessions(connection, filters, now, settings)
    by_workshop: dict[tuple[str, str], list[Loaded]] = defaultdict(list)

    for item in loaded:
        by_workshop[(item.session.name, item.session.collection)].append(item)

    listings: list[WorkshopListing] = []

    for (name, collection), items in sorted(by_workshop.items()):
        kept, quality = include(items, filters.include_incomplete)
        result = outcomes(kept)
        starts = [item.session.started_at for item in items if item.session.started_at]
        seen = [item.session.last_seen for item in items if item.session.last_seen]

        listings.append(
            WorkshopListing(
                name=name,
                collection=collection,
                sources=sorted({item.session.source for item in items}),
                versions=sorted({item.session.version for item in items}),
                sessions=len(items),
                journeys=result.journeys,
                finished=result.finished,
                completion_rate=result.completion_rate,
                share_complete=quality.share_complete,
                first_seen=iso(min(starts)) if starts else "",
                last_seen=iso(max(seen)) if seen else "",
                identity=any(item.session.user for item in items),
            )
        )

    return listings


# Funnel


@dataclass
class PageRef:
    """A page of the workshop, from the page list a session started with."""

    id: str
    path: str
    title: str


@dataclass
class FunnelStep:
    """How many journeys reached a page, left it, and stopped there."""

    page: PageRef
    position: int
    entered: int
    left: int
    stopped: int


@dataclass
class Funnel:
    """Journeys reaching each page in order and where they stop."""

    workshop: WorkshopRef
    data_quality: DataQuality
    journeys: int
    with_pages: int
    finished: int
    steps: list[FunnelStep]
    unlisted_pages: list[str]


def page_order(loaded: Sequence[Loaded]) -> list[PageRef]:
    """The page list most sessions ran with, with any other pages appended."""

    lists = Counter(
        tuple((p["id"], p["path"], p["title"]) for p in item.session.pages or [])
        for item in loaded
        if item.session.pages
    )

    if not lists:
        return []

    common = max(lists, key=lambda entry: (lists[entry], len(entry)))
    order = [PageRef(*entry) for entry in common]
    known = {page.id for page in order}

    for item in loaded:
        for page in item.session.pages or []:
            if page["id"] not in known:
                known.add(page["id"])
                order.append(PageRef(page["id"], page["path"], page["title"]))

    return order


def funnel(
    connection: Connection, filters: Filters, now: datetime, settings: Settings
) -> Funnel:
    """Where journeys through a workshop get to, page by page."""

    collection = resolve_workshop(connection, filters.name, filters.collection)
    loaded = load_sessions(
        connection, filters.replace(collection=collection), now, settings
    )
    kept, quality = include(loaded, filters.include_incomplete)
    chains = journeys(kept)
    order = page_order(kept)
    positions = {page.id: index for index, page in enumerate(order)}
    entered: Counter[str] = Counter()
    left: Counter[str] = Counter()
    stopped: Counter[str] = Counter()
    unlisted: set[str] = set()
    with_pages = 0
    finished = 0

    for chain in chains:
        pages_entered: list[str] = []
        pages_left: set[str] = set()

        for segment in chain.segments:
            pages_entered.extend(segment.session.pages_entered or [])
            pages_left.update(segment.session.pages_left or [])

        seen = set(pages_entered)

        if any(segment.session.pages for segment in chain.segments):
            with_pages += 1

        for page in seen:
            if page in positions:
                entered[page] += 1
            else:
                unlisted.add(page)

        for page in pages_left:
            if page in positions:
                left[page] += 1

        # Where a journey stopped is its furthest page in the list, unless
        # it finished, in which case it stopped nowhere.
        furthest = max(
            (positions[page] for page in seen if page in positions), default=-1
        )

        if chain.outcome == "finished":
            finished += 1
        elif furthest >= 0:
            stopped[order[furthest].id] += 1

    steps = [
        FunnelStep(
            page=page,
            position=index + 1,
            entered=entered[page.id],
            left=left[page.id],
            stopped=stopped[page.id],
        )
        for index, page in enumerate(order)
    ]

    return Funnel(
        workshop=WorkshopRef(filters.name, collection),
        data_quality=quality,
        journeys=len(chains),
        with_pages=with_pages,
        finished=finished,
        steps=steps,
        unlisted_pages=sorted(unlisted),
    )


# Events of a set of sessions


def load_events(
    connection: Connection, session_ids: Sequence[str], kinds: Sequence[str] = ()
) -> list[Any]:
    """The event rows of some sessions, optionally of some kinds, in order."""

    rows: list[Any] = []
    ids = list(session_ids)

    for start in range(0, len(ids), IN_CHUNK):
        statement = select(events).where(
            events.c.session_id.in_(ids[start : start + IN_CHUNK])
        )

        if kinds:
            statement = statement.where(events.c.kind.in_(list(kinds)))

        rows.extend(connection.execute(statement))

    rows.sort(key=lambda row: (row.session_id, row.seq, row.id))

    return rows


# Page timing


@dataclass
class PageTiming:
    """Time spent on one page, from the leaves recorded for it."""

    page: PageRef
    position: int
    sessions: int
    entries: int
    entries_per_session: float
    active_seconds: Percentiles | None
    total_active_seconds: float


@dataclass
class PageTimings:
    """Time on each page of a workshop."""

    workshop: WorkshopRef
    data_quality: DataQuality
    sessions: int
    pages: list[PageTiming]


def page_timings_for(loaded: Sequence[Loaded], rows: Sequence[Any]) -> list[PageTiming]:
    """The per-page timings of some sessions from their page-leave rows."""

    order = page_order(loaded)
    known = {page.id for page in order}
    active: dict[str, list[float]] = defaultdict(list)
    visitors: dict[str, set[str]] = defaultdict(set)

    for row in rows:
        page_id = str(row.page)

        if page_id not in known:
            known.add(page_id)
            order.append(PageRef(page_id, page_id, ""))

        active[page_id].append(float(row.payload.get("active_ms", 0)) / 1000.0)
        visitors[page_id].add(str(row.session_id))

    timings: list[PageTiming] = []

    for index, page in enumerate(order):
        values = active[page.id]
        count = len(visitors[page.id])

        timings.append(
            PageTiming(
                page=page,
                position=index + 1,
                sessions=count,
                entries=len(values),
                entries_per_session=round(len(values) / count, 2) if count else 0.0,
                active_seconds=percentiles(values),
                total_active_seconds=round(sum(values), 3),
            )
        )

    return timings


def page_timings(
    connection: Connection, filters: Filters, now: datetime, settings: Settings
) -> PageTimings:
    """Time on each page of a workshop, from `page-leave` events."""

    collection = resolve_workshop(connection, filters.name, filters.collection)
    loaded = load_sessions(
        connection, filters.replace(collection=collection), now, settings
    )
    kept, quality = include(loaded, filters.include_incomplete)
    rows = load_events(
        connection, [item.session.session_id for item in kept], ["page-leave"]
    )

    return PageTimings(
        workshop=WorkshopRef(filters.name, collection),
        data_quality=quality,
        sessions=len(kept),
        pages=page_timings_for(kept, rows),
    )


# Actions


@dataclass
class ActionUsage:
    """How one action was run and how it went."""

    id: str
    type: str
    page: str
    runs: int
    sessions: int
    by_trigger: dict[str, int]
    ok: int
    error: int
    skipped: int
    downgraded: int


@dataclass
class ActionUsages:
    """Every action of a workshop, with whether the clickable ones are used."""

    workshop: WorkshopRef
    data_quality: DataQuality
    sessions: int
    runs: int
    clicked: int
    sessions_clicking: int
    actions: list[ActionUsage]


def action_usages_for(rows: Sequence[Any]) -> list[ActionUsage]:
    """The per-action usage from some action-executed rows."""

    by_id: dict[str, list[Any]] = defaultdict(list)

    for row in rows:
        by_id[str(row.action_id)].append(row)

    usages: list[ActionUsage] = []

    for action_id, group in by_id.items():
        statuses = Counter(str(row.status) for row in group)
        page = next((str(row.page) for row in group if row.page), "")

        usages.append(
            ActionUsage(
                id=action_id,
                type=str(group[0].payload.get("type", "")),
                page=page or action_page(action_id),
                runs=len(group),
                sessions=len({str(row.session_id) for row in group}),
                by_trigger=dict(
                    Counter(str(row.payload.get("trigger", "")) for row in group)
                ),
                ok=statuses["ok"],
                error=statuses["error"],
                skipped=statuses["skipped"],
                downgraded=sum(1 for row in group if row.payload.get("downgraded")),
            )
        )

    usages.sort(key=lambda usage: (-usage.runs, usage.id))

    return usages


def action_page(action_id: str) -> str:
    """The page an action id belongs to, by the extension's naming.

    An action without an explicit id is named after its page and its
    position, `01-welcome-1`; the page is what is left after the
    trailing number.
    """

    stem, dash, tail = action_id.rpartition("-")

    return stem if dash and tail.isdigit() else ""


def action_usages(
    connection: Connection, filters: Filters, now: datetime, settings: Settings
) -> ActionUsages:
    """Per action: runs by trigger and outcome, and whether clicks happen."""

    collection = resolve_workshop(connection, filters.name, filters.collection)
    loaded = load_sessions(
        connection, filters.replace(collection=collection), now, settings
    )
    kept, quality = include(loaded, filters.include_incomplete)
    rows = load_events(
        connection, [item.session.session_id for item in kept], ["action-executed"]
    )
    clicks = [row for row in rows if row.payload.get("trigger") == "click"]

    return ActionUsages(
        workshop=WorkshopRef(filters.name, collection),
        data_quality=quality,
        sessions=len(kept),
        runs=len(rows),
        clicked=len(clicks),
        sessions_clicking=len({str(row.session_id) for row in clicks}),
        actions=action_usages_for(rows),
    )


# Checks and quizzes


@dataclass
class CheckOutcome:
    """A verify or a quiz: who attempted it, who passed, how many tries."""

    id: str
    kind: str
    runs: int
    sessions: int
    passed: int
    pass_rate: float | None
    attempts_to_pass: Percentiles | None
    errors: int
    skipped: int
    by_trigger: dict[str, int]


@dataclass
class HintUsage:
    """How often a hint was opened."""

    id: str
    opened: int
    sessions: int


@dataclass
class Checks:
    """The checks and quizzes of a workshop and the hints opened beside them."""

    workshop: WorkshopRef
    data_quality: DataQuality
    sessions: int
    checks: list[CheckOutcome]
    hints: list[HintUsage]
    gates_skipped: int


def check_outcomes_for(rows: Sequence[Any]) -> list[CheckOutcome]:
    """The per-check outcomes from verify-result and quiz-answered rows."""

    by_key: dict[tuple[str, str], list[Any]] = defaultdict(list)

    for row in rows:
        by_key[(str(row.kind), str(row.action_id))].append(row)

    results: list[CheckOutcome] = []

    for (kind, check_id), group in by_key.items():
        attempted = {str(row.session_id) for row in group}
        first_pass: dict[str, int] = {}
        statuses: Counter[str] = Counter()

        for row in sorted(group, key=lambda row: (row.session_id, row.seq)):
            passed = (
                bool(row.payload.get("correct"))
                if kind == "quiz-answered"
                else str(row.status) == "ok"
            )

            statuses[str(row.status) or ("correct" if passed else "incorrect")] += 1

            if passed:
                first_pass.setdefault(
                    str(row.session_id), int(row.payload.get("attempt", 1))
                )

        results.append(
            CheckOutcome(
                id=check_id,
                kind="quiz" if kind == "quiz-answered" else "verify",
                runs=len(group),
                sessions=len(attempted),
                passed=len(first_pass),
                pass_rate=round(len(first_pass) / len(attempted), 4)
                if attempted
                else None,
                attempts_to_pass=percentiles(float(n) for n in first_pass.values()),
                errors=statuses["error"] + statuses["incorrect"],
                skipped=statuses["skipped"],
                by_trigger=dict(
                    Counter(
                        str(row.payload.get("trigger", ""))
                        for row in group
                        if row.payload.get("trigger")
                    )
                ),
            )
        )

    results.sort(key=lambda result: (result.kind, -result.runs, result.id))

    return results


def checks(
    connection: Connection, filters: Filters, now: datetime, settings: Settings
) -> Checks:
    """Pass rates, attempts before passing, hints opened and gates skipped."""

    collection = resolve_workshop(connection, filters.name, filters.collection)
    loaded = load_sessions(
        connection, filters.replace(collection=collection), now, settings
    )
    kept, quality = include(loaded, filters.include_incomplete)
    rows = load_events(
        connection,
        [item.session.session_id for item in kept],
        ["verify-result", "quiz-answered", "hint-opened", "gate-skipped"],
    )
    hints: dict[str, set[str]] = defaultdict(set)
    opened: Counter[str] = Counter()

    for row in rows:
        if row.kind == "hint-opened":
            opened[str(row.action_id)] += 1
            hints[str(row.action_id)].add(str(row.session_id))

    return Checks(
        workshop=WorkshopRef(filters.name, collection),
        data_quality=quality,
        sessions=len(kept),
        checks=check_outcomes_for(
            [row for row in rows if row.kind in {"verify-result", "quiz-answered"}]
        ),
        hints=[
            HintUsage(id=hint_id, opened=opened[hint_id], sessions=len(hints[hint_id]))
            for hint_id in sorted(hints)
        ],
        gates_skipped=sum(1 for row in rows if row.kind == "gate-skipped"),
    )


# Trends


@dataclass
class TrendBucket:
    """The outcomes of the journeys that started in one period."""

    start: str
    end: str
    outcomes: Outcomes


@dataclass
class TrendGroup:
    """One value of a dimension across the periods."""

    value: str
    buckets: list[TrendBucket]


@dataclass
class Trends:
    """Outcomes over time, by day or week, and by a dimension if asked."""

    workshop: WorkshopRef
    data_quality: DataQuality
    bucket: str
    group_by: str
    buckets: list[TrendBucket]
    groups: list[TrendGroup]


def bucket_start(moment: datetime, bucket: str) -> datetime:
    """The start of the day or ISO week a moment falls in."""

    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)

    if bucket == "week":
        return day - timedelta(days=day.weekday())

    return day


def bucket_length(bucket: str) -> timedelta:
    """How long a bucket is."""

    return timedelta(days=7 if bucket == "week" else 1)


def bucketed(loaded: Sequence[Loaded], bucket: str) -> list[TrendBucket]:
    """The outcomes per period, journeys placed by their first start.

    Every period between the first and the last is listed, empty ones
    included, so a chart has no holes.
    """

    chains = journeys(loaded)
    by_start: dict[datetime, list[Loaded]] = defaultdict(list)

    for chain in chains:
        started = chain.first.session.started_at

        if started is not None:
            by_start[bucket_start(started, bucket)].extend(chain.segments)

    if not by_start:
        return []

    step = bucket_length(bucket)
    moment = min(by_start)
    last = max(by_start)
    buckets: list[TrendBucket] = []

    while moment <= last:
        buckets.append(
            TrendBucket(
                start=iso(moment),
                end=iso(moment + step),
                outcomes=outcomes(by_start.get(moment, [])),
            )
        )

        moment += step

    return buckets


def trends(
    connection: Connection,
    filters: Filters,
    now: datetime,
    settings: Settings,
    bucket: str = "day",
    group_by: str = "",
) -> Trends:
    """Outcomes bucketed by day or week, and by a dimension when asked."""

    if bucket not in BUCKETS:
        raise QueryError(f"the bucket must be one of {', '.join(BUCKETS)}")

    collection = resolve_workshop(connection, filters.name, filters.collection)
    loaded = load_sessions(
        connection, filters.replace(collection=collection), now, settings
    )
    kept, quality = include(loaded, filters.include_incomplete)
    groups: list[TrendGroup] = []

    if group_by:
        by_value: dict[str, list[Loaded]] = defaultdict(list)

        for chain in journeys(kept):
            by_value[group_value(chain.first, group_by)].extend(chain.segments)

        groups = [
            TrendGroup(value=value, buckets=bucketed(items, bucket))
            for value, items in sorted(by_value.items())
        ]

    return Trends(
        workshop=WorkshopRef(filters.name, collection),
        data_quality=quality,
        bucket=bucket,
        group_by=group_by,
        buckets=bucketed(kept, bucket),
        groups=groups,
    )


# Sessions


@dataclass
class SessionSummary:
    """One session as the lists and the detail show it."""

    session_id: str
    instance_id: str
    name: str
    collection: str
    workshop: str
    source: str
    version: str
    frontend: str
    frontend_version: str
    host: str
    platform: str
    trust: str
    user: str
    token_id: str
    labels: dict[str, str]
    status: str
    started_at: str
    last_seen: str
    ended_at: str
    duration_seconds: float
    current_page: str
    page_position: int
    page_count: int
    pages_done: int
    gates_skipped: int
    events_received: int
    events_expected: int
    gaps: list[list[int]]
    complete: bool
    resumed_from: str
    resumed_by: str
    restarted_from: str


def session_summary(item: Loaded) -> SessionSummary:
    """The summary of a loaded session."""

    session = item.session
    ids = [str(page.get("id", "")) for page in session.pages or []]
    position = ids.index(session.current_page) + 1 if session.current_page in ids else 0

    return SessionSummary(
        session_id=session.session_id,
        instance_id=session.instance_id,
        name=session.name,
        collection=session.collection,
        workshop=session.workshop,
        source=session.source,
        version=session.version,
        frontend=session.frontend,
        frontend_version=session.frontend_version,
        host=session.host,
        platform=session.platform,
        trust=session.trust,
        user=session.user,
        token_id=session.token_id,
        labels=dict(session.labels or {}),
        status=item.status,
        started_at=iso(session.started_at),
        last_seen=iso(session.last_seen),
        ended_at=iso(item.ended_at),
        duration_seconds=round(item.duration_seconds, 3),
        current_page=session.current_page,
        page_position=position,
        page_count=len(ids),
        pages_done=int(session.pages_done or 0),
        gates_skipped=int(session.gates_skipped or 0),
        events_received=int(session.events_received or 0),
        events_expected=int(session.events_expected or 0),
        gaps=[list(gap) for gap in session.gaps or []],
        complete=bool(session.complete),
        resumed_from=session.resumed_from,
        resumed_by=session.resumed_by,
        restarted_from=session.restarted_from,
    )


@dataclass
class SessionPage:
    """The sessions matching the filters, newest first, one page of them."""

    sessions: list[SessionSummary]
    total: int
    next_cursor: str


def encode_cursor(started_at: datetime | None, session_id: str) -> str:
    """A cursor for the session after the given one in the list order."""

    moment = started_at.isoformat() if started_at is not None else ""
    text = f"{moment}|{session_id}"

    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """The position a cursor names, or a `QueryError`."""

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        text = base64.urlsafe_b64decode(padded.encode()).decode()
        moment, _, session_id = text.partition("|")

        return datetime.fromisoformat(moment), session_id
    except (ValueError, UnicodeDecodeError) as error:
        raise QueryError("the cursor is not one this service issued") from error


def list_sessions(
    connection: Connection,
    filters: Filters,
    now: datetime,
    settings: Settings,
    limit: int = DEFAULT_LIMIT,
    cursor: str = "",
) -> SessionPage:
    """Sessions newest first, cursor paged; every session, whatever its quality."""

    loaded = load_sessions(connection, filters, now, settings)
    loaded.reverse()
    limit = max(1, min(limit, MAX_LIMIT))
    total = len(loaded)

    if cursor:
        after_moment, after_id = decode_cursor(cursor)
        loaded = [
            item
            for item in loaded
            if (item.session.started_at or now, item.session.session_id)
            < (after_moment, after_id)
        ]

    page = loaded[:limit]
    following = ""

    if len(loaded) > limit:
        last = page[-1].session

        following = encode_cursor(last.started_at, last.session_id)

    return SessionPage(
        sessions=[session_summary(item) for item in page],
        total=total,
        next_cursor=following,
    )


@dataclass
class PageVisit:
    """One page of a session's workshop and what the session did on it."""

    page: PageRef
    position: int
    entered: bool
    left: bool
    active_seconds: float
    entries: int


@dataclass
class TimelineEntry:
    """One event of a session, reduced to what a reader needs."""

    seq: int
    ts: str
    kind: str
    page: str
    id: str
    status: str
    detail: dict[str, Any]


@dataclass
class SessionDetail:
    """One session: its summary, its pages and its ordered timeline."""

    session: SessionSummary
    pages: list[PageVisit]
    timeline: list[TimelineEntry]
    gaps: list[list[int]]
    chain: list[str]


DETAIL_FIELDS = (
    "type",
    "trigger",
    "attempt",
    "correct",
    "downgraded",
    "hidden",
    "active_ms",
    "requirements",
    "fields",
    "tools",
    "kernel",
    "pages",
    "restarted_from",
    "resumed_from",
)


def timeline_entry(row: Any) -> TimelineEntry:
    """The timeline entry for an event row."""

    payload = dict(row.payload)
    status = str(row.status)

    if row.kind == "quiz-answered":
        status = "correct" if payload.get("correct") else "incorrect"

    detail = {key: payload[key] for key in DETAIL_FIELDS if key in payload}

    if row.kind in {"workshop-start", "workshop-resume"}:
        detail["pages"] = len(payload.get("pages") or [])

    if row.kind == "checkpoint-restored":
        detail["checkpoint"] = str(payload.get("name", ""))

    return TimelineEntry(
        seq=int(row.seq),
        ts=iso(row.ts),
        kind=str(row.kind),
        page=str(row.page),
        id=str(row.action_id),
        status=status,
        detail=detail,
    )


def load_one(
    connection: Connection, session_id: str, now: datetime, settings: Settings
) -> Loaded:
    """One session by id, or `NotFound`."""

    row = connection.execute(
        select(sessions).where(sessions.c.session_id == session_id)
    ).first()

    if row is None:
        raise NotFound(f'no session "{session_id}"')

    session = Session.from_row(row)

    return Loaded(session, status_of(session, now, settings))


def chain_of(connection: Connection, session: Session) -> list[str]:
    """The session ids of the chain this session is part of, oldest first."""

    ids = [session.session_id]
    seen = set(ids)
    previous = session.resumed_from

    while previous and previous not in seen:
        seen.add(previous)
        ids.insert(0, previous)

        row = connection.execute(
            select(sessions.c.resumed_from).where(sessions.c.session_id == previous)
        ).first()

        previous = str(row[0]) if row is not None else ""

    following = session.resumed_by

    while following and following not in seen:
        seen.add(following)
        ids.append(following)

        row = connection.execute(
            select(sessions.c.resumed_by).where(sessions.c.session_id == following)
        ).first()

        following = str(row[0]) if row is not None else ""

    return ids


def session_detail(
    connection: Connection, session_id: str, now: datetime, settings: Settings
) -> SessionDetail:
    """One session with its pages and its timeline."""

    item = load_one(connection, session_id, now, settings)
    session = item.session
    rows = load_events(connection, [session_id])
    active: defaultdict[str, float] = defaultdict(float)
    entries: Counter[str] = Counter()

    for row in rows:
        if row.kind == "page-leave":
            active[str(row.page)] += float(row.payload.get("active_ms", 0)) / 1000.0
        elif row.kind == "page-enter":
            entries[str(row.page)] += 1

    order = page_order([item])
    known = {page.id for page in order}

    for page in session.pages_entered or []:
        if page not in known:
            known.add(page)
            order.append(PageRef(page, page, ""))

    visits = [
        PageVisit(
            page=page,
            position=index + 1,
            entered=page.id in (session.pages_entered or []),
            left=page.id in (session.pages_left or []),
            active_seconds=round(active[page.id], 3),
            entries=entries[page.id],
        )
        for index, page in enumerate(order)
    ]

    return SessionDetail(
        session=session_summary(item),
        pages=visits,
        timeline=[timeline_entry(row) for row in rows],
        gaps=[list(gap) for gap in session.gaps or []],
        chain=chain_of(connection, session),
    )


def session_events(
    connection: Connection, session_id: str, now: datetime, settings: Settings
) -> list[dict[str, Any]]:
    """The raw events of one session in order, labels as the service merged them."""

    load_one(connection, session_id, now, settings)

    return [stored_event(row) for row in load_events(connection, [session_id])]


def stored_event(row: Any) -> dict[str, Any]:
    """An event as stored: the payload with the merged labels and the receipt."""

    event = dict(row.payload)

    event["labels"] = dict(row.labels or {})
    event["received_at"] = iso(row.received_at)
    event["token_id"] = str(row.token_id)

    return event


# Raw events


@dataclass(frozen=True)
class EventFilters:
    """The filters of the raw events query, on the extracted columns."""

    kind: str = ""
    session_id: str = ""
    instance_id: str = ""
    page: str = ""
    id: str = ""
    status: str = ""


@dataclass
class EventPage:
    """One page of raw events, oldest first."""

    events: list[dict[str, Any]]
    next_cursor: str


def query_events(
    connection: Connection,
    filters: Filters,
    event_filters: EventFilters,
    limit: int = DEFAULT_LIMIT,
    cursor: str = "",
) -> EventPage:
    """Raw events by any filter, cursor paged in the order they were stored.

    `since` and `until` apply to the event's own timestamp here. The
    label selector is applied after the page is read from the store, so
    a page can come back short of the limit before the cursor ends.
    """

    limit = max(1, min(limit, MAX_LIMIT))
    statement = select(events).order_by(events.c.id).limit(limit + 1)

    if cursor:
        try:
            statement = statement.where(events.c.id > int(cursor))
        except ValueError as error:
            raise QueryError("the cursor is not one this service issued") from error

    if filters.name:
        statement = statement.where(events.c.name == filters.name)

    if filters.collection is not None:
        statement = statement.where(events.c.collection == filters.collection)

    for name in SESSION_FIELDS:
        value = getattr(filters, name)

        if value:
            statement = statement.where(getattr(events.c, name) == value)

    if filters.since is not None:
        statement = statement.where(events.c.ts >= filters.since)

    if filters.until is not None:
        statement = statement.where(events.c.ts < filters.until)

    columns = {
        "kind": events.c.kind,
        "session_id": events.c.session_id,
        "instance_id": events.c.instance_id,
        "page": events.c.page,
        "id": events.c.action_id,
        "status": events.c.status,
    }

    for name, column in columns.items():
        value = getattr(event_filters, name)

        if value:
            statement = statement.where(column == value)

    rows = list(connection.execute(statement))
    following = str(rows[limit - 1].id) if len(rows) > limit else ""
    page = rows[:limit]

    return EventPage(
        events=[
            stored_event(row)
            for row in page
            if matches(dict(row.labels or {}), filters.labels)
        ],
        next_cursor=following,
    )


# Instances and collections


@dataclass
class Instance:
    """One running frontend and the sessions it opened, in order."""

    instance_id: str
    frontend: str
    host: str
    platform: str
    user: str
    labels: dict[str, str]
    first_seen: str
    last_seen: str
    workshops: list[str]
    sessions: list[SessionSummary]


def instance(
    connection: Connection, instance_id: str, now: datetime, settings: Settings
) -> Instance:
    """The sessions of one running JupyterLab, oldest first."""

    rows = list(
        connection.execute(
            select(sessions)
            .where(sessions.c.instance_id == instance_id)
            .order_by(sessions.c.started_at, sessions.c.session_id)
        )
    )

    if not rows:
        raise NotFound(f'no sessions of an instance "{instance_id}"')

    loaded = [
        Loaded(Session.from_row(row), status_of(row, now, settings)) for row in rows
    ]
    first = loaded[0].session
    seen = [item.session.last_seen for item in loaded if item.session.last_seen]

    return Instance(
        instance_id=instance_id,
        frontend=first.frontend,
        host=first.host,
        platform=first.platform,
        user=next((item.session.user for item in loaded if item.session.user), ""),
        labels=dict(first.labels or {}),
        first_seen=iso(first.started_at),
        last_seen=iso(max(seen)) if seen else "",
        workshops=list(dict.fromkeys(item.session.name for item in loaded)),
        sessions=[session_summary(item) for item in loaded],
    )


@dataclass
class CollectionWorkshop:
    """One workshop of a collection, in the order the sessions suggest."""

    name: str
    position: int
    sessions: int
    instances: int
    finished: int
    completion_rate: float | None


@dataclass
class CollectionListing:
    """One collection seen in the data."""

    collection: str
    workshops: list[CollectionWorkshop]
    sessions: int
    instances: int
    first_seen: str
    last_seen: str


def collection_workshops(loaded: Sequence[Loaded]) -> list[CollectionWorkshop]:
    """The workshops of a collection's sessions in the order instances took them.

    The order is by the average position a workshop had among the
    workshops each instance opened, so the order most learners followed
    comes out even when nobody took every workshop.
    """

    by_instance: dict[str, list[str]] = defaultdict(list)

    for item in loaded:
        names = by_instance[item.session.instance_id]

        if item.session.name not in names:
            names.append(item.session.name)

    positions: dict[str, list[float]] = defaultdict(list)

    for names in by_instance.values():
        for index, name in enumerate(names):
            positions[name].append(float(index))

    order = sorted(
        positions,
        key=lambda name: (statistics.fmean(positions[name]), name),
    )
    by_name: dict[str, list[Loaded]] = defaultdict(list)

    for item in loaded:
        by_name[item.session.name].append(item)

    listings: list[CollectionWorkshop] = []

    for index, name in enumerate(order):
        items = by_name[name]
        result = outcomes(items)

        listings.append(
            CollectionWorkshop(
                name=name,
                position=index + 1,
                sessions=len(items),
                instances=len({item.session.instance_id for item in items}),
                finished=result.finished,
                completion_rate=result.completion_rate,
            )
        )

    return listings


def list_collections(
    connection: Connection, filters: Filters, now: datetime, settings: Settings
) -> list[CollectionListing]:
    """Every collection with sessions, its workshops in the order taken."""

    loaded = load_sessions(connection, filters, now, settings)
    by_collection: dict[str, list[Loaded]] = defaultdict(list)

    for item in loaded:
        if item.session.collection:
            by_collection[item.session.collection].append(item)

    listings: list[CollectionListing] = []

    for collection, items in sorted(by_collection.items()):
        starts = [item.session.started_at for item in items if item.session.started_at]
        seen = [item.session.last_seen for item in items if item.session.last_seen]

        listings.append(
            CollectionListing(
                collection=collection,
                workshops=collection_workshops(items),
                sessions=len(items),
                instances=len({item.session.instance_id for item in items}),
                first_seen=iso(min(starts)) if starts else "",
                last_seen=iso(max(seen)) if seen else "",
            )
        )

    return listings


@dataclass
class CollectionStep:
    """How many instances reached a workshop of the collection and stopped there."""

    name: str
    position: int
    instances: int
    finished: int
    stopped: int


@dataclass
class CollectionProgress:
    """The funnel across a collection's workshops, by instance."""

    collection: str
    data_quality: DataQuality
    instances: int
    in_order: int
    completed_all: int
    steps: list[CollectionStep]


def collection_progress(
    connection: Connection, filters: Filters, now: datetime, settings: Settings
) -> CollectionProgress:
    """How instances moved through a collection's workshops.

    An instance took the workshops in order when the positions of the
    workshops it opened never decrease; it stopped at the last workshop
    it opened unless it finished every workshop of the collection.
    """

    if not filters.collection:
        raise QueryError("a collection is required")

    loaded = load_sessions(connection, filters, now, settings)

    if not loaded:
        raise NotFound(f'no sessions of a collection "{filters.collection}"')

    kept, quality = include(loaded, filters.include_incomplete)
    order = collection_workshops(kept)
    position = {step.name: step.position for step in order}
    by_instance: dict[str, list[Loaded]] = defaultdict(list)

    for item in kept:
        by_instance[item.session.instance_id].append(item)

    reached: Counter[str] = Counter()
    finished: Counter[str] = Counter()
    stopped: Counter[str] = Counter()
    in_order = 0
    completed_all = 0

    for items in by_instance.values():
        taken = list(dict.fromkeys(item.session.name for item in items))
        done = {
            chain.first.session.name
            for chain in journeys(items)
            if chain.outcome == "finished"
        }
        steps = [position[name] for name in taken]

        if steps == sorted(steps):
            in_order += 1

        for name in taken:
            reached[name] += 1

        for name in done:
            finished[name] += 1

        if len(done) == len(order):
            completed_all += 1
        else:
            stopped[max(taken, key=lambda name: position[name])] += 1

    return CollectionProgress(
        collection=str(filters.collection),
        data_quality=quality,
        instances=len(by_instance),
        in_order=in_order,
        completed_all=completed_all,
        steps=[
            CollectionStep(
                name=step.name,
                position=step.position,
                instances=reached[step.name],
                finished=finished[step.name],
                stopped=stopped[step.name],
            )
            for step in order
        ],
    )


# Learners


@dataclass
class Learner:
    """One identified learner's sessions of a workshop."""

    user: str
    sessions: int
    journeys: int
    finished: bool
    best_pages_done: int
    page_count: int
    best_progress: float
    first_seen: str
    last_seen: str
    last_status: str


@dataclass
class Learners:
    """The sessions of a workshop grouped by who did them."""

    workshop: WorkshopRef
    learners: list[Learner]
    anonymous_sessions: int


def learners(
    connection: Connection, filters: Filters, now: datetime, settings: Settings
) -> Learners:
    """Sessions by user, with attempts, best progress and completion.

    Every session counts here, whatever its quality: the question is who
    has not finished, and a session with a gap still says where its
    learner got to.
    """

    collection = resolve_workshop(connection, filters.name, filters.collection)
    loaded = load_sessions(
        connection, filters.replace(collection=collection), now, settings
    )
    by_user: dict[str, list[Loaded]] = defaultdict(list)
    anonymous = 0

    for item in loaded:
        if item.session.user:
            by_user[item.session.user].append(item)
        else:
            anonymous += 1

    people: list[Learner] = []

    for user, items in sorted(by_user.items()):
        chains = journeys(items)
        best = max(chains, key=lambda chain: chain.pages_done)
        count = best.page_count
        done = best.pages_done
        starts = [item.session.started_at for item in items if item.session.started_at]
        seen = [item.session.last_seen for item in items if item.session.last_seen]

        people.append(
            Learner(
                user=user,
                sessions=len(items),
                journeys=len(chains),
                finished=any(chain.outcome == "finished" for chain in chains),
                best_pages_done=done,
                page_count=count,
                best_progress=round(done / count, 4) if count else 0.0,
                first_seen=iso(min(starts)) if starts else "",
                last_seen=iso(max(seen)) if seen else "",
                last_status=items[-1].status,
            )
        )

    return Learners(
        workshop=WorkshopRef(filters.name, collection),
        learners=people,
        anonymous_sessions=anonymous,
    )


# Describe


@dataclass
class LabelKey:
    """A label key seen in the data and where it came from."""

    key: str
    values: list[str]
    sessions: int
    tokens: list[str]
    bound: bool


@dataclass
class Description:
    """What the store holds and how the answers are computed.

    What a client reads first: the event contract, the derived session
    fields and their rules, the metric definitions, the filters, and
    what the data actually contains, so a question is asked of fields
    that exist with values that occur.
    """

    service: dict[str, Any]
    events: dict[str, Any]
    sessions: dict[str, Any]
    metrics: dict[str, str]
    data_quality: dict[str, str]
    filters: dict[str, str]
    data: dict[str, Any]
    labels: list[LabelKey]
    collections: list[str]
    identity: dict[str, Any]
    sql: dict[str, Any]


STATUS_MEANINGS = {
    "active": "an event or a visible heartbeat within ACTIVE_ALLOWANCE seconds",
    "away": "the latest thing heard was a hidden heartbeat, within AWAY_ALLOWANCE",
    "silent": "nothing within the active or away allowance, but less than "
    "SILENT_LIMIT seconds of silence",
    "lost": "silence past SILENT_LIMIT with no finish or abandon: a closed tab, "
    "a culled server, or a learner who walked away",
    "resumed": "a later session named this one in resumed_from, so the learner "
    "carried on in it",
    "finished": "Finish was pressed on the last page (workshop-finish)",
    "abandoned": "the workshop was closed or replaced before finishing "
    "(workshop-abandon)",
}

METRICS = {
    "journey": "a chain of sessions linked by resumed_from, counted once; a "
    "session that resumes one outside the filters starts a chain of its own",
    "outcome": "of a journey, from its last session: finished, abandoned, lost "
    "(silence past SILENT_LIMIT) or in_progress (active, away or silent)",
    "completion_rate": "finished journeys over settled journeys, where settled "
    "is finished plus abandoned plus lost; journeys still in progress are "
    "left out of both",
    "finished_skipping_gates": "of the finished journeys, those in which a "
    "session moved past unmet requirements under soft gating at least once: "
    "finishes the workshop's checks did not confirm, counted as finished by "
    "the completion rate",
    "duration_seconds": "of a finished journey, the time from each session's "
    "start to its end or last event, summed over the chain, so time between a "
    "stop and a resume is not counted; percentiles over the finished journeys",
    "pages_done": "the pages a session left, which is how many it worked "
    "through; a journey's is the distinct pages left across its sessions",
    "funnel.entered": "journeys that entered the page at least once",
    "funnel.left": "journeys that left the page at least once",
    "funnel.stopped": "journeys not finished whose furthest page in the list is "
    "this one",
    "pages.active_seconds": "from page-leave's active_ms, one value per leave, "
    "so a page entered twice contributes twice",
    "actions.by_trigger": "runs by what ran the action: click, role, auto, "
    "cascade or trigger; clicked counts the click runs",
    "checks.pass_rate": "sessions that passed the check at least once over "
    "sessions that ran it; a quiz passes when answered correctly",
    "checks.attempts_to_pass": "the attempt number of each session's first "
    "pass, percentiles over the sessions that passed",
    "trends": "journeys bucketed by the day or ISO week their first session "
    "started in, every bucket between the first and the last listed",
    "collections.order": "workshops ordered by the average position they had "
    "among the workshops each instance opened",
    "collections.in_order": "instances whose workshops were opened in "
    "non-decreasing collection order",
}

DATA_QUALITY = {
    "considered": "sessions the filters matched",
    "included": "sessions the answer used",
    "excluded_gaps": "left out for a missing range of events after the first, "
    "which is a delivery failure; a resend closes it",
    "excluded_missing_head": "left out because the first events never arrived, "
    "so there is no page list and no start",
    "open_ended": "included sessions that stopped without a finish or abandon "
    "(status lost); silence is an outcome, usually a learner walking away",
    "in_progress": "included sessions still active, away or silent",
    "complete": "sessions whose terminal event arrived with nothing missing",
    "share_complete": "complete over considered",
    "include_incomplete": "true when the caller asked for the excluded sessions "
    "to be counted; the session and event queries always return everything",
}

FILTERS = {
    "labels": "a label selector: course=intro-git,term!=2025,cohort in (a,b), "
    "host notin (x); every term must match",
    "name": "the workshop's manifest name",
    "collection": "the collection the workshop was subscribed from; empty "
    "selects sessions opened outside any collection; omitted on a name-keyed "
    "route means the one collection the name has, or an ambiguity error",
    "source": "where the copy came from: git:<url>@<ref>/<subdir>, "
    "archive:<url> or local:<path>",
    "version": "the workshop's manifest version",
    "frontend": "jupyterlab or jupyterlite",
    "host": "local, binder, jupyterhub, codespaces or static",
    "platform": "linux, macos, windows or emscripten",
    "token_id": "the jti of the token the session's first batch arrived under",
    "user": "the learner's identity where the deployment supplies one",
    "since": "ISO 8601 UTC; sessions started at or after it (events: their ts)",
    "until": "ISO 8601 UTC; sessions started before it (events: their ts)",
    "status": "one or more of the session statuses, comma separated",
    "include_incomplete": "true to count sessions with missing events in rates "
    "and timings",
    "group_by": "collection, version, frontend, host, platform, user, or a label key",
    "limit": f"page size, at most {MAX_LIMIT}",
    "cursor": "the next_cursor of the previous page",
}


def describe(connection: Connection, now: datetime, settings: Settings) -> Description:
    """What the store holds and how every answer is computed."""

    schema = load_schema()
    definitions = schema.get("definitions", {})
    kinds = {
        kind: {
            "description": spec.get("description", ""),
            "fields": {
                name: field_spec.get("description", "")
                for name, field_spec in spec.get("properties", {}).items()
            },
        }
        for kind, spec in definitions.get("kinds", {}).items()
    }
    base_fields = {
        name: spec.get("description", "")
        for name, spec in schema.get("properties", {}).items()
    }

    counts = connection.execute(
        select(
            func.count(events.c.id),
            func.min(events.c.ts),
            func.max(events.c.ts),
        )
    ).one()
    rows = list(
        connection.execute(
            select(
                sessions.c.session_id,
                sessions.c.name,
                sessions.c.collection,
                sessions.c.host,
                sessions.c.frontend,
                sessions.c.frontend_version,
                sessions.c.platform,
                sessions.c.token_id,
                sessions.c.user,
                sessions.c.labels,
            )
        )
    )

    return Description(
        service={
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
            "dialect": connection.dialect.name,
            "now": iso(now),
        },
        events={"base_fields": base_fields, "kinds": kinds},
        sessions={
            "statuses": {
                status: STATUS_MEANINGS[status]
                .replace("ACTIVE_ALLOWANCE", str(int(settings.active_allowance)))
                .replace("AWAY_ALLOWANCE", str(int(settings.away_allowance)))
                .replace("SILENT_LIMIT", str(int(settings.silent_limit)))
                for status in STATUSES
            },
            "thresholds": {
                "active_allowance": settings.active_allowance,
                "away_allowance": settings.away_allowance,
                "silent_limit": settings.silent_limit,
            },
            "completeness": {
                "events_expected": "the seq of the terminal event when a finish "
                "or abandon arrived, otherwise the highest seq seen",
                "gaps": "the ranges of seq below events_expected that never "
                "arrived, as [first, last] pairs",
                "complete": "a terminal event arrived and nothing is missing",
            },
            "fields": list(SessionSummary.__dataclass_fields__),
        },
        metrics=dict(METRICS),
        data_quality=dict(DATA_QUALITY),
        filters=dict(FILTERS),
        data={
            "events": int(counts[0] or 0),
            "sessions": len(rows),
            "workshops": len({(row.name, row.collection) for row in rows}),
            "first_event": iso(counts[1]) if counts[1] else "",
            "last_event": iso(counts[2]) if counts[2] else "",
            "frontends": sorted({str(row.frontend) for row in rows if row.frontend}),
            "frontend_versions": sorted(
                {str(row.frontend_version) for row in rows if row.frontend_version}
            ),
            "hosts": sorted({str(row.host) for row in rows if row.host}),
            "platforms": sorted({str(row.platform) for row in rows if row.platform}),
            "tokens": sorted({str(row.token_id) for row in rows if row.token_id}),
        },
        labels=label_keys(rows),
        collections=sorted({str(row.collection) for row in rows if row.collection}),
        identity=identity_availability(rows),
        sql=describe_sql(settings, connection.dialect.name),
    )


def label_keys(rows: Sequence[Any]) -> list[LabelKey]:
    """The label keys seen, their values, and whether a token binds them.

    The store keeps the merged labels, not which came from the token,
    so a key is reported as bound when every session that arrived under
    a token carries it with a single value for that token, which is
    what a token's label looks like and a client's rarely does.
    """

    values: dict[str, set[str]] = defaultdict(set)
    carriers: Counter[str] = Counter()
    by_token: dict[str, list[dict[str, str]]] = defaultdict(list)

    for row in rows:
        labels = dict(row.labels or {})

        for key, value in labels.items():
            values[key].add(str(value))
            carriers[key] += 1

        if row.token_id:
            by_token[str(row.token_id)].append(labels)

    keys: list[LabelKey] = []

    for key in sorted(values):
        binders = [
            token
            for token, sets in by_token.items()
            if all(key in labels for labels in sets)
            and len({labels[key] for labels in sets}) == 1
        ]

        keys.append(
            LabelKey(
                key=key,
                values=sorted(values[key]),
                sessions=carriers[key],
                tokens=sorted(binders),
                bound=bool(binders),
            )
        )

    return keys


def identity_availability(rows: Sequence[Any]) -> dict[str, Any]:
    """Whether sessions carry a user, per token and per host."""

    by_token: dict[str, list[bool]] = defaultdict(list)
    by_host: dict[str, list[bool]] = defaultdict(list)

    for row in rows:
        identified = bool(row.user)

        if row.token_id:
            by_token[str(row.token_id)].append(identified)

        by_host[str(row.host)].append(identified)

    def share(flags: list[bool]) -> float:
        return round(sum(flags) / len(flags), 4) if flags else 0.0

    return {
        "meaning": "the share of sessions carrying a user; a deployment with "
        "identity answers who has not finished, one without answers how many",
        "by_token": {token: share(flags) for token, flags in sorted(by_token.items())},
        "by_host": {host: share(flags) for host, flags in sorted(by_host.items())},
    }


# Time helpers


def iso(moment: datetime | None) -> str:
    """A naive UTC moment as ISO 8601 with a Z, or empty for None."""

    if moment is None:
        return ""

    return moment.replace(microsecond=0).isoformat() + "Z"


def parse_moment(text: str) -> datetime:
    """A naive UTC datetime from ISO 8601, a date alone included."""

    value = text.strip()

    if not value:
        raise ValueError("an empty timestamp")

    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)

    return moment


__all__ = [
    "AmbiguousWorkshop",
    "DataQuality",
    "EventFilters",
    "Filters",
    "NotFound",
    "QueryError",
    "action_usages",
    "checks",
    "collection_progress",
    "describe",
    "funnel",
    "parse_filters",
    "instance",
    "learners",
    "list_collections",
    "list_sessions",
    "list_workshops",
    "page_timings",
    "parse_moment",
    "query_events",
    "session_detail",
    "session_events",
    "trends",
    "workshop_summary",
]
