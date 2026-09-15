"""The read-only SQL tool: its guards, its limits and its route."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from workshop_analytics.config import Settings
from workshop_analytics.sql import (
    SqlDisabled,
    SqlError,
    SqlTimeout,
    SqlTool,
    check_statement,
    describe_sql,
    plain_value,
)

from .conftest import Seeded


def test_only_a_single_select_is_accepted() -> None:
    assert check_statement("select 1;") == "select 1"
    assert check_statement("  -- a comment\n  WITH x AS (SELECT 1) SELECT * FROM x")
    assert check_statement("/* why */ explain select 1")

    for bad, reason in {
        "": "empty",
        "delete from events": "not DELETE",
        "select 1; select 2": "semicolon",
        "pragma table_info(events)": "not PRAGMA",
        "insert into events values (1)": "not INSERT",
    }.items():
        with pytest.raises(SqlError, match=reason):
            check_statement(bad)


def test_a_statement_runs_with_named_parameters(app: Any, seeded: Seeded) -> None:
    tool: SqlTool = app.state.sql
    result = tool.run(
        "select name, count(*) as n from sessions where collection = :c "
        "group by name order by n desc",
        {"c": ""},
    )

    assert result.columns == ["name", "n"]
    assert result.rows == [["hello-jupyterlab", 7], ["why-a-workshop", 1]]
    assert result.row_count == 2
    assert result.truncated is False
    assert result.elapsed_ms >= 0


def test_rows_are_capped_and_truncation_is_reported(app: Any, seeded: Seeded) -> None:
    tool: SqlTool = app.state.sql
    page = tool.run("select session_id from sessions order by session_id", limit=2)

    assert page.row_count == 2
    assert page.truncated is True

    whole = tool.run("select session_id from sessions")

    assert whole.row_count == 11
    assert whole.truncated is False

    capped = SqlTool(
        Settings(database_url=app.state.settings.database_url, sql_max_rows=4)
    )

    assert capped.run("select session_id from sessions", limit=100).row_count == 4


def test_the_engine_cannot_write(app: Any, seeded: Seeded) -> None:
    tool: SqlTool = app.state.sql

    # A WITH that writes gets past the first word check and is stopped by
    # the read-only engine itself.
    with pytest.raises(SqlError, match="readonly|read-only|query_only"):
        tool.run("with x as (select 1) delete from events")

    assert tool.run("select count(*) from events").rows[0][0] > 0


def test_a_slow_statement_is_stopped_at_the_deadline(app: Any, seeded: Seeded) -> None:
    quick = SqlTool(
        Settings(database_url=app.state.settings.database_url, sql_timeout=0.2)
    )

    with pytest.raises(SqlTimeout, match="stopped after 0.2 seconds"):
        quick.run(
            "with recursive r(n) as (select 1 union all select n + 1 from r) "
            "select count(*) from r"
        )

    # The connection is usable again afterwards.
    assert quick.run("select 1").rows == [[1]]


def test_the_tool_can_be_off_or_unavailable(tmp_path: Any) -> None:
    off = SqlTool(
        Settings(database_url=f"sqlite:///{tmp_path / 'a.db'}", sql_tool=False)
    )

    assert off.enabled is False

    with pytest.raises(SqlDisabled, match="SQL_TOOL"):
        off.run("select 1")

    memory = SqlTool(Settings(database_url="sqlite://"))

    assert memory.enabled is False
    assert "file-backed" in memory.reason

    described = describe_sql(Settings(database_url="sqlite://"), "sqlite")

    assert described["enabled"] is False
    assert "events" in described["tables"]
    assert "sessions" in described["tables"]
    assert described["tables"]["events"]["columns"]["payload"] == "JSON"


def test_cells_come_out_as_json_can_carry_them(app: Any, seeded: Seeded) -> None:
    tool: SqlTool = app.state.sql
    result = tool.run(
        "select started_at, sessions.labels, json_extract(payload, '$.kind') "
        "as kind from sessions join events using (session_id) limit 1"
    )

    assert result.rows[0][0].startswith("2026-09-15 03:13:55")
    assert result.rows[0][2] == "workshop-start"
    assert plain_value(b"abc") == "<3 bytes>"


@pytest.mark.anyio
async def test_the_route_answers_and_refuses_like_the_tool(
    client: httpx.AsyncClient, seeded: Seeded, api_token: str, dashboard_token: str
) -> None:
    headers = {"authorization": f"Bearer {api_token}"}
    good = await client.post(
        "/api/sql",
        json={
            "sql": "select count(*) as n from sessions where name = :n",
            "params": {"n": "hello-jupyterlab"},
        },
        headers=headers,
    )

    assert good.status_code == 200
    assert good.json()["rows"] == [[7]]

    bad = await client.post(
        "/api/sql", json={"sql": "drop table events"}, headers=headers
    )

    assert bad.status_code == 400
    assert "DROP" in bad.json()["detail"]

    broken = await client.post(
        "/api/sql", json={"sql": "select nothing from nowhere"}, headers=headers
    )

    assert broken.status_code == 400

    refused = await client.post(
        "/api/sql",
        json={"sql": "select 1"},
        headers={"authorization": f"Bearer {dashboard_token}"},
    )

    assert refused.status_code == 401


@pytest.mark.anyio
async def test_a_deployment_can_turn_the_route_off(
    settings: Settings, key: bytes
) -> None:
    from workshop_analytics.app import create_app

    from .conftest import mint

    app = create_app(Settings(**{**settings.__dict__, "sql_tool": False}))
    token = mint(key, ("api",))[0]
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.post(
            "/api/sql",
            json={"sql": "select 1"},
            headers={"authorization": f"Bearer {token}"},
        )
        described = await c.get(
            "/api/describe", headers={"authorization": f"Bearer {token}"}
        )

    app.state.engine.dispose()

    assert response.status_code == 404
    assert described.json()["sql"]["enabled"] is False
