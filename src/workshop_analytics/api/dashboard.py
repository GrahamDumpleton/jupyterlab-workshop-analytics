"""The live dashboard page, its login and its logout."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..tokens import TokenError
from .auth import (
    clear_session_cookie,
    issue_session,
    read_session,
    set_session_cookie,
    verified,
)

router = APIRouter()


def render(
    request: Request, template: str, status_code: int = 200, **context: Any
) -> HTMLResponse:
    """Render a dashboard template."""

    templates = request.app.state.templates
    assets = request.app.state.assets_version
    response: HTMLResponse = templates.TemplateResponse(
        request,
        template,
        {"request": request, "assets": assets, **context},
        status_code=status_code,
    )

    return response


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> Response:
    """The live view, or the login page when there is no session.

    A one-time `?token=` on the URL is exchanged for the cookie and
    redirected away, so the token leaves the address bar and the
    history entry.
    """

    token = request.query_params.get("token", "").strip()

    if token:
        try:
            claims = verified(request, token, "dashboard")
        except TokenError as error:
            return render(request, "login.html", error=str(error), status_code=401)

        params = dict(request.query_params)
        params.pop("token", None)
        target = request.url.path

        if params:
            target += "?" + "&".join(f"{k}={v}" for k, v in params.items())

        response: Response = RedirectResponse(target, status_code=303)
        value = issue_session(
            request.app.state.signing_key, claims, request.app.state.settings
        )

        set_session_cookie(response, request, value)

        return response

    session = read_session(request)

    if session is None:
        return render(request, "login.html", error="")

    return render(
        request,
        "live.html",
        viewer=session.name,
        labels=request.query_params.get("labels", ""),
        linger=request.app.state.settings.linger,
    )


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request) -> Response:
    """The one-field login form."""

    return render(request, "login.html", error="")


@router.post("/login", include_in_schema=False)
async def login(request: Request, token: str = Form("")) -> Response:
    """Exchange a pasted dashboard token for the session cookie."""

    try:
        claims = verified(request, token.strip(), "dashboard")
    except TokenError as error:
        return render(request, "login.html", error=str(error), status_code=401)

    response: Response = RedirectResponse("/", status_code=303)
    value = issue_session(
        request.app.state.signing_key, claims, request.app.state.settings
    )

    set_session_cookie(response, request, value)

    return response


@router.post("/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    """Clear the session cookie."""

    response: Response = RedirectResponse("/login", status_code=303)

    clear_session_cookie(response, request)

    return response
