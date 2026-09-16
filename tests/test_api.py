"""The query API: the routes, their guard, their errors and their formats."""

from __future__ import annotations

import json

import httpx
import pytest

from .conftest import COLLECTION, Seeded

pytestmark = pytest.mark.anyio


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


ROUTES = [
    "/api/describe",
    "/api/workshops",
    "/api/collections",
    "/api/collections/progress?collection=" + COLLECTION,
    "/api/workshops/hello-jupyterlab",
    "/api/workshops/hello-jupyterlab/funnel",
    "/api/workshops/hello-jupyterlab/pages",
    "/api/workshops/hello-jupyterlab/actions",
    "/api/workshops/hello-jupyterlab/coverage",
    "/api/workshops/hello-jupyterlab/checks",
    "/api/workshops/hello-jupyterlab/trends",
    "/api/workshops/hello-jupyterlab/sessions",
    "/api/workshops/hello-jupyterlab/learners",
    "/api/sessions",
    "/api/sessions/hello-gap",
    "/api/sessions/hello-gap/events",
    "/api/events",
    "/api/instances/inst-coll",
]


async def test_every_query_route_needs_an_api_token(
    client: httpx.AsyncClient, seeded: Seeded, dashboard_token: str
) -> None:
    for route in ROUTES:
        bare = await client.get(route)
        wrong = await client.get(route, headers=auth(dashboard_token))

        assert bare.status_code == 401, route
        assert wrong.status_code == 401, route
        assert "scope" in wrong.json()["detail"], route


async def test_every_query_route_answers_from_the_store(
    client: httpx.AsyncClient, seeded: Seeded, api_token: str
) -> None:
    for route in ROUTES:
        response = await client.get(route, headers=auth(api_token))

        assert response.status_code == 200, (route, response.text)

    summary = (
        await client.get("/api/workshops/hello-jupyterlab", headers=auth(api_token))
    ).json()

    assert summary["workshop"] == {"name": "hello-jupyterlab", "collection": ""}
    assert summary["data_quality"]["considered"] == 7
    assert summary["total"]["journeys"] == 4
    assert summary["total"]["completion_rate"] == round(2 / 3, 4)
    assert summary["groups"][0]["value"] == "0.1.0"

    listing = (await client.get("/api/workshops", headers=auth(api_token))).json()

    assert len(listing) == 4

    page = (
        await client.get(
            "/api/workshops/hello-jupyterlab/sessions",
            params={"limit": 2, "status": "finished,lost"},
            headers=auth(api_token),
        )
    ).json()

    assert page["total"] == 5
    assert len(page["sessions"]) == 2
    assert page["next_cursor"]

    detail = (
        await client.get("/api/sessions/hello-part2", headers=auth(api_token))
    ).json()

    assert detail["chain"] == ["hello-part1", "hello-part2"]
    assert detail["timeline"][0]["kind"] == "workshop-resume"
    assert detail["pages"][0]["directives"][0]["id"] == "01-welcome-1"

    report = (
        await client.get(
            "/api/workshops/hello-jupyterlab/coverage", headers=auth(api_token)
        )
    ).json()

    assert report["never_run"] == ["01-welcome-3", "01-welcome-5", "05-variables-2"]
    assert (
        next(d for d in report["pages"][0]["directives"] if d["id"] == "01-welcome-3")[
            "ran"
        ]
        == 0
    )


async def test_the_common_filters_reach_every_route(
    client: httpx.AsyncClient, seeded: Seeded, api_token: str
) -> None:
    narrowed = await client.get(
        "/api/workshops/hello-jupyterlab/funnel",
        params={"labels": "course=intro", "include_incomplete": "true"},
        headers=auth(api_token),
    )

    assert narrowed.json()["journeys"] == 1

    by_name = await client.get(
        "/api/sessions", params={"name": "why-a-workshop"}, headers=auth(api_token)
    )

    assert by_name.json()["total"] == 2

    grouped = await client.get(
        "/api/workshops/hello-jupyterlab",
        params={"group_by": "user"},
        headers=auth(api_token),
    )

    assert {g["value"] for g in grouped.json()["groups"]} == {"", "alice", "bob"}


