"""The dashboard's pages: the overview, the live view, the history, a
workshop's reports, a session's drill-down, the downloads, the login
and the logout.

Every page is rendered on the server from the `queries` functions the
API answers with, under the dashboard cookie, so a number on a page is
the number the API gives and the cookie never needs to reach `/api`.
Filters and periods live in the URL, with the same names the API's
query parameters have, so a view can be linked to.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from .. import queries, views
from ..projection import STATUSES
from ..queries import (
    AmbiguousWorkshop,
    Checks,
    CollectionProgress,
    Coverage,
    Filters,
    Funnel,
    NotFound,
    PageTimings,
    QueryError,
    SessionDetail,
    SessionPage,
    Trends,
    WorkshopListing,
    WorkshopSummary,
    iso,
)
from ..store.writes import utcnow
from ..tokens import TokenError
from ..views import Attention, Chart, Period, Totals
from .auth import (
    clear_session_cookie,
    issue_session,
    read_session,
    set_session_cookie,
    verified,
)

router = APIRouter()

TABS: tuple[tuple[str, str, str], ...] = (
    ("/live", "Now", "live"),
    ("/sessions", "History", "history"),
    ("/", "Workshops", "overview"),
)

DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("host", "Host"),
    ("frontend", "Frontend"),
    ("version", "Version"),
    ("collection", "Collection"),
    ("platform", "Platform"),
)

HISTORY_LIMIT = 50

EVENT_CHUNK = 200


def render(
    request: Request, template: str, status_code: int = 200, **context: Any
) -> HTMLResponse:
    """Render a dashboard template."""

    templates = request.app.state.templates
    assets = request.app.state.assets_version
    response: HTMLResponse = templates.TemplateResponse(
        request,
        template,
        {"request": request, "assets": assets, "tabs": TABS, **context},
        status_code=status_code,
    )

    return response


def problem(
    request: Request,
    viewer: str,
    heading: str,
    message: str,
    status_code: int,
    choices: list[tuple[str, str]] | None = None,
) -> HTMLResponse:
    """A page saying what could not be shown, with the choices when there are some."""

    return render(
        request,
        "problem.html",
        status_code=status_code,
        viewer=viewer,
        tab="",
        labels="",
        heading=heading,
        message=message,
        choices=choices or [],
    )


def page_params(request: Request) -> dict[str, str]:
    """The query string as one value per name, statuses joined by commas.

    The history form sends one `status` per checkbox; a link sends
    them comma separated, as the API takes them. Either way they end
    up as one string here.
    """

    params: dict[str, str] = {}

    for key in request.query_params.keys():
        values = [value.strip() for value in request.query_params.getlist(key)]

        if key == "status":
            params[key] = ",".join(
                part for value in values for part in value.split(",") if part
            )
        else:
            params[key] = values[-1]

    return params


def filters_of(
    params: dict[str, str], period: Period, collection: str | None
) -> Filters:
    """The typed filters a page's parameters and period select."""

    return queries.parse_filters(
        labels=params.get("labels", ""),
        name=params.get("name", ""),
        collection=collection,
        source=params.get("source", ""),
        version=params.get("version", ""),
        frontend=params.get("frontend", ""),
        host=params.get("host", ""),
        platform=params.get("platform", ""),
        token_id=params.get("token_id", ""),
        user=params.get("user", ""),
        since=iso(period.since) if period.since else "",
        until=iso(period.until) if period.until else "",
        status=params.get("status", ""),
        include_incomplete=params.get("include_incomplete", "") == "true",
    )


def history_collection(params: dict[str, str]) -> str | None:
    """The collection the history form chose.

    A form cannot leave a parameter out, so on the history page an
    empty value means any collection and `none` means the sessions
    opened outside any, which the API expresses as an empty value.
    """

    collection = params.get("collection", "")

    if not collection:
        return None

    return "" if collection == "none" else collection


# The overview


@dataclass
class OverviewReport:
    """What the overview shows: the total trend, the workshops, the collections."""

    trends: Trends
    totals: Totals
    chart: Chart
    workshops: list[WorkshopListing]
    sparklines: dict[tuple[str, str], list[int]]
    collections: list[CollectionProgress]


