"""The query API: every question under `/api`, answered by `queries`.

The routes are thin: parse the filters, open a connection, call the
query function, and turn its dataclass into the response. FastAPI
derives the OpenAPI document at `/docs` from the same dataclasses, so
the document and the answers cannot drift apart. Every route needs a
token with the `api` scope.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import Connection

from .. import queries
from ..queries import (
    ActionUsages,
    AmbiguousWorkshop,
    Checks,
    CollectionListing,
    CollectionProgress,
    Coverage,
    Description,
    EventFilters,
    EventPage,
    Filters,
    Funnel,
    Instance,
    Learners,
    NotFound,
    PageTimings,
    QueryError,
    SessionDetail,
    SessionPage,
    Trends,
    WorkshopListing,
    WorkshopSummary,
)
from ..sql import SqlDisabled, SqlError, SqlResult, SqlTimeout
from ..store.writes import utcnow
from .auth import require_api

router = APIRouter(prefix="/api", dependencies=[Depends(require_api)])

NDJSON = "application/x-ndjson"


def filters(
    labels: Annotated[
        str, Query(description="A label selector, every term matching.")
    ] = "",
    collection: Annotated[
        str | None,
        Query(description="The collection; empty for sessions outside any."),
    ] = None,
    source: str = "",
    version: str = "",
    frontend: str = "",
    host: str = "",
    platform: str = "",
    token_id: str = "",
    user: str = "",
    since: Annotated[str, Query(description="ISO 8601 UTC, inclusive.")] = "",
    until: Annotated[str, Query(description="ISO 8601 UTC, exclusive.")] = "",
    status: Annotated[str, Query(description="Statuses, comma separated.")] = "",
    include_incomplete: Annotated[
        bool, Query(description="Count sessions with missing events too.")
    ] = False,
) -> Filters:
    """The common filters, parsed from the query string."""

    try:
        return queries.parse_filters(
            labels=labels,
            collection=collection,
            source=source,
            version=version,
            frontend=frontend,
            host=host,
            platform=platform,
            token_id=token_id,
            user=user,
            since=since,
            until=until,
            status=status,
            include_incomplete=include_incomplete,
        )
    except QueryError as error:
        raise HTTPException(400, str(error)) from error


CommonFilters = Annotated[Filters, Depends(filters)]


def filters_with_name(
    filters: CommonFilters,
    name: Annotated[str, Query(description="The workshop's manifest name.")] = "",
) -> Filters:
    """The common filters plus a workshop name, for the routes not keyed by one.

    The name-keyed routes take the name from their path, where a query
    parameter of the same name would collide, so it is added here.
    """

    return filters.replace(name=name.strip())


AnyFilters = Annotated[Filters, Depends(filters_with_name)]


@contextmanager
def connected(request: Request) -> Iterator[Connection]:
    """A connection on the application's engine, for one answer."""

    with request.app.state.engine.connect() as connection:
        yield connection


async def answer[T](request: Request, question: Callable[[Connection], T]) -> T:
    """Run a question on the thread pool and turn its refusals into responses.

    An ambiguous workshop name answers 409 with the candidate
    collections in the detail, so the caller's next request can name
    one; nothing found is 404; a request the layer cannot read is 400.
    """

    def run() -> T:
        with connected(request) as connection:
            return question(connection)

    try:
        return await run_in_threadpool(run)
    except AmbiguousWorkshop as error:
        raise HTTPException(
            409,
            {
                "error": "ambiguous",
                "message": str(error),
                "name": error.name,
                "collections": error.collections,
            },
        ) from error
    except NotFound as error:
        raise HTTPException(404, str(error)) from error
    except QueryError as error:
        raise HTTPException(400, str(error)) from error


def settings_of(request: Request) -> Any:
    """The application's settings."""

    return request.app.state.settings


@router.get("/describe", summary="What the store holds and how answers are computed")
async def describe(request: Request) -> Description:
    """Event kinds and fields, session rules, metric definitions, and the
    label keys, collections and identity actually seen. Read this first."""

    settings = settings_of(request)

    return await answer(request, lambda c: queries.describe(c, utcnow(), settings))


@router.get("/workshops", summary="The workshops seen, as name and collection pairs")
async def workshops(request: Request, filters: AnyFilters) -> list[WorkshopListing]:
    """Discovery: every name and collection pair with sessions, with the
    sources and versions seen, counts, activity and completion."""

    settings = settings_of(request)

    return await answer(
        request, lambda c: queries.list_workshops(c, filters, utcnow(), settings)
    )


@router.get("/collections", summary="The collections seen and their workshops")
async def collections(request: Request, filters: AnyFilters) -> list[CollectionListing]:
    """Each collection with its workshops in the order instances took them."""

    settings = settings_of(request)

    return await answer(
        request, lambda c: queries.list_collections(c, filters, utcnow(), settings)
    )


