"""`GET /api/live` and `GET /api/live/stream`: the sessions in progress."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from ..live import Broadcaster, stream
from ..projection import live_rows
from ..selectors import SelectorError, Term, matches, parse_selector
from ..store.writes import utcnow
from ..tokens import Claims
from .auth import require_viewer

router = APIRouter()


def selector_terms(request: Request) -> tuple[Term, ...]:
    """The parsed `labels` selector of a request, empty for none."""

    text = request.query_params.get("labels", "").strip()

    if not text:
        return ()

    try:
        return parse_selector(text)
    except SelectorError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/live")
async def live(
    request: Request, claims: Annotated[Claims, Depends(require_viewer)]
) -> dict[str, Any]:
    """The sessions the live view shows now, narrowed by a label selector."""

    terms = selector_terms(request)
    settings = request.app.state.settings
    now = utcnow()
    rows = await run_in_threadpool(live_rows, request.app.state.engine, now, settings)
    shown = [row for row in rows if matches(row["labels"], terms)]

    return {
        "now": now.replace(microsecond=0).isoformat() + "Z",
        "linger": settings.linger,
        "sessions": shown,
    }


@router.get("/api/live/stream")
async def live_stream(
    request: Request, claims: Annotated[Claims, Depends(require_viewer)]
) -> StreamingResponse:
    """An SSE stream of session deltas as batches arrive."""

    terms = selector_terms(request)
    broadcaster: Broadcaster = request.app.state.broadcaster
    subscription = broadcaster.subscribe()

    def accept(delta: dict[str, Any]) -> bool:
        return matches(delta.get("labels", {}), terms)

    return StreamingResponse(
        stream(broadcaster, subscription, accept),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