def overview_report(
    connection: Any,
    filters: Filters,
    period: Period,
    now: datetime,
    settings: Any,
    bucket: str,
) -> OverviewReport:
    """Ask the overview's questions on one connection.

    The sparklines are the last thirty days whatever the period, one
    `trends` call per workshop, since a trend is per workshop and the
    listing is what says which there are.
    """

    trends = queries.trends(connection, filters, now, settings, bucket)
    workshops = queries.list_workshops(connection, filters, now, settings)
    recent = filters.replace(
        since=queries.bucket_start(
            now - timedelta(days=views.SPARKLINE_DAYS - 1), "day"
        ),
        until=None,
    )
    sparklines: dict[tuple[str, str], list[int]] = {}

    for listing in workshops:
        each = queries.trends(
            connection,
            recent.replace(name=listing.name, collection=listing.collection),
            now,
            settings,
            "day",
        )

        sparklines[(listing.name, listing.collection)] = views.sparkline(each, now)

    collections = [
        queries.collection_progress(
            connection, filters.replace(collection=listing.collection), now, settings
        )
        for listing in queries.list_collections(connection, filters, now, settings)
    ]

    return OverviewReport(
        trends=trends,
        totals=views.totals_of(trends.buckets),
        chart=views.outcome_chart(trends, period, now),
        workshops=workshops,
        sparklines=sparklines,
        collections=collections,
    )


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def overview(request: Request) -> Response:
    """The overview, or the login page when there is no session.

    A one-time `?token=` on the URL is exchanged for the cookie and
    redirected away, so the token leaves the address bar and the
    history entry.
    """

    token = request.query_params.get("token", "").strip()

    if token:
        try:
            claims = verified(request, token, "dashboard")
        except TokenError as error:
            return render(request, "login.html", error=str(error), status_code=401)

        params = dict(request.query_params)
        params.pop("token", None)
        target = request.url.path

        if params:
            target += "?" + "&".join(f"{k}={v}" for k, v in params.items())

        response: Response = RedirectResponse(target, status_code=303)
        value = issue_session(
            request.app.state.signing_key, claims, request.app.state.settings
        )

        set_session_cookie(response, request, value)

        return response

    session = read_session(request)

    if session is None:
        return render(request, "login.html", error="")

    settings = request.app.state.settings
    now = utcnow()
    params = page_params(request)

    try:
        period = views.period_of(params, now)
        bucket = views.bucket_for(period, params, now)
        filters = filters_of(params, period, None)
    except (QueryError, ValueError) as error:
        return problem(request, session.name, "Workshops", str(error), 400)

    def question() -> OverviewReport:
        with request.app.state.engine.connect() as connection:
            return overview_report(connection, filters, period, now, settings, bucket)

    report: OverviewReport = await run_in_threadpool(question)

    return render(
        request,
        "overview.html",
        viewer=session.name,
        tab="overview",
        labels=params.get("labels", ""),
        params=params,
        period=period,
        trends=report.trends,
        totals=report.totals,
        chart=report.chart,
        workshops=report.workshops,
        sparklines=report.sparklines,
        spark_days=views.SPARKLINE_DAYS,
        collections=report.collections,
    )


# The live view


@router.get("/live", response_class=HTMLResponse, include_in_schema=False)
async def live(request: Request) -> Response:
    """The live view: the sessions being done right now, updating as they go."""

    session = read_session(request)

    if session is None:
        return render(request, "login.html", error="", status_code=401)

    return render(
        request,
        "live.html",
        viewer=session.name,
        tab="live",
        labels=request.query_params.get("labels", ""),
        linger=request.app.state.settings.linger,
    )


# The history and its downloads


@dataclass
class Choices:
    """The values the history form offers, the ones that occur in the data."""

    names: list[str]
    collections: list[str]
    hosts: list[str]
    frontends: list[str]
    statuses: tuple[str, ...]


@dataclass
class HistoryReport:
    """One page of the history and the choices for its form."""

    page: SessionPage
    choices: Choices


def history_selection(params: dict[str, str], now: datetime) -> tuple[Period, Filters]:
    """The period and filters the history parameters select, or a `QueryError`."""

    period = views.period_of(params, now, default="all")

    return period, filters_of(params, period, history_collection(params))


