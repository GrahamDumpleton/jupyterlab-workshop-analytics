"""The MCP server: the query API's questions as tools, at `/mcp`.

The tools are the same functions the REST routes call, wrapped once
more, so the two can never answer differently. The server speaks the
streamable HTTP transport, stateless with plain JSON responses, and
sits behind the same bearer check as `/api`: a token with the `api`
scope, sent as `Authorization: Bearer <token>`. The MCP SDK's own
OAuth machinery is not used; the service's tokens are the credential.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from sqlalchemy import Connection
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__, queries
from .api.auth import bearer_token, refused, verified
from .projection import live_rows
from .queries import AmbiguousWorkshop, EventFilters, Filters, QueryError
from .selectors import matches
from .sql import SqlError
from .store.writes import utcnow
from .tokens import TokenError

MCP_PATH = "/mcp"

INSTRUCTIONS = """\
Progress analytics for jupyterlab-workshop workshops. Call describe
first: it states the event contract, the session statuses and the
thresholds this deployment runs with, how every metric is defined,
the filters, and what the store actually holds (label keys, hosts,
collections, whether sessions carry a learner identity). Then
list_workshops to see what can be asked about. Every summary carries
a data_quality note; read it before drawing a conclusion. A workshop
is identified by name and collection; when a name is ambiguous the
tool says which collections it has, so pass collection next. Rates
count journeys, chains of sessions linked by resumes, not sessions.
"""


class Selection(BaseModel):
    """The common filters every question takes, as a tool argument."""

    labels: str = Field(
        default="",
        description="A label selector: course=intro-git,term!=2025,"
        "cohort in (a,b),host notin (x). Every term must match.",
    )
    collection: str | None = Field(
        default=None,
        description="The collection the workshop was subscribed from. "
        "Empty string selects sessions opened outside any collection.",
    )
    source: str = Field(default="", description="Where the copy came from.")
    version: str = Field(default="", description="The workshop's manifest version.")
    frontend: str = Field(default="", description="jupyterlab or jupyterlite.")
    host: str = Field(
        default="", description="local, binder, jupyterhub, codespaces or static."
    )
    platform: str = Field(
        default="", description="linux, macos, windows or emscripten."
    )
    token_id: str = Field(default="", description="The jti of the posting token.")
    user: str = Field(default="", description="The learner's identity, where present.")
    since: str = Field(
        default="", description="ISO 8601 UTC; sessions started at or after it."
    )
    until: str = Field(
        default="", description="ISO 8601 UTC; sessions started before it."
    )
    status: str = Field(
        default="",
        description="Session statuses, comma separated: active, away, silent, "
        "lost, resumed, finished, abandoned.",
    )
    include_incomplete: bool = Field(
        default=False,
        description="Count sessions with missing events in rates and timings.",
    )


def to_filters(select: Selection | None, name: str = "") -> Filters:
    """The typed filters for a selection, or a `ToolError` naming the fault."""

    values = select.model_dump() if select is not None else {}

    try:
        return queries.parse_filters(name=name, **values)
    except QueryError as error:
        raise ToolError(str(error)) from error


class Guard:
    """The bearer check in front of the MCP transport.

    An ASGI wrapper rather than the SDK's auth middleware, so the same
    verification the routes use, with its deny list and its refusal
    log, decides here too.
    """

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)

            return

        request = Request(scope, receive)
        token = bearer_token(request)

        if not token:
            refused(request, "no token was sent")

            await JSONResponse({"detail": "a token is required"}, 401)(
                scope, receive, send
            )

            return

        try:
            verified(request, token, "api")
        except TokenError as error:
            await JSONResponse({"detail": str(error)}, 401)(scope, receive, send)

            return

        await self.inner(scope, receive, send)


def build_server(app: FastAPI) -> MCPServer:
    """The MCP server with one tool per question, bound to the application."""

    server = MCPServer(
        name="jupyterlab-workshop-analytics",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    state = app.state

    async def ask[T](question: Callable[[Connection], T]) -> T:
        """Run a question on the thread pool, refusals as tool errors."""

        def run() -> T:
            with state.engine.connect() as connection:
                return question(connection)

        try:
            return await run_in_threadpool(run)
        except AmbiguousWorkshop as error:
            raise ToolError(
                f"{error}; its collections are "
                + ", ".join(repr(item) for item in error.collections)
            ) from error
        except QueryError as error:
            raise ToolError(str(error)) from error

    def now() -> Any:
        return utcnow()

    @server.tool(
        description="What the store holds and how every answer is computed. Read first."
    )
    async def describe() -> dict[str, Any]:
        return asdict(await ask(lambda c: queries.describe(c, now(), state.settings)))

    @server.tool(
        description="Every workshop seen, as name and collection pairs, with "
        "counts, completion and activity."
    )
    async def list_workshops(select: Selection | None = None) -> dict[str, Any]:
        filters = to_filters(select)
        listing = await ask(
            lambda c: queries.list_workshops(c, filters, now(), state.settings)
        )

        return {"workshops": [asdict(item) for item in listing]}

    @server.tool(
        description="The collections seen, each with its workshops in the "
        "order instances took them."
    )
    async def list_collections(select: Selection | None = None) -> dict[str, Any]:
        filters = to_filters(select)
        listing = await ask(
            lambda c: queries.list_collections(c, filters, now(), state.settings)
        )

        return {"collections": [asdict(item) for item in listing]}

    @server.tool(
        description="The funnel across a collection's workshops: instances "
        "reaching each, in order, and where they stopped."
    )
    async def collection_progress(
        collection: str, select: Selection | None = None
    ) -> dict[str, Any]:
        filters = to_filters(select).replace(collection=collection)

        return asdict(
            await ask(
                lambda c: queries.collection_progress(c, filters, now(), state.settings)
            )
        )

    @server.tool(
        description="A workshop's outcomes: starts, resumes, finished, finished "
        "skipping gates, abandoned, lost, completion rate and duration "
        "percentiles, in total "
        "and by a dimension (version by default, or a field or label key)."
    )
    async def workshop_summary(
        name: str, select: Selection | None = None, group_by: str = "version"
    ) -> dict[str, Any]:
        filters = to_filters(select, name)

        return asdict(
            await ask(
                lambda c: queries.workshop_summary(
                    c, filters, now(), state.settings, group_by.strip() or "version"
                )
            )
        )

    @server.tool(
        description="Journeys reaching each page of a workshop in order, and "
        "the page each stopped on."
    )
    async def funnel(name: str, select: Selection | None = None) -> dict[str, Any]:
        filters = to_filters(select, name)

        return asdict(
            await ask(lambda c: queries.funnel(c, filters, now(), state.settings))
        )

    @server.tool(
        description="Time on each page of a workshop from page-leave, as "
        "percentiles, with entries per session."
    )
    async def page_timing(name: str, select: Selection | None = None) -> dict[str, Any]:
        filters = to_filters(select, name)

        return asdict(
            await ask(lambda c: queries.page_timings(c, filters, now(), state.settings))
        )

    @server.tool(
        description="Per action of a workshop: runs by trigger, ok, error, "
        "skipped and downgraded, and whether clickable actions are used."
    )
    async def action_usage(
        name: str, select: Selection | None = None
    ) -> dict[str, Any]:
        filters = to_filters(select, name)

        return asdict(
            await ask(
                lambda c: queries.action_usages(c, filters, now(), state.settings)
            )
        )

    @server.tool(
        description="What nobody ran in a workshop: per page and directive, the "
        "sessions whose page list named it and the sessions that ran it, with "
        "how each was expected to start; only sessions from extension 0.2.1 "
        "and later carry the inventory."
    )
    async def coverage(name: str, select: Selection | None = None) -> dict[str, Any]:
        filters = to_filters(select, name)

        return asdict(
            await ask(lambda c: queries.coverage(c, filters, now(), state.settings))
        )

    @server.tool(
        description="Verify and quiz pass rates of a workshop, attempts before "
        "passing, hints opened and gates skipped."
    )
    async def checks(name: str, select: Selection | None = None) -> dict[str, Any]:
        filters = to_filters(select, name)

        return asdict(
            await ask(lambda c: queries.checks(c, filters, now(), state.settings))
        )

    @server.tool(
        description="A workshop's outcomes bucketed by day or week, and by a "
        "dimension when group_by is given."
    )
    async def trends(
        name: str,
        select: Selection | None = None,
        bucket: str = "day",
        group_by: str = "",
    ) -> dict[str, Any]:
        filters = to_filters(select, name)

        return asdict(
            await ask(
                lambda c: queries.trends(
                    c, filters, now(), state.settings, bucket.strip(), group_by.strip()
                )
            )
        )

    @server.tool(
        description="Sessions newest first, every session whatever its "
        "quality, cursor paged; name narrows to one workshop."
    )
    async def list_sessions(
        select: Selection | None = None,
        name: str = "",
        limit: int = queries.DEFAULT_LIMIT,
        cursor: str = "",
    ) -> dict[str, Any]:
        filters = to_filters(select, name)

        def question(connection: Connection) -> queries.SessionPage:
            narrowed = filters

            if name:
                collection = queries.resolve_workshop(
                    connection, name, filters.collection
                )
                narrowed = filters.replace(collection=collection)

            return queries.list_sessions(
                connection, narrowed, now(), state.settings, limit, cursor
            )

        return asdict(await ask(question))

    @server.tool(
        description="One session: its summary, its pages with time spent, "
        "its ordered timeline, its gaps and the chain it belongs to."
    )
    async def session_timeline(session_id: str) -> dict[str, Any]:
        return asdict(
            await ask(
                lambda c: queries.session_detail(c, session_id, now(), state.settings)
            )
        )

    @server.tool(
        description="The raw stored events of one session in order, with the "
        "labels as the service merged them."
    )
    async def session_events(session_id: str) -> dict[str, Any]:
        records = await ask(
            lambda c: queries.session_events(c, session_id, now(), state.settings)
        )

        return {"events": records}

    @server.tool(
        description="Raw events by any filter, oldest first, cursor paged. "
        "since and until apply to the event's own timestamp; event_status is "
        "the event's own status field; id is an action, check, quiz, form or "
        "hint id."
    )
    async def query_events(
        select: Selection | None = None,
        name: str = "",
        kind: str = "",
        session_id: str = "",
        instance_id: str = "",
        page: str = "",
        id: str = "",
        event_status: str = "",
        limit: int = queries.DEFAULT_LIMIT,
        cursor: str = "",
    ) -> dict[str, Any]:
        filters = to_filters(select, name).replace(status=())
        event_filters = EventFilters(
            kind=kind.strip(),
            session_id=session_id.strip(),
            instance_id=instance_id.strip(),
            page=page.strip(),
            id=id.strip(),
            status=event_status.strip(),
        )

        return asdict(
            await ask(
                lambda c: queries.query_events(c, filters, event_filters, limit, cursor)
            )
        )

    @server.tool(
        description="The sessions of one running frontend in the order they "
        "started, with each workshop's outcome."
    )
    async def instance(instance_id: str) -> dict[str, Any]:
        return asdict(
            await ask(lambda c: queries.instance(c, instance_id, now(), state.settings))
        )

    @server.tool(
        description="A workshop's sessions grouped by learner identity, with "
        "attempts, best progress and completion; anonymous sessions counted."
    )
    async def learners(name: str, select: Selection | None = None) -> dict[str, Any]:
        filters = to_filters(select, name)

        return asdict(
            await ask(lambda c: queries.learners(c, filters, now(), state.settings))
        )

    @server.tool(
        description="The sessions in progress right now, as the live "
        "dashboard shows them, narrowed by a label selector."
    )
    async def live(labels: str = "") -> dict[str, Any]:
        try:
            terms = queries.parse_filters(labels=labels).labels
        except QueryError as error:
            raise ToolError(str(error)) from error

        moment = now()
        rows = await run_in_threadpool(live_rows, state.engine, moment, state.settings)

        return {
            "now": queries.iso(moment),
            "sessions": [row for row in rows if matches(row["labels"], terms)],
        }

    @server.tool(
        description="Run one read-only SQL statement (SELECT or WITH) against "
        "the store, in its own dialect, with named :params. Rows are capped "
        "and the statement is stopped at a deadline. describe lists the "
        "tables, the dialect and the notes on JSON columns."
    )
    async def sql(
        sql: str,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(state.sql.run, sql, params, limit)
        except SqlError as error:
            raise ToolError(str(error)) from error

        return asdict(result)

    return server


def mount(app: FastAPI) -> MCPServer:
    """Build the server and put its transport at `/mcp` behind the guard.

    The SDK's Starlette app is used as the route's endpoint, so its
    router sees the same path it was built for; its lifespan is not
    run by the mount, so the application's lifespan runs the session
    manager itself. DNS rebinding protection is off: the service runs
    behind an ingress under a real host name, and the bearer token is
    the guard.
    """

    server = build_server(app)
    transport = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    app.router.routes.append(
        Route(MCP_PATH, endpoint=Guard(transport), methods=["GET", "POST", "DELETE"])
    )

    return server
