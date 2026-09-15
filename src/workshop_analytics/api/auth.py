"""Authentication: bearer tokens by scope, and the dashboard's cookie.

A token arrives as `Authorization: Bearer <token>`, or as `?token=` on
the URL for a configuration that must stay URL-only. The sink answers
404 to any bad token, so the URL space reveals nothing; the read API
answers 401. The dashboard is entered by exchanging a `dashboard`
token for a signed session cookie, which is what the long-lived event
stream authenticates with, since a browser's `EventSource` cannot set
a header.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

import jwt
from fastapi import HTTPException, Request
from fastapi.responses import Response

from ..config import SESSION_COOKIE, Settings
from ..tokens import ALGORITHM, Claims, DenyList, TokenError, now, verify

SESSION_COOKIE_MAX_AGE = 12 * 3600

log = logging.getLogger(__name__)


def bearer_token(request: Request) -> str:
    """The token in the request, from the header or the query string."""

    header = request.headers.get("authorization", "")
    scheme, _, credentials = header.partition(" ")

    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip()

    return request.query_params.get("token", "").strip()


def refused(request: Request, reason: str) -> None:
    """Log why a request's token was refused, never the token itself.

    The response carries the reason too, but the sender that most needs
    it, the extension, discards the body, so the operator reads it here.
    """

    log.warning("%s %s refused: %s", request.method, request.url.path, reason)


def verified(request: Request, token: str, scope: str | tuple[str, ...]) -> Claims:
    """The claims of a token verified for a scope, or a `TokenError`."""

    state = request.app.state
    key: bytes = state.signing_key
    denied: DenyList = state.denied

    try:
        return verify(token, key, scope=scope, denied=denied)
    except TokenError as error:
        refused(request, str(error))

        raise


def require(
    scope: str, status: int
) -> Callable[[Request], Coroutine[Any, Any, Claims]]:
    """A dependency that verifies the request's token for a scope."""

    async def dependency(request: Request) -> Claims:
        token = bearer_token(request)

        if not token:
            refused(request, "no token was sent")

            raise HTTPException(status, "a token is required")

        try:
            return verified(request, token, scope)
        except TokenError as error:
            raise HTTPException(status, str(error)) from error

    return dependency


require_ingest = require("ingest", 404)

require_api = require("api", 401)


def issue_session(key: bytes, claims: Claims, settings: Settings) -> str:
    """A signed session value for the dashboard cookie, never the token."""

    moment = int(now())
    lifetime = min(int(settings.session_hours * 3600), claims.expires_at - moment)
    payload = {
        "jti": claims.jti,
        "sub": claims.name,
        "scope": ["dashboard"],
        "session": True,
        "iat": moment,
        "exp": moment + max(lifetime, 1),
    }

    return jwt.encode(payload, key, algorithm=ALGORITHM)


def read_session(request: Request) -> Claims | None:
    """The claims behind a valid session cookie, or None."""

    value = request.cookies.get(SESSION_COOKIE, "")

    if not value:
        return None

    try:
        payload = jwt.decode(
            value, request.app.state.signing_key, algorithms=[ALGORITHM]
        )
    except jwt.PyJWTError:
        return None

    if not payload.get("session"):
        return None

    jti = str(payload.get("jti", ""))

    if jti in request.app.state.denied:
        return None

    return Claims(
        jti=jti,
        name=str(payload.get("sub", "")),
        scopes=("dashboard",),
        issued_at=int(payload.get("iat", 0)),
        not_before=int(payload.get("iat", 0)),
        expires_at=int(payload.get("exp", 0)),
    )


def cookie_secure(request: Request) -> bool:
    """Whether the cookie should carry the Secure flag for this request."""

    setting: bool | None = request.app.state.settings.cookie_secure

    if setting is not None:
        return setting

    forwarded = request.headers.get("x-forwarded-proto", "")

    return request.url.scheme == "https" or forwarded.lower() == "https"


def set_session_cookie(response: Response, request: Request, value: str) -> None:
    """Attach the session cookie with the flags the dashboard wants."""

    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        secure=cookie_secure(request),
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    """Remove the session cookie."""

    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=cookie_secure(request),
        samesite="strict",
    )


async def require_viewer(request: Request) -> Claims:
    """Claims for a read of the live view: a session cookie or a bearer.

    A `dashboard` or `api` token in the header is accepted too, so a
    script can read the live view without the login dance.
    """

    session = read_session(request)

    if session is not None:
        return session

    token = bearer_token(request)

    if not token:
        refused(request, "no session and no token")

        raise HTTPException(401, "sign in to the dashboard or send a token")

    try:
        return verified(request, token, ("api", "dashboard"))
    except TokenError as error:
        raise HTTPException(401, str(error)) from error
