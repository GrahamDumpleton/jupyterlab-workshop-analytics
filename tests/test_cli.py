"""The command line, through its entry point with the environment held."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import wrapture
from sqlalchemy import func, select

from workshop_analytics.app import create_app
from workshop_analytics.cli import (
    SHUTDOWN_GRACE_SECONDS,
    QueryStripper,
    main,
    make_server,
)
from workshop_analytics.config import Settings
from workshop_analytics.live import CLOSING, Broadcaster
from workshop_analytics.store import make_engine
from workshop_analytics.store.tables import events, sessions
from workshop_analytics.tokens import inspect, verify

from .conftest import FIXTURES


@pytest.fixture
def environment(tmp_path: Path, key_text: str) -> Iterator[dict[str, str]]:
    """The environment the commands read, held in place for the test."""

    values = {
        "TOKEN_SIGNING_KEY": key_text,
        "DATABASE_URL": f"sqlite:///{tmp_path / 'cli.db'}",
    }
    bindings = [
        wrapture.binding(os.environ, item=name).overrides(value)
        for name, value in values.items()
    ]

    for binding in bindings:
        binding.apply()

    try:
        yield values
    finally:
        for binding in bindings:
            binding.remove()


def run(*args: str) -> int:
    return main(list(args))


def test_key_generate_prints_a_usable_key(capsys: Any) -> None:
    assert run("key", "generate") == 0

    printed = capsys.readouterr().out.strip()

    assert len(base64.b64decode(printed)) == 32


def test_token_issue_without_a_key_fails_plainly(capsys: Any, tmp_path: Path) -> None:
    with wrapture.binding(os.environ, item="TOKEN_SIGNING_KEY").hides():
        status = run("token", "issue", "--name", "x", "--expires", "1d")

    assert status == 2
    assert "no signing key" in capsys.readouterr().err

    key_file = tmp_path / "key.txt"

    key_file.write_text(base64.b64encode(b"k" * 32).decode())

    with wrapture.binding(os.environ, item="TOKEN_SIGNING_KEY").hides():
        status = run(
            "token",
            "issue",
            "--name",
            "x",
            "--expires",
            "1d",
            "--key-file",
            str(key_file),
        )

    assert status == 0
    assert "jti:" in capsys.readouterr().out


def test_token_issue_requires_an_expiry(environment: dict[str, str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run("token", "issue", "--name", "x")

    assert exit_info.value.code == 2


def test_token_issue_prints_the_token_and_its_id(
    environment: dict[str, str], key: bytes, capsys: Any
) -> None:
    status = run(
        "token",
        "issue",
        "--name",
        "showcase",
        "--expires",
        "2027-01-31",
        "--label",
        "deployment=showcase",
        "--origin",
        "https://lite.example",
    )
    lines = dict(
        line.split(": ", 1) for line in capsys.readouterr().out.strip().splitlines()
    )

    assert status == 0

    claims = verify(lines["token"], key, scope="ingest")

    assert claims.jti == lines["jti"]
    assert claims.scopes == ("ingest",)
    assert claims.labels == {"deployment": "showcase"}
    assert claims.origins == ("https://lite.example",)
    assert lines["expires"] == "2027-01-31T23:59:59Z"


def test_token_issue_json_round_trips_through_inspect(
    environment: dict[str, str], capsys: Any
) -> None:
    status = run(
        "token",
        "issue",
        "--name",
        "analyst",
        "--expires",
        "90d",
        "--scope",
        "api",
        "--scope",
        "dashboard",
        "--json",
    )
    printed = json.loads(capsys.readouterr().out)

    assert status == 0
    assert printed["claims"]["scope"] == ["api", "dashboard"]

    capsys.readouterr()

    assert run("token", "inspect", printed["token"]) == 0
    assert json.loads(capsys.readouterr().out) == printed["claims"]
    assert inspect(printed["token"]).name == "analyst"


def test_a_bad_label_option_is_refused(
    environment: dict[str, str], capsys: Any
) -> None:
    status = run("token", "issue", "--name", "x", "--expires", "1d", "--label", "nokey")

    assert status == 2
    assert "key=value" in capsys.readouterr().err


def test_import_feeds_the_same_pipeline(
    environment: dict[str, str], capsys: Any
) -> None:
    fixture = str(FIXTURES / "hello-jupyterlab.jsonl")

    assert run("import", fixture, "--label", "course=hello") == 0
    assert "stored 63" in capsys.readouterr().out
    assert run("import", fixture) == 0
    assert "duplicates 63" in capsys.readouterr().out

    engine = make_engine(environment["DATABASE_URL"])

    with engine.connect() as connection:
        stored = connection.execute(select(func.count()).select_from(events)).scalar()
        row = connection.execute(select(sessions)).one()

    engine.dispose()

    assert stored == 63
    assert row.labels == {"course": "hello"}
    assert row.token_id == ""
    assert row.complete is True


def test_import_counts_under_a_token(environment: dict[str, str], capsys: Any) -> None:
    assert (
        run(
            "token",
            "issue",
            "--name",
            "offline",
            "--expires",
            "1d",
            "--label",
            "class=offline",
            "--json",
        )
        == 0
    )

    token = json.loads(capsys.readouterr().out)["token"]
    fixture = str(FIXTURES / "why-a-workshop.jsonl")

    assert run("import", fixture, "--token", token) == 0

    engine = make_engine(environment["DATABASE_URL"])

    with engine.connect() as connection:
        row = connection.execute(select(sessions)).one()

    engine.dispose()

    assert row.labels == {"class": "offline"}
    assert row.token_id == inspect(token).jti

    assert run("import", fixture, "--token", "not-a-token") == 2


def test_migrate_and_rebuild_report_what_they_did(
    environment: dict[str, str], capsys: Any
) -> None:
    assert run("migrate") == 0
    assert "migrated" in capsys.readouterr().out
    assert run("import", str(FIXTURES / "why-a-workshop.jsonl")) == 0

    capsys.readouterr()

    assert run("rebuild") == 0
    assert "rebuilt 1 session(s)" in capsys.readouterr().out


def test_import_reports_a_missing_file(
    environment: dict[str, str], capsys: Any, tmp_path: Path
) -> None:
    assert run("import", str(tmp_path / "missing.jsonl")) == 2
    assert "cannot read" in capsys.readouterr().err


@pytest.mark.anyio
async def test_serve_shutdown_ends_the_streams_before_draining(
    settings: Settings,
) -> None:
    import uvicorn

    app = create_app(settings)
    server = make_server(app, host="127.0.0.1", port=0)
    subscription = app.state.broadcaster.subscribe()

    # uvicorn's own shutdown waits for the open responses, so the
    # streams must have been told before it runs; stand in for it and
    # check the order.
    close = wrapture.binding(Broadcaster, "close")
    drained = wrapture.binding(uvicorn.Server, "shutdown").on_call.returns(None)

    with wrapture.timeline(close, drained) as tape:
        await server.shutdown()
        await asyncio.sleep(0)

        tape.assert_order(close, drained)

    assert subscription.queue.get_nowait() is CLOSING
    assert server.config.timeout_graceful_shutdown == SHUTDOWN_GRACE_SECONDS

    app.state.engine.dispose()


def test_serve_keeps_query_strings_out_of_the_access_log(settings: Settings) -> None:
    app = create_app(settings)
    make_server(app, host="127.0.0.1", port=0)
    logger = logging.getLogger("uvicorn.access")

    assert any(isinstance(f, QueryStripper) for f in logger.filters)

    # Log a request the way uvicorn does, with a token in the query, and
    # read back what a handler on that logger receives.
    seen: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = seen.append  # type: ignore[method-assign]
    logger.addHandler(handler)

    try:
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1234",
            "GET",
            "/?token=eyJhbGciOiJIUzI1NiJ9.secret.signature",
            "1.1",
            303,
        )
    finally:
        logger.removeHandler(handler)

    message = seen[0].getMessage()

    assert "secret" not in message
    assert message == '127.0.0.1:1234 - "GET / HTTP/1.1" 303'

    app.state.engine.dispose()