@router.get(
    "/collections/progress", summary="The funnel across a collection's workshops"
)
async def collection_progress(
    request: Request, filters: AnyFilters
) -> CollectionProgress:
    """How many instances took each workshop of the collection, how many
    took them in order, and where they stopped. `collection` is required."""

    settings = settings_of(request)

    return await answer(
        request,
        lambda c: queries.collection_progress(c, filters, utcnow(), settings),
    )


def named(filters: Filters, name: str) -> Filters:
    """The filters with the route's workshop name in place."""

    return filters.replace(name=name)


@router.get("/workshops/{name}", summary="A workshop's outcomes, by version")
async def workshop_summary(
    request: Request,
    name: str,
    filters: CommonFilters,
    group_by: Annotated[
        str, Query(description="A dimension or label key.")
    ] = "version",
) -> WorkshopSummary:
    """Starts, resumes, finishes, abandons, lost, completion rate and
    duration percentiles, in total and by the grouping dimension."""

    settings = settings_of(request)
    narrowed = named(filters, name)

    return await answer(
        request,
        lambda c: queries.workshop_summary(
            c, narrowed, utcnow(), settings, group_by.strip() or "version"
        ),
    )


@router.get("/workshops/{name}/funnel", summary="Where journeys stop, page by page")
async def funnel(request: Request, name: str, filters: CommonFilters) -> Funnel:
    """Journeys reaching each page in order, and the page each stopped on."""

    settings = settings_of(request)
    narrowed = named(filters, name)

    return await answer(
        request, lambda c: queries.funnel(c, narrowed, utcnow(), settings)
    )


@router.get("/workshops/{name}/pages", summary="Time on each page")
async def pages(request: Request, name: str, filters: CommonFilters) -> PageTimings:
    """Time on each page from `page-leave`, as percentiles, with entries
    per session."""

    settings = settings_of(request)
    narrowed = named(filters, name)

    return await answer(
        request, lambda c: queries.page_timings(c, narrowed, utcnow(), settings)
    )


@router.get("/workshops/{name}/actions", summary="How each action is used")
async def actions(request: Request, name: str, filters: CommonFilters) -> ActionUsages:
    """Per action id: runs by trigger, ok, error, skipped and downgraded,
    and whether the clickable actions are used at all."""

    settings = settings_of(request)
    narrowed = named(filters, name)

    return await answer(
        request, lambda c: queries.action_usages(c, narrowed, utcnow(), settings)
    )


@router.get("/workshops/{name}/coverage", summary="What nobody ran, page by page")
async def coverage(request: Request, name: str, filters: CommonFilters) -> Coverage:
    """Per page and directive, the sessions whose page list named it and
    the sessions that ran it, from the directive inventory extension
    0.2.1 sends; sessions without one are counted and otherwise silent."""

    settings = settings_of(request)
    narrowed = named(filters, name)

    return await answer(
        request, lambda c: queries.coverage(c, narrowed, utcnow(), settings)
    )


@router.get("/workshops/{name}/checks", summary="Check and quiz pass rates")
async def checks(request: Request, name: str, filters: CommonFilters) -> Checks:
    """Verify and quiz pass rates, attempts before passing, hints opened
    and gates skipped."""

    settings = settings_of(request)
    narrowed = named(filters, name)

    return await answer(
        request, lambda c: queries.checks(c, narrowed, utcnow(), settings)
    )


@router.get("/workshops/{name}/trends", summary="Outcomes over time")
async def trends(
    request: Request,
    name: str,
    filters: CommonFilters,
    bucket: Annotated[str, Query(description="day or week.")] = "day",
    group_by: Annotated[str, Query(description="A dimension or label key.")] = "",
) -> Trends:
    """The summary's outcomes bucketed by day or week over the range, and
    by a dimension when asked."""

    settings = settings_of(request)
    narrowed = named(filters, name)

    return await answer(
        request,
        lambda c: queries.trends(
            c, narrowed, utcnow(), settings, bucket.strip(), group_by.strip()
        ),
    )


@router.get("/workshops/{name}/sessions", summary="A workshop's sessions")
async def workshop_sessions(
    request: Request,
    name: str,
    filters: CommonFilters,
    limit: Annotated[int, Query(ge=1, le=queries.MAX_LIMIT)] = queries.DEFAULT_LIMIT,
    cursor: str = "",
) -> SessionPage:
    """Sessions newest first with status, start, duration, last page and
    pages done; every session, whatever its quality."""

    settings = settings_of(request)
    narrowed = named(filters, name)

    return await answer(
        request,
        lambda c: queries.list_sessions(
            c,
            narrowed.replace(
                collection=queries.resolve_workshop(c, name, narrowed.collection)
            ),
            utcnow(),
            settings,
            limit,
            cursor,
        ),
    )