async def test_an_ambiguous_name_answers_409_with_the_candidates(
    client: httpx.AsyncClient, seeded: Seeded, api_token: str
) -> None:
    response = await client.get(
        "/api/workshops/why-a-workshop", headers=auth(api_token)
    )
    detail = response.json()["detail"]

    assert response.status_code == 409
    assert detail["error"] == "ambiguous"
    assert detail["name"] == "why-a-workshop"
    assert detail["collections"] == ["", COLLECTION]

    chosen = await client.get(
        "/api/workshops/why-a-workshop",
        params={"collection": COLLECTION},
        headers=auth(api_token),
    )

    assert chosen.status_code == 200
    assert chosen.json()["workshop"]["collection"] == COLLECTION

    uncollected = await client.get(
        "/api/workshops/why-a-workshop",
        params={"collection": ""},
        headers=auth(api_token),
    )

    assert uncollected.status_code == 200
    assert uncollected.json()["workshop"]["collection"] == ""


async def test_bad_requests_answer_400_and_missing_things_404(
    client: httpx.AsyncClient, seeded: Seeded, api_token: str
) -> None:
    cases = {
        ("/api/sessions", "labels", "course=="): 400,
        ("/api/sessions", "since", "yesterday"): 400,
        ("/api/sessions", "status", "asleep"): 400,
        ("/api/sessions", "cursor", "???"): 400,
        ("/api/sessions", "limit", "0"): 422,
        ("/api/workshops/hello-jupyterlab/trends", "bucket", "month"): 400,
        ("/api/collections/progress", "collection", ""): 400,
    }

    for (route, key, value), expected in cases.items():
        response = await client.get(route, params={key: value}, headers=auth(api_token))

        assert response.status_code == expected, (route, key, value, response.text)

    for route in (
        "/api/workshops/nothing",
        "/api/sessions/nothing",
        "/api/instances/nothing",
    ):
        response = await client.get(route, headers=auth(api_token))

        assert response.status_code == 404, route


async def test_events_come_as_json_or_json_lines(
    client: httpx.AsyncClient, seeded: Seeded, api_token: str
) -> None:
    as_json = await client.get(
        "/api/sessions/hello-gap/events", headers=auth(api_token)
    )

    assert as_json.headers["content-type"].startswith("application/json")
    assert len(as_json.json()["events"]) == 60

    by_format = await client.get(
        "/api/sessions/hello-gap/events",
        params={"format": "ndjson"},
        headers=auth(api_token),
    )
    lines = by_format.text.splitlines()

    assert by_format.headers["content-type"].startswith("application/x-ndjson")
    assert len(lines) == 60
    assert json.loads(lines[0])["kind"] == "workshop-start"

    by_accept = await client.get(
        "/api/events",
        params={"kind": "workshop-finish", "limit": 2},
        headers={**auth(api_token), "accept": "application/x-ndjson"},
    )

    assert by_accept.headers["content-type"].startswith("application/x-ndjson")
    assert len(by_accept.text.splitlines()) == 2

    paged = await client.get(
        "/api/events",
        params={"kind": "workshop-finish", "limit": 2},
        headers=auth(api_token),
    )
    body = paged.json()

    assert len(body["events"]) == 2
    assert body["next_cursor"]


async def test_the_openapi_document_describes_the_routes(
    client: httpx.AsyncClient,
) -> None:
    document = (await client.get("/openapi.json")).json()
    paths = document["paths"]

    assert "/api/describe" in paths
    assert "/api/workshops/{name}/funnel" in paths
    assert "/api/sessions/{session_id}/events" in paths

    parameters = {
        p["name"] for p in paths["/api/workshops/{name}"]["get"]["parameters"]
    }

    assert {"name", "labels", "collection", "since", "until", "group_by"} <= parameters

    schemas = document["components"]["schemas"]

    assert "DataQuality" in schemas
    assert "Outcomes" in schemas
