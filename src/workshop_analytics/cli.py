"""The `workshop-analytics` command.

`serve` runs the service; `migrate` and `rebuild` maintain the store;
`import` feeds a file through the sink's own pipeline; `key generate`,
`token issue` and `token inspect` manage the credentials. The same
console script runs in the container, so every command works through
`kubectl exec` in a deployed pod, where the key and the database are
already in the environment.

The module imports none of the application until `serve` has applied
the wrapture configuration, so the observe entries in `wrapture.toml`
land before the modules they name are imported.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from .config import Settings
from .labels import parse_label
from .tokens import (
    DEFAULT_SCOPES,
    SCOPES,
    TokenError,
    expiry_text,
    generate_key,
    inspect,
    issue,
    now,
    parse_expiry,
    read_key,
)

PROGRAM = "workshop-analytics"


class CommandError(Exception):
    """A command that cannot proceed, with the message to print."""


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for every subcommand."""

    parser = argparse.ArgumentParser(
        prog=PROGRAM, description="The jupyterlab-workshop analytics service."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the service")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on")
    serve.add_argument("--port", type=int, default=8080, help="port to listen on")
    serve.add_argument(
        "--no-migrate",
        dest="migrate",
        action="store_false",
        help="do not bring the database up to date before serving",
    )

    commands.add_parser("migrate", help="bring the database up to the latest schema")
    commands.add_parser("rebuild", help="recompute every session from the events")

    dump = commands.add_parser(
        "export", help="write the stored events as JSON lines import reads back"
    )
    dump.add_argument(
        "--output", type=Path, help="the file to write (default: standard output)"
    )
    dump.add_argument(
        "--selector", default="", help="a label selector, as the API takes it"
    )
    dump.add_argument("--name", default="", help="one workshop by manifest name")
    dump.add_argument(
        "--collection",
        default=None,
        help="one collection; an empty string for sessions outside any",
    )
    dump.add_argument("--since", default="", help="events from this moment, UTC")
    dump.add_argument("--until", default="", help="events before this moment, UTC")

    load = commands.add_parser(
        "import", help="feed an events.jsonl file through the sink's pipeline"
    )
    load.add_argument("path", type=Path, help="the file to import")
    load.add_argument(
        "--token", default="", help="a signed ingest token to count the file under"
    )
    load.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a trusted label for this import; may be repeated",
    )

    key = commands.add_parser("key", help="manage the signing key")
    key_commands = key.add_subparsers(dest="key_command", required=True)
    key_commands.add_parser("generate", help="print a fresh random signing key")

    token = commands.add_parser("token", help="issue and inspect tokens")
    token_commands = token.add_subparsers(dest="token_command", required=True)

    mint = token_commands.add_parser("issue", help="sign a new token")
    mint.add_argument("--name", required=True, help="who the token is for")
    mint.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a label the token binds to every event; may be repeated",
    )
    mint.add_argument(
        "--origin",
        action="append",
        default=[],
        help="a browser origin the token may post from; may be repeated",
    )
    mint.add_argument(
        "--expires",
        required=True,
        help="when the token stops working: a date (2027-01-31) or a duration (90d)",
    )
    mint.add_argument(
        "--scope",
        action="append",
        default=[],
        choices=SCOPES,
        help=f"what the token may do; may be repeated (default: {DEFAULT_SCOPES[0]})",
    )
    mint.add_argument(
        "--key-file", type=Path, help="read the signing key from this file"
    )
    mint.add_argument(
        "--json", action="store_true", help="print the token and its claims as JSON"
    )

    show = token_commands.add_parser(
        "inspect", help="decode a token's claims without the key"
    )
    show.add_argument("token", help="the token, or - to read it from standard input")

    return parser


def _settings() -> Settings:
    return Settings.from_environment()


def _key(key_file: Path | None) -> bytes:
    try:
        return read_key(os.environ.get("TOKEN_SIGNING_KEY", ""), key_file)
    except TokenError as error:
        raise CommandError(str(error)) from error


