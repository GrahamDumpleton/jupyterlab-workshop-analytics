"""The in-process broadcaster and the Server-Sent Events stream.

Newly projected sessions are published as deltas to every subscriber.
Each subscriber owns one bounded asyncio queue; a subscriber that
cannot keep up is dropped rather than allowed to hold memory for
everyone. Publishing happens from the thread pool the ingest endpoint
runs in, so the hand-off to each subscriber's event loop is done with
`call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import wrapture

QUEUE_SIZE = 256

KEEPALIVE_SECONDS = 15.0

CLOSED = object()

CLOSING = object()


@dataclass(eq=False)
class Subscription:
    """One listener: its queue and the loop the queue belongs to."""

    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Any] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    dropped: bool = False

    def offer(self, delta: Any) -> None:
        """Put a delta on the queue, or mark the subscriber dropped when full."""

        if self.dropped:
            return

        try:
            self.queue.put_nowait(delta)
        except asyncio.QueueFull:
            self.dropped = True

            while not self.queue.empty():
                self.queue.get_nowait()

            self.queue.put_nowait(CLOSED)


class Broadcaster:
    """Fan session deltas out to every open stream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[Subscription] = set()
        self._closed = False

    @property
    def subscribers(self) -> int:
        """How many streams are open."""

        with self._lock:
            return len(self._subscribers)

    def subscribe(self) -> Subscription:
        """Register the calling task's loop as a listener."""

        subscription = Subscription(loop=asyncio.get_running_loop())

        with self._lock:
            self._subscribers.add(subscription)

            # A stream opened while the service is going down ends at
            # once, rather than holding the shutdown open.
            if self._closed:
                subscription.offer(CLOSING)

        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Forget a listener."""

        with self._lock:
            self._subscribers.discard(subscription)

    def close(self) -> None:
        """End every open stream, for shutdown.

        Each listener is told on its own loop, so this is safe to call
        from any thread; the stream sends a closing frame and returns,
        which lets the server finish the response and drain.
        """

        with self._lock:
            self._closed = True
            listeners = list(self._subscribers)

        for subscription in listeners:
            subscription.loop.call_soon_threadsafe(subscription.offer, CLOSING)

        wrapture.annotate(subscribers=len(listeners))

    def publish(self, delta: dict[str, Any]) -> int:
        """Hand a delta to every listener; returns how many were offered it."""

        with self._lock:
            listeners = list(self._subscribers)

        for subscription in listeners:
            if subscription.dropped:
                continue

            subscription.loop.call_soon_threadsafe(subscription.offer, delta)

        wrapture.annotate(subscribers=len(listeners))

        return len(listeners)


def frame(event: str, data: Any) -> str:
    """One SSE frame."""

    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def stream(
    broadcaster: Broadcaster,
    subscription: Subscription,
    accept: Any = None,
) -> AsyncIterator[str]:
    """Yield SSE frames for the subscription until it is dropped or closed.

    `accept` is a predicate over deltas, for a stream narrowed by a
    label selector. A keepalive comment goes out whenever nothing else
    has for a while, so proxies keep the connection open.
    """

    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    subscription.queue.get(), timeout=KEEPALIVE_SECONDS
                )
            except TimeoutError:
                yield ": keepalive\n\n"

                continue

            if item is CLOSED:
                yield frame("dropped", {"reason": "the stream fell behind"})

                return

            if item is CLOSING:
                yield frame("closed", {"reason": "the service is shutting down"})

                return

            if accept is None or accept(item):
                yield frame("session", item)
    finally:
        broadcaster.unsubscribe(subscription)