@router.get("/workshops/{name}/learners", summary="Sessions by learner")
async def learners(request: Request, name: str, filters: CommonFilters) -> Learners:
    """Sessions grouped by user with attempts, best progress and completion,
    and how many sessions carried no identity."""

    settings = settings_of(request)
    narrowed = named(filters, name)

    return await answer(
        request, lambda c: queries.learners(c, narrowed, utcnow(), settings)
    )


@router.get("/sessions", summary="Sessions by any filter")
async def sessions(
    request: Request,
    filters: AnyFilters,
    limit: Annotated[int, Query(ge=1, le=queries.MAX_LIMIT)] = queries.DEFAULT_LIMIT,
    cursor: str = "",
) -> SessionPage:
    """Sessions newest first across every workshop, cursor paged."""

    settings = settings_of(request)

    return await answer(
        request,
        lambda c: queries.list_sessions(c, filters, utcnow(), settings, limit, cursor),
    )


@router.get("/sessions/{session_id}", summary="One session and its timeline")
async def session(request: Request, session_id: str) -> SessionDetail:
    """The session's summary, its pages with time spent, its ordered
    timeline, its gaps, and the chain of sessions it belongs to."""

    settings = settings_of(request)

    return await answer(
        request, lambda c: queries.session_detail(c, session_id, utcnow(), settings)
    )


def events_response(
    request: Request, records: list[dict[str, Any]], format: str, extra: dict[str, Any]
) -> Response:
    """Events as JSON lines or as a JSON object, by `format` or `Accept`."""

    wants_lines = format == "ndjson" or (
        not format and NDJSON in request.headers.get("accept", "")
    )

    if wants_lines:
        body = "".join(
            json.dumps(record, separators=(",", ":")) + "\n" for record in records
        )

        return Response(body, media_type=NDJSON)

    return Response(
        json.dumps({"events": records, **extra}, separators=(",", ":")),
        media_type="application/json",
    )


@router.get("/sessions/{session_id}/events", summary="One session's raw events")
async def session_events(
    request: Request,
    session_id: str,
    format: Annotated[str, Query(description="json or ndjson.")] = "",
) -> Response:
    """The stored events of one session in order, as JSON or JSON lines,
    with the labels as the service merged them."""

    settings = settings_of(request)
    records = await answer(
        request, lambda c: queries.session_events(c, session_id, utcnow(), settings)
    )

    return events_response(request, records, format.strip().lower(), {})


@router.get("/events", summary="Raw events by any filter")
async def events(
    request: Request,
    filters: AnyFilters,
    kind: str = "",
    session_id: str = "",
    instance_id: str = "",
    page: str = "",
    id: str = "",
    event_status: Annotated[str, Query(alias="event_status")] = "",
    limit: Annotated[int, Query(ge=1, le=queries.MAX_LIMIT)] = queries.DEFAULT_LIMIT,
    cursor: str = "",
    format: Annotated[str, Query(description="json or ndjson.")] = "",
) -> Response:
    """The escape hatch: stored events by any filter, oldest first, cursor
    paged. `since` and `until` apply to the event's own timestamp; `status`
    filters sessions, `event_status` the event's own status field."""

    event_filters = EventFilters(
        kind=kind.strip(),
        session_id=session_id.strip(),
        instance_id=instance_id.strip(),
        page=page.strip(),
        id=id.strip(),
        status=event_status.strip(),
    )
    result: EventPage = await answer(
        request,
        lambda c: queries.query_events(
            c, filters.replace(status=()), event_filters, limit, cursor
        ),
    )

    return events_response(
        request,
        result.events,
        format.strip().lower(),
        {"next_cursor": result.next_cursor},
    )


@router.get("/instances/{instance_id}", summary="One running frontend's sessions")
async def instance(request: Request, instance_id: str) -> Instance:
    """The sessions of one running JupyterLab in the order they started,
    with each workshop's outcome."""

    settings = settings_of(request)

    return await answer(
        request, lambda c: queries.instance(c, instance_id, utcnow(), settings)
    )


@dataclass
class SqlRequest:
    """The body of a SQL request."""

    sql: str
    params: dict[str, Any] | None = None
    limit: int | None = None


@router.post("/sql", summary="Run one read-only SQL statement")
async def run_sql(request: Request, body: Annotated[SqlRequest, Body()]) -> SqlResult:
    """One SELECT or WITH against the store in its own dialect, with named
    `:params`, rows capped and the statement stopped at a deadline. The
    tables, the dialect and the notes on the JSON columns are in
    `describe`. A deployment can turn the tool off, in which case this
    answers 404."""

    tool = request.app.state.sql

    try:
        return await run_in_threadpool(tool.run, body.sql, body.params, body.limit)
    except SqlDisabled as error:
        raise HTTPException(404, str(error)) from error
    except SqlTimeout as error:
        raise HTTPException(408, str(error)) from error
    except SqlError as error:
        raise HTTPException(400, str(error)) from error
