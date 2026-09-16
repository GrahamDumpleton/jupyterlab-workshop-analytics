"""The dashboard's pages and its two ways in."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any
from urllib.parse import quote_plus

import httpx
import pytest

from workshop_analytics import queries
from workshop_analytics.app import DASHBOARD_DIR
from workshop_analytics.config import SESSION_COOKIE
from workshop_analytics.queries import Filters
from workshop_analytics.store.writes import utcnow

from .conftest import COLLECTION, Seeded, variant, without_inventory


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
    assert "<title>Workshops</title>" in page.text
    assert "supervisor" in page.text
    assert 'href="/live"' in page.text and 'href="/sessions"' in page.text

    live_page = await client.get("/live", headers=with_cookie(cookie))

    assert live_page.status_code == 200
    assert "Workshops in progress" in live_page.text

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
    value = entered.history[0].cookies.get(SESSION_COOKIE)

    assert value

    live = await client.get("/live", headers=with_cookie(value))

    assert f"/static/live.css?v={version}" in entered.text
    assert f"/static/live.js?v={version}" in live.text
    assert (await client.get(f"/static/live.js?v={version}")).status_code == 200


async def test_the_session_page_shows_the_timeline_to_a_signed_in_viewer(
    app: Any, client: httpx.AsyncClient, dashboard_token: str, seeded: Seeded
) -> None:
    signed_out = await client.get(f"/sessions/{seeded.gapped}")

    assert signed_out.status_code == 401
    assert 'name="token"' in signed_out.text

    signed_in = await client.post("/login", data={"token": dashboard_token})
    cookie = signed_in.cookies.get(SESSION_COOKIE)
    page = await client.get(f"/sessions/{seeded.gapped}", headers=with_cookie(cookie))

    assert page.status_code == 200
    assert "hello-jupyterlab" in page.text
    assert "workshop-start" in page.text
    assert "missing events 10 to 12" in page.text
    assert "course=advanced" in page.text
    assert f"/sessions/{seeded.part1}" not in page.text

    chained = await client.get(f"/sessions/{seeded.part2}", headers=with_cookie(cookie))

    assert f'href="/sessions/{seeded.part1}"' in chained.text
    assert "workshop-resume" in chained.text

    # The page table says how many of each page's directives ran, with
    # the ones never run named, and unknown for a session whose
    # extension sent no inventory.
    complete = await client.get(
        f"/sessions/{seeded.complete}", headers=with_cookie(cookie)
    )

    assert "5 of 7 run" in complete.text
    assert "Never run: 01-welcome-3 (" in complete.text
    assert "unknown" not in complete.text

    app.state.ingest.accept(
        without_inventory(seeded.hello, "hello-older", instance_id="inst-old"), None
    )

    older = await client.get("/sessions/hello-older", headers=with_cookie(cookie))

    # Seven listed pages and the one entered outside the list.
    assert older.text.count(">unknown<") == 8

    missing = await client.get("/sessions/nothing", headers=with_cookie(cookie))

    assert missing.status_code == 404
    assert "There is no session" in missing.text


@pytest.fixture
async def cookie(client: httpx.AsyncClient, dashboard_token: str) -> dict[str, str]:
    """The headers of a signed-in supervisor."""

    signed_in = await client.post("/login", data={"token": dashboard_token})
    value = signed_in.cookies.get(SESSION_COOKIE)

    assert value

    return with_cookie(value)


async def test_every_page_needs_a_session(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    for path in (
        "/live",
        "/sessions",
        "/workshops/hello-jupyterlab",
        "/downloads/sessions.csv",
        "/downloads/events.ndjson",
    ):
        response = await client.get(path)

        assert response.status_code == 401, path
        assert 'name="token"' in response.text, path


async def test_the_history_lists_sessions_newest_first_and_filters_them(
    client: httpx.AsyncClient, cookie: dict[str, str], seeded: Seeded
) -> None:
    page = await client.get("/sessions", headers=cookie)

    assert page.status_code == 200
    assert "11 match" in page.text
    assert page.text.index(f"/sessions/{seeded.live}") < page.text.index(
        f"/sessions/{seeded.complete}"
    )
    assert 'href="/workshops/hello-jupyterlab?collection="' in page.text
    assert "course=advanced" in page.text

    # The form offers the values that occur.
    assert '<option value="hello-jupyterlab"' in page.text
    assert f'<option value="{COLLECTION}"' in page.text
    assert '<option value="binder"' in page.text or '<option value="local"' in page.text

    narrowed = await client.get(
        "/sessions",
        params={"name": "hello-jupyterlab", "labels": "course=advanced"},
        headers=cookie,
    )

    assert "1 match" in narrowed.text
    assert f"/sessions/{seeded.gapped}" in narrowed.text
    assert f"/sessions/{seeded.complete}" not in narrowed.text
    assert 'value="course=advanced"' in narrowed.text

    by_status = await client.get(
        "/sessions",
        params=[("status", "finished"), ("status", "lost"), ("collection", "none")],
        headers=cookie,
    )

    assert 'value="finished" checked' in by_status.text
    assert 'value="lost" checked' in by_status.text
    assert "status-abandoned" not in by_status.text

    bad = await client.get("/sessions", params={"status": "asleep"}, headers=cookie)

    assert bad.status_code == 200
    assert "unknown status asleep" in bad.text
    assert "0 match" not in bad.text


async def test_the_history_pages_with_a_cursor(
    client: httpx.AsyncClient, cookie: dict[str, str], seeded: Seeded
) -> None:
    first = await client.get("/sessions", params={"limit": 4}, headers=cookie)
    older = re.search(r'href="([^"]*cursor=[^"]+)" rel="next"', first.text)

    assert older is not None
    assert "showing 4" in first.text

    second = await client.get(older.group(1).replace("&amp;", "&"), headers=cookie)
    ids_first = set(re.findall(r'href="/sessions/([^"]+)" title', first.text))
    ids_second = set(re.findall(r'href="/sessions/([^"]+)" title', second.text))

    assert len(ids_first) == 4 and len(ids_second) == 4
    assert not ids_first & ids_second
    assert ">Newest<" in second.text


async def test_the_downloads_serve_the_selection(
    client: httpx.AsyncClient, cookie: dict[str, str], seeded: Seeded
) -> None:
    sheet = await client.get(
        "/downloads/sessions.csv", params={"name": "hello-jupyterlab"}, headers=cookie
    )

    assert sheet.status_code == 200
    assert sheet.headers["content-type"].startswith("text/csv")
    assert "sessions.csv" in sheet.headers["content-disposition"]

    rows = list(csv.DictReader(io.StringIO(sheet.text)))

    assert len(rows) == 7
    assert rows[0]["session_id"] == seeded.live
    assert {row["labels"] for row in rows} >= {"course=intro", "course=advanced"}
    assert rows[-1]["status"] in {"finished", "lost", "resumed", "abandoned"}

    lines = await client.get(
        "/downloads/events.ndjson",
        params={"name": "hello-jupyterlab", "labels": "course=advanced"},
        headers=cookie,
    )

    assert lines.status_code == 200
    assert lines.headers["content-type"].startswith("application/x-ndjson")

    events = [json.loads(line) for line in lines.text.splitlines()]

    assert len(events) == len(seeded.hello) - 3
    assert {event["session_id"] for event in events} == {seeded.gapped}
    assert [event["seq"] for event in events][:3] == [1, 2, 3]

    bad = await client.get(
        "/downloads/sessions.csv", params={"since": "yesterday"}, headers=cookie
    )

    assert bad.status_code == 400


async def test_the_workshop_page_shows_the_reports(
    app: Any, client: httpx.AsyncClient, cookie: dict[str, str], seeded: Seeded
) -> None:
    page = await client.get(
        "/workshops/hello-jupyterlab", params={"period": "all"}, headers=cookie
    )

    assert page.status_code == 200
    assert "<title>hello-jupyterlab</title>" in page.text
    assert 'id="funnel"' in page.text and 'id="checks"' in page.text

    # The stacked bars count what trends counts: one rect per outcome
    # that occurred in a bucket.
    with app.state.engine.connect() as connection:
        trends = queries.trends(
            connection, Filters(name="hello-jupyterlab"), utcnow(), app.state.settings
        )

    for key in ("finished", "lost", "in_progress"):
        expected = sum(1 for b in trends.buckets if getattr(b.outcomes, key))

        assert page.text.count(f'class="seg seg-{key}"') == expected, key

    assert sum(b.outcomes.finished for b in trends.buckets) == 2
    assert "finished 2" in page.text

    # The never-run list names the three directives the recording skipped.
    for directive in ("01-welcome-3", "01-welcome-5", "05-variables-2"):
        assert directive in page.text

    assert "Needs attention" in page.text
    assert "01-welcome-3" in page.text

    # Grouping draws one bar per value and lists the groups.
    grouped = await client.get(
        "/workshops/hello-jupyterlab",
        params={"period": "all", "group_by": "user"},
        headers=cookie,
    )

    assert 'class="seg seg-group-0"' in grouped.text
    assert "alice" in grouped.text and "bob" in grouped.text

    # A period nothing falls in still renders, with empty charts.
    quiet = await client.get(
        "/workshops/hello-jupyterlab",
        params={"since": "2020-01-01", "until": "2020-02-01"},
        headers=cookie,
    )

    assert quiet.status_code == 200
    assert "nothing in this period" in quiet.text


async def test_the_workshop_page_refuses_what_the_api_refuses(
    client: httpx.AsyncClient, cookie: dict[str, str], seeded: Seeded
) -> None:
    ambiguous = await client.get("/workshops/why-a-workshop", headers=cookie)

    assert ambiguous.status_code == 409
    assert "more than one collection" in ambiguous.text
    assert 'href="/workshops/why-a-workshop?collection="' in ambiguous.text
    assert f'href="/workshops/why-a-workshop?collection={quote_plus(COLLECTION)}"' in (
        ambiguous.text
    )

    chosen = await client.get(
        "/workshops/why-a-workshop", params={"collection": COLLECTION}, headers=cookie
    )

    assert chosen.status_code == 200

    missing = await client.get("/workshops/nothing", headers=cookie)

    assert missing.status_code == 404

    bad = await client.get(
        "/workshops/hello-jupyterlab", params={"labels": "=="}, headers=cookie
    )

    assert bad.status_code == 400


async def test_the_overview_lists_workshops_and_collections(
    client: httpx.AsyncClient, cookie: dict[str, str], seeded: Seeded
) -> None:
    page = await client.get("/", params={"period": "all"}, headers=cookie)

    assert page.status_code == 200
    assert 'href="/workshops/hello-jupyterlab?period=all&amp;collection="' in page.text
    assert page.text.count('class="sparkline"') == 4
    assert "8 journeys" in page.text
    assert 'class="seg seg-finished"' in page.text

    # The collection's funnel names its workshops in the order taken.
    assert "collection-card" in page.text
    assert page.text.index(
        "why-a-workshop</a>", page.text.index("collection-card")
    ) < page.text.index("guided-not-documented</a>", page.text.index("collection-card"))

    # The period narrows the listing; a labels selector is kept in the tabs.
    quiet = await client.get(
        "/",
        params={"since": "2020-01-01", "until": "2020-02-01", "labels": "course=intro"},
        headers=cookie,
    )

    assert "No workshop has sessions in this period" in quiet.text
    assert 'href="/sessions?labels=course%3Dintro"' in quiet.text

    bad = await client.get("/", params={"since": "never"}, headers=cookie)

    assert bad.status_code == 400


async def test_the_pages_show_a_collection_by_its_title(
    app: Any, client: httpx.AsyncClient, cookie: dict[str, str], seeded: Seeded
) -> None:
    app.state.ingest.accept(
        variant(
            seeded.why,
            "why-a",
            instance_id="inst-a",
            collection="collection.json",
            collection_id="example.org/a",
            collection_title="Collection A",
        ),
        None,
    )

    history = await client.get(
        "/sessions", params={"collection": "example.org/a"}, headers=cookie
    )

    assert "1 match" in history.text
    assert 'title="example.org/a">Collection A</span>' in history.text
    assert (
        '<option value="example.org/a" selected>Collection A</option>' in history.text
    )

    overview = await client.get("/", params={"period": "all"}, headers=cookie)

    assert 'title="example.org/a">Collection A</td>' in overview.text
    assert 'title="example.org/a">Collection A</span>' in overview.text

    page = await client.get(
        "/workshops/why-a-workshop",
        params={"collection": "example.org/a", "period": "all"},
        headers=cookie,
    )

    assert page.status_code == 200
    assert 'title="example.org/a">Collection A</span>' in page.text