@router.get("/sessions", response_class=HTMLResponse, include_in_schema=False)
async def history(request: Request) -> Response:
    """The history: every session matching the filters, newest first, paged."""

    session = read_session(request)

    if session is None:
        return render(request, "login.html", error="", status_code=401)

    settings = request.app.state.settings
    now = utcnow()
    params = page_params(request)
    error = ""

    # A selection that cannot be read still shows the form, with the
    # fault above an empty table, so it can be corrected in place.
    try:
        period, filters = history_selection(params, now)
    except (QueryError, ValueError) as fault:
        error = str(fault)
        period = views.period_of({}, now, default="all")
        filters = Filters()

    try:
        limit = max(1, min(int(params.get("limit", HISTORY_LIMIT)), queries.MAX_LIMIT))
    except ValueError:
        limit = HISTORY_LIMIT

    cursor = params.get("cursor", "")

    def question() -> HistoryReport:
        with request.app.state.engine.connect() as connection:
            description = queries.describe(connection, now, settings)
            workshops = queries.list_workshops(connection, Filters(), now, settings)
            choices = Choices(
                names=sorted({listing.name for listing in workshops}),
                collections=list(description.collections),
                hosts=list(description.data["hosts"]),
                frontends=list(description.data["frontends"]),
                statuses=STATUSES,
            )

            if error:
                return HistoryReport(SessionPage([], 0, ""), choices)

            page = queries.list_sessions(
                connection, filters, now, settings, limit, cursor
            )

            return HistoryReport(page, choices)

    try:
        report: HistoryReport = await run_in_threadpool(question)
    except QueryError as fault:
        error = str(fault)
        report = HistoryReport(
            SessionPage([], 0, ""), Choices([], [], [], [], STATUSES)
        )

    return render(
        request,
        "history.html",
        viewer=session.name,
        tab="history",
        labels=params.get("labels", ""),
        params=params,
        period=period,
        error=error,
        page=report.page,
        rows=report.page.sessions,
        choices=report.choices,
        selected_statuses=set(filters.status),
    )


@router.get("/downloads/sessions.csv", include_in_schema=False)
async def sessions_csv(request: Request) -> Response:
    """The history's selection as CSV, one line per session, every session in it."""

    session = read_session(request)

    if session is None:
        return render(request, "login.html", error="", status_code=401)

    settings = request.app.state.settings
    now = utcnow()
    params = page_params(request)

    try:
        _, filters = history_selection(params, now)
    except (QueryError, ValueError) as error:
        return Response(str(error), status_code=400, media_type="text/plain")

    def lines() -> Iterator[str]:
        with request.app.state.engine.connect() as connection:
            loaded = queries.load_sessions(connection, filters, now, settings)

            loaded.reverse()

            yield from views.csv_lines(queries.session_summary(item) for item in loaded)

    return StreamingResponse(
        lines(),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": 'attachment; filename="sessions.csv"'},
    )


@router.get("/downloads/events.ndjson", include_in_schema=False)
async def events_ndjson(request: Request) -> Response:
    """The events of the history's selection as JSON lines, session by session.

    Newest session first, each session's events in order, so the file
    reads as the history does and `import` reads it back.
    """

    session = read_session(request)

    if session is None:
        return render(request, "login.html", error="", status_code=401)

    settings = request.app.state.settings
    now = utcnow()
    params = page_params(request)

    try:
        _, filters = history_selection(params, now)
    except (QueryError, ValueError) as error:
        return Response(str(error), status_code=400, media_type="text/plain")

    def lines() -> Iterator[str]:
        with request.app.state.engine.connect() as connection:
            loaded = queries.load_sessions(connection, filters, now, settings)
            ids = [item.session.session_id for item in reversed(loaded)]

            # A chunk of sessions at a time keeps the memory bounded by
            # the chunk rather than by the selection.
            for start in range(0, len(ids), EVENT_CHUNK):
                rows = queries.load_events(connection, ids[start : start + EVENT_CHUNK])

                for row in rows:
                    record = queries.stored_event(row)

                    yield json.dumps(record, separators=(",", ":")) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"content-disposition": 'attachment; filename="events.ndjson"'},
    )


# A workshop's reports


@dataclass
class WorkshopReport:
    """Everything the workshop page shows, asked on one connection."""

    summary: WorkshopSummary
    trends: Trends
    funnel: Funnel
    timings: PageTimings
    coverage: Coverage
    checks: Checks
    label_keys: list[str]


def workshop_report(
    connection: Any,
    filters: Filters,
    now: datetime,
    settings: Any,
    bucket: str,
    group_by: str,
) -> WorkshopReport:
    """Ask every report of one workshop; the first resolves the name."""

    summary = queries.workshop_summary(
        connection, filters, now, settings, group_by or "version"
    )
    narrowed = filters.replace(collection=summary.workshop.collection)

    return WorkshopReport(
        summary=summary,
        trends=queries.trends(connection, narrowed, now, settings, bucket, group_by),
        funnel=queries.funnel(connection, narrowed, now, settings),
        timings=queries.page_timings(connection, narrowed, now, settings),
        coverage=queries.coverage(connection, narrowed, now, settings),
        checks=queries.checks(connection, narrowed, now, settings),
        label_keys=[
            label.key for label in queries.describe(connection, now, settings).labels
        ],
    )


