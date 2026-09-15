"""The broadcaster, the stream and the live endpoints."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import wrapture
from sqlalchemy import Engine

from workshop_analytics.config import Settings
from workshop_analytics.ingest import Ingest
from workshop_analytics.live import (
    CLOSED,
    CLOSING,
    QUEUE_SIZE,
    Broadcaster,
    frame,
    stream,
)
from workshop_analytics.schema import EventValidator

from .conftest import bearer, load_fixture, mint, ndjson, shifted_to_now


def test_ingest_publishes_one_delta_per_session(
    engine: Engine, settings: Settings
) -> None:
    broadcaster = wrapture.mock(Broadcaster)
    ingest = Ingest(
        engine=engine,
        settings=settings,
        validator=EventValidator(),
        broadcaster=broadcaster,
    )
    events = load_fixture("why-a-workshop")

    with wrapture.timeline():
        ingest.accept(events, None)

        published = broadcaster.publish.events.assert_once().first

        assert published.arguments["delta"]["session_id"] == events[0]["session_id"]
        assert published.arguments["delta"]["status"] == "finished"


@pytest.mark.anyio
async def test_fan_out_and_slow_subscriber_dropping() -> None:
    broadcaster = Broadcaster()
    quick = broadcaster.subscribe()
    slow = broadcaster.subscribe()

    assert broadcaster.subscribers == 2

    await asyncio.to_thread(broadcaster.publish, {"session_id": "a"})
    await asyncio.sleep(0)

    assert (await quick.queue.get()) == {"session_id": "a"}
    assert slow.queue.qsize() == 1

    # The quick subscriber is drained as it goes; the slow one never is.
    for number in range(QUEUE_SIZE + 1):
        broadcaster.publish({"session_id": str(number)})
        await asyncio.sleep(0)
        quick.queue.get_nowait()

    assert slow.dropped is True
    assert slow.queue.qsize() == 1
    assert slow.queue.get_nowait() is CLOSED
    assert quick.dropped is False

    broadcaster.unsubscribe(quick)
    broadcaster.unsubscribe(slow)

    assert broadcaster.subscribers == 0


@pytest.mark.anyio
async def test_close_ends_every_stream_and_any_opened_after() -> None:
    broadcaster = Broadcaster()
    first = stream(broadcaster, broadcaster.subscribe())
    second = stream(broadcaster, broadcaster.subscribe())

    # Closing comes from the server's shutdown, off the loop's thread.
    await asyncio.to_thread(broadcaster.close)
    await asyncio.sleep(0)

    for frames in (first, second):
        assert await frames.__anext__() == frame(
            "closed", {"reason": "the service is shutting down"}
        )

        with pytest.raises(StopAsyncIteration):
            await frames.__anext__()

    late = broadcaster.subscribe()

    assert late.queue.get_nowait() is CLOSING
    assert broadcaster.subscribers == 1


@pytest.mark.anyio
async def test_the_stream_frames_deltas_and_ends_when_dropped() -> None:
    broadcaster = Broadcaster()
    subscription = broadcaster.subscribe()
    frames = stream(broadcaster, subscription, lambda d: d.get("keep", True))

    broadcaster.publish({"session_id": "a", "keep": True})
    broadcaster.publish({"session_id": "b", "keep": False})
    broadcaster.publish({"session_id": "c"})
    await asyncio.sleep(0)

    assert await frames.__anext__() == frame(
        "session", {"session_id": "a", "keep": True}
    )
    assert await frames.__anext__() == frame("session", {"session_id": "c"})

    subscription.offer(CLOSED)

    dropped = await frames.__anext__()

    assert dropped.startswith("event: dropped")

    with pytest.raises(StopAsyncIteration):
        await frames.__anext__()

    assert broadcaster.subscribers == 0


@pytest.mark.anyio
async def test_the_live_endpoint_needs_a_viewer_and_filters_by_labels(
    client: httpx.AsyncClient, key: bytes, dashboard_token: str
) -> None:
    course_a, _ = mint(key, labels={"course": "a"})
    course_b, _ = mint(key, labels={"course": "b"})
    a = shifted_to_now(load_fixture("why-a-workshop")[:3])
    b = [
        dict(e, session_id="other")
        for e in shifted_to_now(load_fixture("guided-not-documented")[:3])
    ]

    await client.post("/events", content=ndjson(a), headers=bearer(course_a))
    await client.post("/events", content=ndjson(b), headers=bearer(course_b))

    denied = await client.get("/api/live")

    assert denied.status_code == 401

    # A dashboard token is accepted outright: the viewer check must not
    # log a refusal for the api scope it does not carry.
    logs = wrapture.capture_logs("workshop_analytics.api.auth")

    with wrapture.timeline(logs):
        everything = await client.get(
            "/api/live", headers={"authorization": f"Bearer {dashboard_token}"}
        )

        assert logs.events.at_level("WARNING").count == 0

    names = sorted(row["name"] for row in everything.json()["sessions"])

    assert names == ["guided-not-documented", "why-a-workshop"]

    narrowed = await client.get(
        "/api/live",
        params={"labels": "course=b"},
        headers={"authorization": f"Bearer {dashboard_token}"},
    )

    assert [row["name"] for row in narrowed.json()["sessions"]] == [
        "guided-not-documented"
    ]

    malformed = await client.get(
        "/api/live",
        params={"labels": "course=="},
        headers={"authorization": f"Bearer {dashboard_token}"},
    )

    assert malformed.status_code == 400


@pytest.mark.anyio
async def test_the_stream_endpoint_delivers_a_posted_batch(
    app: Any, client: httpx.AsyncClient, key: bytes, dashboard_token: str
) -> None:
    token, _ = mint(key)
    body = ndjson(shifted_to_now(load_fixture("why-a-workshop")[:2]))
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/live/stream",
        "raw_path": b"/api/live/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"authorization", f"Bearer {dashboard_token}".encode())],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()

        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        await sent.put(message)

    stream_task = asyncio.create_task(app(scope, receive, send))

    try:
        start = await asyncio.wait_for(sent.get(), 5)

        assert start["type"] == "http.response.start"
        assert start["status"] == 200
        assert dict(start["headers"])[b"content-type"].startswith(b"text/event-stream")

        posted = await client.post("/events", content=body, headers=bearer(token))

        assert posted.status_code == 202

        chunk = await asyncio.wait_for(sent.get(), 5)
        text = chunk["body"].decode()

        assert text.startswith("event: session\n")

        delta = json.loads(text.split("data: ", 1)[1].strip())

        assert delta["name"] == "why-a-workshop"
        assert delta["status"] == "active"
        assert app.state.broadcaster.subscribers == 1
    finally:
        stream_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await stream_task

    assert app.state.broadcaster.subscribers == 0
