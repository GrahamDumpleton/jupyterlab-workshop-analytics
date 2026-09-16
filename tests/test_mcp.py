"""The MCP server: its guard, its tools and their agreement with the API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from .conftest import COLLECTION, Seeded, mint

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "describe",
    "list_workshops",
    "list_collections",
    "collection_progress",
    "workshop_summary",
    "funnel",
    "page_timing",
    "action_usage",
    "coverage",
    "checks",
    "trends",
    "list_sessions",
    "session_timeline",
    "session_events",
    "query_events",
    "instance",
    "learners",
    "live",
    "sql",
}


class Client:
    """A JSON-RPC client speaking the streamable HTTP transport in-process."""

    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self.http = http
        self.headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        self.counter = 0

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.counter += 1
        message = {"jsonrpc": "2.0", "id": self.counter, "method": method}

        if params is not None:
            message["params"] = params

        response = await self.http.post("/mcp", json=message, headers=self.headers)

        assert response.status_code == 200, response.text

        return response.json()

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """A tool's result, its structured content or its error text."""

        result = (await self.rpc("tools/call", {"name": name, "arguments": arguments}))[
            "result"
        ]

        if result.get("isError"):
            return {"error": result["content"][0]["text"]}

        structured = result.get("structuredContent")

        if structured is not None:
            return dict(structured)

        return dict(json.loads(result["content"][0]["text"]))


@pytest.fixture
async def running(app: Any) -> AsyncIterator[Any]:
    """The app with its lifespan running, which the MCP transport needs."""

    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def mcp(running: Any, seeded: Seeded, api_token: str) -> AsyncIterator[Client]:
    transport = httpx.ASGITransport(app=running)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield Client(http, api_token)


async def test_the_endpoint_needs_an_api_token(
    running: Any, key: bytes, dashboard_token: str
) -> None:
    transport = httpx.ASGITransport(app=running)
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        bare = await http.post("/mcp", json=message)
        wrong = await http.post(
            "/mcp", json=message, headers={"authorization": f"Bearer {dashboard_token}"}
        )
        right = await http.post(
            "/mcp",
            json=message,
            headers={
                "authorization": f"Bearer {mint(key, ('api',))[0]}",
                "accept": "application/json, text/event-stream",
            },
        )

    assert bare.status_code == 401
    assert wrong.status_code == 401
    assert "scope" in wrong.json()["detail"]
    assert right.status_code == 200


async def test_initialize_and_list_tools(mcp: Client) -> None:
    initialized = await mcp.rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )

    assert (
        initialized["result"]["serverInfo"]["name"] == "jupyterlab-workshop-analytics"
    )
    assert "describe" in initialized["result"]["instructions"]

    listed = await mcp.rpc("tools/list")
    tools = {tool["name"]: tool for tool in listed["result"]["tools"]}

    assert set(tools) == EXPECTED_TOOLS
    assert "select" in tools["funnel"]["inputSchema"]["properties"]
    assert tools["funnel"]["inputSchema"]["required"] == ["name"]
    assert "labels" in json.dumps(tools["funnel"]["inputSchema"])


async def test_the_tools_answer_as_the_api_does(
    mcp: Client, seeded: Seeded, api_token: str
) -> None:
    summary = await mcp.call("workshop_summary", {"name": "hello-jupyterlab"})
    over_http = (
        await mcp.http.get(
            "/api/workshops/hello-jupyterlab",
            headers={"authorization": f"Bearer {api_token}"},
        )
    ).json()

    assert summary == over_http

    described = await mcp.call("describe", {})

    assert described["sql"]["enabled"] is True
    assert "hello-jupyterlab" in json.dumps(await mcp.call("list_workshops", {}))

    narrowed = await mcp.call(
        "funnel",
        {"name": "hello-jupyterlab", "select": {"labels": "course=intro"}},
    )

    assert narrowed["journeys"] == 1

    grouped = await mcp.call(
        "workshop_summary", {"name": "hello-jupyterlab", "group_by": "user"}
    )

    assert {g["value"] for g in grouped["groups"]} == {"", "alice", "bob"}

    timeline = await mcp.call("session_timeline", {"session_id": seeded.part2})

    assert timeline["chain"] == [seeded.part1, seeded.part2]

    page = await mcp.call("list_sessions", {"name": "hello-jupyterlab", "limit": 3})

    assert page["total"] == 7
    assert page["next_cursor"]

    events = await mcp.call("query_events", {"kind": "workshop-finish", "limit": 2})

    assert len(events["events"]) == 2

    progress = await mcp.call("collection_progress", {"collection": COLLECTION})

    assert progress["instances"] == 2

    report = await mcp.call("coverage", {"name": "hello-jupyterlab"})

    assert report["never_run"] == ["01-welcome-3", "01-welcome-5", "05-variables-2"]

    live = await mcp.call("live", {})

    assert [s["session_id"] for s in live["sessions"]] == [seeded.live]

    rows = await mcp.call(
        "sql",
        {
            "sql": "select count(*) as n from sessions where user = :u",
            "params": {"u": "alice"},
        },
    )

    assert rows["rows"] == [[2]]


async def test_refusals_come_back_as_tool_errors(mcp: Client) -> None:
    ambiguous = await mcp.call("workshop_summary", {"name": "why-a-workshop"})

    assert "more than one collection" in ambiguous["error"]
    assert COLLECTION in ambiguous["error"]

    missing = await mcp.call("session_timeline", {"session_id": "nothing"})

    assert "no session" in missing["error"]

    bad_filter = await mcp.call("list_sessions", {"select": {"status": "asleep"}})

    assert "unknown status" in bad_filter["error"]

    bad_sql = await mcp.call("sql", {"sql": "delete from events"})

    assert "DELETE" in bad_sql["error"]

    bad_bucket = await mcp.call(
        "trends", {"name": "hello-jupyterlab", "bucket": "month"}
    )

    assert "bucket" in bad_bucket["error"]