@router.get("/workshops/{name}", response_class=HTMLResponse, include_in_schema=False)
async def workshop_page(request: Request, name: str) -> Response:
    """One workshop's reports: outcomes, trend, funnel, timings, coverage, checks.

    `collection` on the URL says which of the name's collections; a
    name that has sessions under several and no collection answers a
    chooser, as the API answers 409.
    """

    session = read_session(request)

    if session is None:
        return render(request, "login.html", error="", status_code=401)

    settings = request.app.state.settings
    now = utcnow()
    params = page_params(request)
    collection = request.query_params.get("collection")
    group_by = params.get("group_by", "")

    try:
        period = views.period_of(params, now)
        bucket = views.bucket_for(period, params, now)
        filters = filters_of(params, period, collection).replace(name=name)
    except (QueryError, ValueError) as error:
        return problem(request, session.name, name, str(error), 400)

    def question() -> WorkshopReport:
        with request.app.state.engine.connect() as connection:
            return workshop_report(connection, filters, now, settings, bucket, group_by)

    try:
        report: WorkshopReport = await run_in_threadpool(question)
    except AmbiguousWorkshop as error:
        choices = [
            (
                f"/workshops/{name}" + views.query_string(params, collection=option),
                option or "outside any collection",
            )
            for option in error.collections
        ]

        return problem(request, session.name, name, str(error), 409, choices)
    except NotFound as error:
        return problem(request, session.name, name, str(error), 404)
    except QueryError as error:
        return problem(request, session.name, name, str(error), 400)

    dimensions = list(DIMENSIONS) + [(key, f"Label {key}") for key in report.label_keys]
    labels_of = dict(dimensions)
    attention: list[Attention] = views.attention(
        report.funnel, report.checks, report.coverage
    )

    return render(
        request,
        "workshop.html",
        viewer=session.name,
        tab="overview",
        labels=params.get("labels", ""),
        params=params,
        period=period,
        name=name,
        collection=report.summary.workshop.collection,
        summary=report.summary,
        trends=report.trends,
        chart=views.outcome_chart(report.trends, period, now),
        group_by=group_by,
        group_label=labels_of.get(group_by or "version", group_by),
        dimensions=dimensions,
        attention=attention,
        funnel=report.funnel,
        timings=report.timings,
        coverage=report.coverage,
        checks=report.checks,
    )


# A session


@router.get(
    "/sessions/{session_id}", response_class=HTMLResponse, include_in_schema=False
)
async def session_page(request: Request, session_id: str) -> Response:
    """One session's drill-down: its summary, its pages and its timeline.

    Rendered on the server from the same query the API answers, so the
    dashboard cookie never needs to reach the query API.
    """

    session = read_session(request)

    if session is None:
        return render(request, "login.html", error="", status_code=401)

    settings = request.app.state.settings

    def question() -> SessionDetail:
        with request.app.state.engine.connect() as connection:
            return queries.session_detail(connection, session_id, utcnow(), settings)

    try:
        detail: SessionDetail | None = await run_in_threadpool(question)
    except NotFound:
        detail = None

    return render(
        request,
        "session.html",
        viewer=session.name,
        tab="history",
        labels="",
        session_id=session_id,
        detail=detail,
        status_code=200 if detail is not None else 404,
    )


# Template filters


def duration_text(seconds: float) -> str:
    """Seconds as the short form the pages use: 45s, 12m, 1h 05m."""

    whole = int(seconds)

    if whole < 60:
        return f"{whole}s"

    if whole < 3600:
        return f"{whole // 60}m"

    return f"{whole // 3600}h {(whole % 3600) // 60:02d}m"


def detail_text(detail: dict[str, Any]) -> str:
    """A timeline entry's extra fields as `key=value` pairs."""

    return " ".join(f"{key}={value}" for key, value in detail.items())


def percent_text(share: float | None) -> str:
    """A share as a whole percentage, or nothing for none."""

    if share is None:
        return ""

    return f"{share * 100:.0f}%"


# Signing in and out


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request) -> Response:
    """The one-field login form."""

    return render(request, "login.html", error="")


@router.post("/login", include_in_schema=False)
async def login(request: Request, token: str = Form("")) -> Response:
    """Exchange a pasted dashboard token for the session cookie."""

    try:
        claims = verified(request, token.strip(), "dashboard")
    except TokenError as error:
        return render(request, "login.html", error=str(error), status_code=401)

    response: Response = RedirectResponse("/", status_code=303)
    value = issue_session(
        request.app.state.signing_key, claims, request.app.state.settings
    )

    set_session_cookie(response, request, value)

    return response


@router.post("/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    """Clear the session cookie."""

    response: Response = RedirectResponse("/login", status_code=303)

    clear_session_cookie(response, request)

    return response
