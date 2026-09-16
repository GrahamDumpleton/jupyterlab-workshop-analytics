"""The sink: tokens, bodies, validation, dedupe, labels and the trace it leaves."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import wrapture
from sqlalchemy import select

from workshop_analytics.app import create_app
from workshop_analytics.config import Settings
from workshop_analytics.ingest import Ingest
from workshop_analytics.store.tables import events

from .conftest import bearer, load_fixture, mint, ndjson

REQUEST = "fastapi.applications:FastAPI.__call__"

pytestmark = pytest.mark.anyio


@pytest.fixture
def traced_app(settings: Settings) -> Iterator[Any]:
    """The app built under the packaged FastAPI instrumentation.

    Built inside the scope so the routes register while the
    instrumentation is watching, and every request records as one tree.
    """

    with wrapture.instrumentation("fastapi"):
        app = create_app(settings)

        yield app

        app.state.engine.dispose()


@pytest.fixture
async def traced_client(traced_app: Any) -> Any:
    transport = httpx.ASGITransport(app=traced_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_a_batch_is_accepted_and_traced(
    traced_client: httpx.AsyncClient, ingest_token: str
) -> None:
    accept = wrapture.binding(Ingest, "accept")
    body = ndjson(load_fixture("why-a-workshop"))

    with wrapture.timeline(accept) as tape:
        response = await traced_client.post(
            "/events", content=body, headers=bearer(ingest_token)
        )

        assert response.status_code == 202
        assert response.json() == {
            "received": 23,
            "stored": 23,
            "duplicates": 0,
            "rejected": 0,
            "problems": [],
        }

        # The request is the root; the pipeline ran inside it, across the
        # thread pool hop, and its phases and counts are on the tape.
        request = tape.where(path=REQUEST).assert_once()
        call = accept.events.assert_once().first

        assert request.first.result == "202 Accepted"
        assert tape.parent_of(call) is request.first
        assert call.data["stored"] == 23
        assert call.data["received"] == 23

        for phase in ("ingest.parse", "ingest.store", "ingest.broadcast"):
            block = tape.blocks(phase).assert_once().first

            assert tape.parent_of(block) is call


async def test_a_resend_stores_nothing_new(
    client: httpx.AsyncClient, ingest_token: str
) -> None:
    body = ndjson(load_fixture("guided-not-documented"))
    insert = wrapture.binding("workshop_analytics.ingest", "insert_events")

    with wrapture.timeline(insert):
        first = await client.post("/events", content=body, headers=bearer(ingest_token))
        second = await client.post(
            "/events", content=body, headers=bearer(ingest_token)
        )

        assert first.json()["stored"] == 25
        assert second.json() == {
            "received": 25,
            "stored": 0,
            "duplicates": 25,
            "rejected": 0,
            "problems": [],
        }

        insert.events.assert_times(2)
        assert insert.events.last.result == []


async def test_a_malformed_line_is_rejected_alone_and_logged(
    traced_client: httpx.AsyncClient, ingest_token: str
) -> None:
    good = load_fixture("why-a-workshop")
    broken = dict(good[3])

    del broken["session_id"]

    body = ndjson(good[:3]) + b"not json at all\n" + ndjson([broken]) + ndjson(good[4:])
    logs = wrapture.capture_logs("workshop_analytics.ingest")
    accept = wrapture.binding(Ingest, "accept")

    with wrapture.timeline(logs, accept) as tape:
        response = await traced_client.post(
            "/events", content=body, headers=bearer(ingest_token)
        )

        payload = response.json()

        assert response.status_code == 202
        assert payload["received"] == 24
        assert payload["stored"] == 22
        assert payload["rejected"] == 2
        assert payload["problems"][0] == "line 4: not JSON"
        assert "session_id" in payload["problems"][1]

        warning = logs.events.at_level("WARNING").assert_once().first
        parse = tape.blocks("ingest.parse").assert_once().first

        assert tape.parent_of(warning) is parse
        assert tape.parent_of(parse) is accept.events.first


async def test_the_token_is_accepted_in_the_header_or_the_query(
    traced_client: httpx.AsyncClient, ingest_token: str
) -> None:
    body = ndjson(load_fixture("why-a-workshop")[:2])

    with wrapture.timeline() as tape:
        by_header = await traced_client.post(
            "/events", content=body, headers=bearer(ingest_token)
        )
        by_query = await traced_client.post(
            f"/events?token={ingest_token}",
            content=body,
            headers={"content-type": "application/x-ndjson"},
        )

        assert by_header.status_code == 202
        assert by_query.status_code == 202

        # The recorded requests carry the token in neither path nor query.
        requests = tape.where(path=REQUEST).assert_times(2)

        for event in (requests.first, requests.last):
            assert ingest_token not in str(event.data.get("path", ""))
            assert ingest_token not in str(event.data.get("query", ""))

        assert requests.last.data["query"] == "token=<redacted>"


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "expired",
        "not-yet",
        "wrong-scope",
        "wrong-key",
        "denied",
    ],
)
async def test_every_bad_token_answers_404(
    client: httpx.AsyncClient, key: bytes, settings: Settings, token: str
) -> None:
    body = ndjson(load_fixture("why-a-workshop")[:1])
    value = token

    if token == "expired":
        value, claims = mint(key, expires="1h")
        frozen = float(claims.expires_at + 10)
    elif token == "not-yet":
        value, claims = mint(key, not_before=int(wrapture_now()) + 3600, expires="1d")
        frozen = float(claims.not_before - 10)
    elif token == "wrong-scope":
        value, _ = mint(key, ("api",))
        frozen = 0.0
    elif token == "wrong-key":
        from workshop_analytics.tokens import decode_key, generate_key

        value, _ = mint(decode_key(generate_key()))
        frozen = 0.0
    elif token == "denied":
        value, claims = mint(key)
        frozen = 0.0

        assert settings.denied_tokens_file is not None

        settings.denied_tokens_file.write_text(claims.jti + "\n")
    else:
        frozen = 0.0

    headers = bearer(value) if value else {"content-type": "application/x-ndjson"}

    from workshop_analytics import tokens

    logs = wrapture.capture_logs("workshop_analytics.api.auth")

    with wrapture.timeline(logs):
        if frozen:
            with wrapture.binding(tokens, attr="now").overrides(lambda: frozen):
                response = await client.post("/events", content=body, headers=headers)
        else:
            response = await client.post("/events", content=body, headers=headers)

        assert response.status_code == 404

        # The refusal is logged with its reason, for the operator, and the
        # response carries the same reason; the token itself is in neither.
        warning = logs.events.at_level("WARNING").assert_once().first
        message = warning.data["message"]

        assert message.startswith("POST /events refused: ")
        assert not value or value not in message

        if value:
            assert response.json()["detail"] in message


def wrapture_now() -> float:
    from workshop_analytics import tokens

    return tokens.now()


async def test_token_labels_win_and_client_labels_keep_their_place(
    client: httpx.AsyncClient, key: bytes, app: Any
) -> None:
    token, claims = mint(key, labels={"course": "intro-git", "term": "2026-s2"})
    event = dict(load_fixture("why-a-workshop")[0])

    event["labels"] = {"course": "something-else", "cohort": "a"}

    response = await client.post(
        "/events", content=ndjson([event]), headers=bearer(token)
    )

    assert response.status_code == 202

    with app.state.engine.connect() as connection:
        row = connection.execute(select(events)).one()

    assert row.labels == {
        "course": "intro-git",
        "term": "2026-s2",
        "client.course": "something-else",
        "cohort": "a",
    }
    assert row.token_id == claims.jti
    assert row.payload["labels"] == {"course": "something-else", "cohort": "a"}


async def test_an_event_breaking_the_label_rules_is_rejected_with_the_reason(
    client: httpx.AsyncClient, ingest_token: str
) -> None:
    event = dict(load_fixture("why-a-workshop")[0])

    event["labels"] = {f"k{i}": "v" for i in range(17)}

    response = await client.post(
        "/events", content=ndjson([event]), headers=bearer(ingest_token)
    )
    payload = response.json()

    assert response.status_code == 202
    assert payload["stored"] == 0
    assert payload["rejected"] == 1
    assert "labels" in payload["problems"][0]


async def test_a_json_array_body_is_the_import_shape(
    client: httpx.AsyncClient, ingest_token: str
) -> None:
    body = json.dumps(load_fixture("why-a-workshop")).encode()
    headers = {
        "authorization": f"Bearer {ingest_token}",
        "content-type": "application/json",
    }

    response = await client.post("/events", content=body, headers=headers)

    assert response.status_code == 202
    assert response.json()["stored"] == 23


async def test_a_body_that_is_not_a_batch_is_refused_whole(
    traced_client: httpx.AsyncClient, ingest_token: str
) -> None:
    headers = {
        "authorization": f"Bearer {ingest_token}",
        "content-type": "application/json",
    }

    with wrapture.timeline() as tape:
        response = await traced_client.post("/events", content=b"{", headers=headers)

        assert response.status_code == 400
        assert "not JSON" in response.json()["detail"]

        request = tape.where(path=REQUEST).assert_once()

        # The malformed body was turned into a 400 rather than raised, and
        # the request event still knows the failure.
        assert request.first.failed
        assert request.first.caught

    unsupported = await traced_client.post(
        "/events",
        content=b"x",
        headers={**headers, "content-type": "image/png"},
    )

    assert unsupported.status_code == 400


async def test_a_body_past_the_cap_is_refused(key_text: str, tmp_path: Any) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'small.db'}",
        signing_key=key_text,
        max_body_bytes=200,
    )
    app = create_app(settings)
    token, _ = mint(app.state.signing_key)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.post(
            "/events",
            content=ndjson(load_fixture("why-a-workshop")),
            headers=bearer(token),
        )

    app.state.engine.dispose()

    assert response.status_code == 413


async def test_a_token_is_rate_limited_per_minute(key_text: str, tmp_path: Any) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rate.db'}",
        signing_key=key_text,
        rate_limit_per_minute=2,
    )
    app = create_app(settings)
    token, _ = mint(app.state.signing_key)
    body = ndjson(load_fixture("why-a-workshop")[:1])
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        statuses = [
            (await c.post("/events", content=body, headers=bearer(token))).status_code
            for _ in range(3)
        ]

    app.state.engine.dispose()

    assert statuses == [202, 202, 429]


async def test_cross_origin_posts_follow_the_token_origins(
    client: httpx.AsyncClient, key: bytes
) -> None:
    token, _ = mint(key, origins=("https://lite.example",))
    body = ndjson(load_fixture("why-a-workshop")[:1])

    preflight = await client.options(
        f"/events?token={token}", headers={"origin": "https://lite.example"}
    )

    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "https://lite.example"
    assert "Authorization" in preflight.headers["access-control-allow-headers"]

    refused = await client.options(
        f"/events?token={token}", headers={"origin": "https://other.example"}
    )

    assert refused.status_code == 404

    blind = await client.options("/events", headers={"origin": "https://lite.example"})

    assert blind.status_code == 404

    posted = await client.post(
        "/events",
        content=body,
        headers={**bearer(token), "origin": "https://lite.example"},
    )

    assert posted.status_code == 202
    assert posted.headers["access-control-allow-origin"] == "https://lite.example"


async def test_a_deployment_can_allow_origins_for_every_token(
    key_text: str, tmp_path: Any
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'cors.db'}",
        signing_key=key_text,
        allowed_origins=("https://site.example",),
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        preflight = await c.options(
            "/events", headers={"origin": "https://site.example"}
        )

    app.state.engine.dispose()

    assert preflight.status_code == 204


async def test_an_unknown_field_is_stored_whole_and_warned_once(
    traced_app: Any, traced_client: httpx.AsyncClient, ingest_token: str
) -> None:
    """A field the vendored schema does not know rides along.

    The schema is refreshed at an extension release, and the extension
    that sends a new field may reach a deployment first. The event is
    stored as it arrived, so a later `rebuild` under the refreshed
    schema reads the field, and the log says once what was seen.
    """

    good = load_fixture("why-a-workshop")
    start = next(e for e in good if e["kind"] == "workshop-start")
    newer = dict(start)
    newer["pages"] = [{**page, "colour": "red"} for page in start["pages"]]
    unknown_kind = {
        **good[1],
        "kind": "something-new",
        "seq": len(good) + 1,
        "extra": True,
    }
    batch = [newer if e is start else e for e in good] + [unknown_kind]
    logs = wrapture.capture_logs("workshop_analytics.ingest")

    with wrapture.timeline(logs):
        first = await traced_client.post(
            "/events", content=ndjson(batch), headers=bearer(ingest_token)
        )
        second = await traced_client.post(
            "/events", content=ndjson(batch), headers=bearer(ingest_token)
        )

        assert first.json()["stored"] == len(batch)
        assert first.json()["rejected"] == 0
        assert second.json()["duplicates"] == len(batch)

        warning = logs.events.at_level("WARNING").assert_once().first

        assert "pages[].colour" in warning.data["message"]

    # Both events are stored as they arrived, unknown parts included.
    with traced_app.state.engine.connect() as connection:
        rows = connection.execute(
            select(events.c.kind, events.c.payload).where(
                events.c.session_id == start["session_id"]
            )
        ).all()

    payloads = {kind: payload for kind, payload in rows}

    assert payloads["workshop-start"]["pages"][0]["colour"] == "red"
    assert payloads["something-new"]["extra"] is True
