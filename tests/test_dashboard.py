"""The dashboard page and its two ways in."""

from __future__ import annotations

import httpx
import pytest

from workshop_analytics.app import DASHBOARD_DIR
from workshop_analytics.config import SESSION_COOKIE


def with_cookie(value: str) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={value}"}


pytestmark = pytest.mark.anyio


async def test_without_a_session_the_page_is_the_login_form(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert 'name="token"' in response.text
    assert "Workshops in progress" not in response.text


async def test_the_login_form_exchanges_a_dashboard_token_for_a_cookie(
    client: httpx.AsyncClient, dashboard_token: str, api_token: str
) -> None:
    refused = await client.post("/login", data={"token": api_token})

    assert refused.status_code == 401
    assert "scope" in refused.text

    accepted = await client.post("/login", data={"token": dashboard_token})

    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/"

    cookie = accepted.cookies.get(SESSION_COOKIE)

    assert cookie and dashboard_token not in cookie
    assert "httponly" in accepted.headers["set-cookie"].lower()
    assert "samesite=strict" in accepted.headers["set-cookie"].lower()

    page = await client.get("/", headers=with_cookie(cookie))

    assert page.status_code == 200
    assert "Workshops in progress" in page.text
    assert "supervisor" in page.text

    live = await client.get("/api/live", headers=with_cookie(cookie))

    assert live.status_code == 200


async def test_a_one_time_token_on_the_url_is_redirected_away(
    client: httpx.AsyncClient, dashboard_token: str
) -> None:
    response = await client.get(
        "/", params={"token": dashboard_token, "labels": "course=a"}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?labels=course=a"
    assert response.cookies.get(SESSION_COOKIE)

    bad = await client.get("/", params={"token": "nope"})

    assert bad.status_code == 401
    assert 'name="token"' in bad.text


async def test_logout_clears_the_cookie(
    client: httpx.AsyncClient, dashboard_token: str
) -> None:
    signed_in = await client.post("/login", data={"token": dashboard_token})
    cookie = signed_in.cookies.get(SESSION_COOKIE)

    assert cookie

    response = await client.post("/logout", headers=with_cookie(cookie))

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert f"{SESSION_COOKIE}=" in response.headers["set-cookie"]
    assert "max-age=0" in response.headers["set-cookie"].lower()


async def test_the_cookie_is_secure_behind_https(
    client: httpx.AsyncClient, dashboard_token: str
) -> None:
    plain = await client.post("/login", data={"token": dashboard_token})

    assert "secure" not in plain.headers["set-cookie"].lower()

    forwarded = await client.post(
        "/login",
        data={"token": dashboard_token},
        headers={"x-forwarded-proto": "https"},
    )

    assert "secure" in forwarded.headers["set-cookie"].lower()


async def test_static_assets_and_health_need_no_login(
    client: httpx.AsyncClient,
) -> None:
    script = await client.get("/static/live.js")
    health = await client.get("/healthz")

    assert script.status_code == 200
    assert health.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_the_pages_link_their_assets_by_version(
    client: httpx.AsyncClient, dashboard_token: str
) -> None:
    from workshop_analytics.app import assets_version

    version = assets_version(DASHBOARD_DIR / "static")
    login = await client.get("/login")

    assert f"/static/live.css?v={version}" in login.text

    entered = await client.get(f"/?token={dashboard_token}", follow_redirects=True)

    assert f"/static/live.js?v={version}" in entered.text
    assert (await client.get(f"/static/live.js?v={version}")).status_code == 200
