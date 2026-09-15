"""`POST /events`: the endpoint the extension's forwarder talks to."""

from __future__ import annotations

from typing import Any

import wrapture
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..ingest import BatchError, Ingest, RateLimiter, parse_body
from ..tokens import Claims, TokenError
from .auth import bearer_token, refused, verified

router = APIRouter()

CORS_METHODS = "POST, OPTIONS"

CORS_HEADERS = "Authorization, Content-Type"


def allowed_origin(request: Request, claims: Claims | None) -> str:
    """The origin to allow in a cross-origin answer, or empty for none.

    The token's own origins are consulted first, then the deployment's
    `ALLOWED_ORIGINS`. A preflight carries no header, so a JupyterLite
    site either sends the token as `?token=` or is listed against the
    deployment.
    """

    origin = request.headers.get("origin", "").rstrip("/")

    if not origin:
        return ""

    candidates: list[str] = list(request.app.state.settings.allowed_origins)

    if claims is not None:
        candidates.extend(claims.origins)

    for candidate in candidates:
        if candidate == "*" or candidate.rstrip("/") == origin:
            return origin

    return ""


def cors_headers(origin: str) -> dict[str, str]:
    """The response headers for an allowed origin."""

    if not origin:
        return {}

    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": CORS_METHODS,
        "Access-Control-Allow-Headers": CORS_HEADERS,
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


@router.options("/events", include_in_schema=False)
async def preflight(request: Request) -> Response:
    """Answer a browser's preflight for an origin the token or deployment allows."""

    claims: Claims | None = None
    token = request.query_params.get("token", "").strip()

    if token:
        try:
            claims = verified(request, token, "ingest")
        except TokenError:
            claims = None

    origin = allowed_origin(request, claims)

    if not origin:
        raise HTTPException(404)

    return Response(status_code=204, headers=cors_headers(origin))


async def read_body(request: Request, limit: int) -> bytes:
    """The request body, refused once it grows past the cap."""

    declared = request.headers.get("content-length", "")

    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"the body may be at most {limit} bytes")

    chunks: list[bytes] = []
    size = 0

    async for chunk in request.stream():
        size += len(chunk)

        if size > limit:
            raise HTTPException(413, f"the body may be at most {limit} bytes")

        chunks.append(chunk)

    return b"".join(chunks)


@router.post("/events", status_code=202)
async def receive(request: Request) -> Response:
    """Accept a batch of events under a verified `ingest` token."""

    token = bearer_token(request)

    if not token:
        refused(request, "no token was sent")

        raise HTTPException(404)

    try:
        claims = verified(request, token, "ingest")
    except TokenError as error:
        raise HTTPException(404, str(error)) from error

    limiter: RateLimiter = request.app.state.rate_limiter

    if not limiter.allow(claims.jti):
        raise HTTPException(429, "too many batches; try again in a minute")

    settings = request.app.state.settings
    body = await read_body(request, settings.max_body_bytes)

    try:
        items = parse_body(body, request.headers.get("content-type", ""))
    except BatchError as error:
        wrapture.current_event(kind="request").note_exception(error)

        raise HTTPException(400, str(error)) from error

    ingest: Ingest = request.app.state.ingest
    result = await run_in_threadpool(ingest.accept, items, claims)
    payload: dict[str, Any] = result.as_dict()
    headers = cors_headers(allowed_origin(request, claims))

    return JSONResponse(payload, status_code=202, headers=headers)