def _labels(options: Sequence[str]) -> dict[str, str]:
    labels: dict[str, str] = {}

    for option in options:
        try:
            key, value = parse_label(option)
        except ValueError as error:
            raise CommandError(str(error)) from error

        labels[key] = value

    return labels


def command_serve(args: argparse.Namespace) -> int:
    """Apply the wrapture configuration, then run uvicorn."""

    import wrapture

    if "WRAPTURE_CONFIG" in os.environ:
        wrapture.load_config(os.environ["WRAPTURE_CONFIG"]).apply()

    from .app import create_app

    settings = _settings()

    try:
        app = create_app(settings, run_migrations=args.migrate)
    except TokenError as error:
        raise CommandError(str(error)) from error

    make_server(app, host=args.host, port=args.port).run()

    return 0


class QueryStripper(logging.Filter):
    """Keep the query string out of uvicorn's access log.

    uvicorn logs every request as its method, its path and its query
    string, and two of the service's routes take a token in the query:
    the dashboard's `?token=` sign-in and the `/events?token=` form a
    JupyterLite site uses. A token must never reach a log, so the line
    keeps the path and drops everything after the question mark.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args

        # The access record's arguments are the client, the method, the
        # path with its query string, the HTTP version and the status.
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].partition("?")[0], *args[3:])

        return True


STRIP_QUERY = QueryStripper()

SHUTDOWN_GRACE_SECONDS = 5


def make_server(app: Any, *, host: str, port: int) -> Any:
    """A uvicorn server for the app whose shutdown ends the live streams.

    uvicorn waits for every open response to finish before it stops,
    and a dashboard's event stream never finishes on its own, so a
    plain server hangs on Ctrl-C until a second one forces it. The
    server returned here closes the broadcaster as the first step of
    its shutdown, which ends each stream with a closing frame, and
    cuts whatever is still open after a short grace.
    """

    import uvicorn

    class Server(uvicorn.Server):
        async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
            app.state.broadcaster.close()

            await super().shutdown(sockets)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
    )

    # uvicorn's logging is configured by the Config above; the filter
    # goes on afterwards so its access lines never carry a query string.
    logging.getLogger("uvicorn.access").addFilter(STRIP_QUERY)

    return Server(config)


def command_migrate(args: argparse.Namespace) -> int:
    """Bring the database up to the latest migration."""

    from .store import make_engine, migrate

    settings = _settings()
    engine = make_engine(settings.database_url)

    migrate(engine)
    engine.dispose()

    print(f"migrated {settings.database_url}")

    return 0


def command_rebuild(args: argparse.Namespace) -> int:
    """Recompute the sessions projection from the events table."""

    from .projection import rebuild
    from .store import make_engine, migrate

    settings = _settings()
    engine = make_engine(settings.database_url)

    migrate(engine)

    count = rebuild(engine)

    engine.dispose()

    print(f"rebuilt {count} session(s)")

    return 0


def command_import(args: argparse.Namespace) -> int:
    """Feed a file of events through the same pipeline as the sink."""

    from .ingest import Ingest, parse_body, unwrap
    from .live import Broadcaster
    from .schema import EventValidator
    from .store import make_engine, migrate
    from .tokens import verify

    settings = _settings()
    labels = _labels(args.label)
    claims = None

    if args.token:
        try:
            claims = verify(args.token, _key(None), scope="ingest")
        except TokenError as error:
            raise CommandError(str(error)) from error

    try:
        body = args.path.read_bytes()
    except OSError as error:
        raise CommandError(f"cannot read {args.path}: {error}") from error

    content_type = "application/json" if args.path.suffix == ".json" else ""
    items = unwrap(parse_body(body, content_type))
    engine = make_engine(settings.database_url)

    migrate(engine)

    ingest = Ingest(
        engine=engine,
        settings=settings,
        validator=EventValidator(),
        broadcaster=Broadcaster(),
    )
    result = ingest.accept(items, claims, labels)

    engine.dispose()

    print(
        f"received {result.received}, stored {result.stored}, "
        f"duplicates {result.duplicates}, rejected {result.rejected}"
    )

    for problem in result.problems[:5]:
        print(f"  {problem}")

    return 0 if result.stored or not result.rejected else 1


def command_export(args: argparse.Namespace) -> int:
    """Write the stored events as JSON lines that `import` reads back whole.

    Each line holds the event as it was received beside what the store
    added: the labels it was kept with, the token id and the receipt
    time. The events table is the whole store, sessions being derived
    from it, so the file moves a deployment or backs one up.
    """

    from sqlalchemy import select

    from .queries import parse_moment
    from .selectors import SelectorError, matches, parse_selector
    from .store import make_engine, migrate
    from .store.tables import events

    settings = _settings()

    try:
        terms = parse_selector(args.selector)
        since = parse_moment(args.since) if args.since else None
        until = parse_moment(args.until) if args.until else None
    except (SelectorError, ValueError) as error:
        raise CommandError(str(error)) from error

    statement = select(events).order_by(events.c.id)

    if args.name:
        statement = statement.where(events.c.name == args.name)

    if args.collection is not None:
        statement = statement.where(events.c.collection == args.collection)

    if since is not None:
        statement = statement.where(events.c.ts >= since)

    if until is not None:
        statement = statement.where(events.c.ts < until)

    engine = make_engine(settings.database_url)

    migrate(engine)

    count = 0

    try:
        output: TextIO = (
            args.output.open("w", encoding="utf-8") if args.output else sys.stdout
        )

        try:
            with engine.connect() as connection:
                rows = connection.execution_options(yield_per=1000).execute(statement)

                for row in rows:
                    labels = dict(row.labels or {})

                    if not matches(labels, terms):
                        continue

                    line = {
                        "event": row.payload,
                        "labels": labels,
                        "token_id": str(row.token_id),
                        "received_at": row.received_at.isoformat() + "Z",
                    }

                    output.write(json.dumps(line, separators=(",", ":")) + "\n")
                    count += 1
        finally:
            if args.output:
                output.close()
    except OSError as error:
        raise CommandError(f"cannot write {args.output}: {error}") from error
    finally:
        engine.dispose()

    # The count goes to standard error, since standard output may be the file.
    print(f"exported {count} event(s)", file=sys.stderr)

    return 0


def command_key_generate(args: argparse.Namespace) -> int:
    """Print a fresh signing key."""

    print(generate_key())

    return 0


def command_token_issue(args: argparse.Namespace) -> int:
    """Sign a token from the options and print it with its id."""

    key = _key(args.key_file)
    labels = _labels(args.label)

    try:
        expires = parse_expiry(args.expires, now())
        token, claims = issue(
            key,
            name=args.name,
            expires=expires,
            labels=labels,
            origins=args.origin,
            scopes=args.scope or DEFAULT_SCOPES,
        )
    except TokenError as error:
        raise CommandError(str(error)) from error

    if args.json:
        print(json.dumps({"token": token, "claims": claims.as_dict()}, indent=2))
    else:
        print(f"token: {token}")
        print(f"jti: {claims.jti}")
        print(f"expires: {expiry_text(claims)}")

    return 0


def command_token_inspect(args: argparse.Namespace) -> int:
    """Decode a token's claims."""

    token = sys.stdin.read() if args.token == "-" else args.token

    try:
        claims = inspect(token.strip())
    except TokenError as error:
        raise CommandError(str(error)) from error

    print(json.dumps(claims.as_dict(), indent=2))

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; returns the exit status."""

    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        ("serve", None): command_serve,
        ("migrate", None): command_migrate,
        ("rebuild", None): command_rebuild,
        ("import", None): command_import,
        ("export", None): command_export,
        ("key", "generate"): command_key_generate,
        ("token", "issue"): command_token_issue,
        ("token", "inspect"): command_token_inspect,
    }
    subcommand = getattr(args, "key_command", None) or getattr(
        args, "token_command", None
    )
    handler = handlers[(args.command, subcommand)]

    try:
        return handler(args)
    except CommandError as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)

        return 2


if __name__ == "__main__":
    sys.exit(main())
