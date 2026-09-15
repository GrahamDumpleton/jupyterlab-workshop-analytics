# Development

## The environment

Python 3.14, managed with [uv](https://docs.astral.sh/uv/). `just
install` runs `uv sync`, which creates `.venv` from the lock file with
the dev group. Everything runs through `uv run`, and the Justfile
wraps the common tasks; `just --list` shows them.

| Target | What it does |
| ------ | ------------ |
| `just serve` | Runs the service with `wrapture.toml` applied. Needs `TOKEN_SIGNING_KEY`. The OpenAPI document is at `/docs`. |
| `just test` | Runs pytest; arguments pass through, so `just test tests/test_ingest.py -k labels` works. |
| `just lint`, `just format` | ruff, checking and fixing. |
| `just typecheck` | mypy, strict, over the package. |
| `just migrate`, `just rebuild`, `just import <file>` | The store commands. |
| `just key-generate`, `just token-issue ...`, `just token-inspect <token>` | The credential commands. |
| `just otel` | Starts otel-desktop-viewer to receive the traces. |

## Settings

Every setting is an environment variable, read once at startup by
`Settings.from_environment()`.

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `DATABASE_URL` | `sqlite:///analytics.db` | The SQLAlchemy URL of the store. The container uses `sqlite:////data/analytics.db`. |
| `TOKEN_SIGNING_KEY` | none, required to serve | The base64 key every token is signed with. |
| `DENIED_TOKENS_FILE` | none | A file of revoked token ids, one per line. |
| `ALLOWED_ORIGINS` | none | Browser origins allowed to post cross-origin under any token, comma separated. |
| `ACTIVE_ALLOWANCE` | `150` | Seconds since the last event or visible heartbeat within which a session is active. |
| `AWAY_ALLOWANCE` | `750` | Seconds since a hidden heartbeat within which a session is away. |
| `SILENT_LIMIT` | `1800` | Seconds of silence after which a session is lost. |
| `LIVE_LINGER` | `300` | Seconds a finished or abandoned session stays on the live view. |
| `MAX_BODY_BYTES` | `2097152` | The largest batch body accepted. |
| `MAX_BATCH` | `1000` | The most events read from one batch. |
| `RATE_LIMIT_PER_MINUTE` | `120` | Batches one token may post per minute. |
| `SESSION_HOURS` | `12` | How long a dashboard cookie lasts. |
| `COOKIE_SECURE` | unset | Force the cookie's Secure flag on or off; unset follows the request's scheme. |
| `SQL_TOOL` | on | `off` disables the read-only SQL tool on `/api/sql` and `/mcp`. |
| `SQL_TIMEOUT` | `10` | Seconds a SQL statement may run before it is stopped. |
| `SQL_MAX_ROWS` | `1000` | The most rows a SQL statement returns. |
| `WRAPTURE_CONFIG` | unset | A wrapture configuration for `serve` to apply. |

## The dashboard's static files

The pages link `live.js` and `live.css` with a query string that is a
digest of the static directory, computed once at startup, so a browser
that cached one version fetches the next after the service restarts.
Templates are re-read on change without a restart; the script and
stylesheet need one, or a hard reload, since the page's header row is
built by the script and the row template by the markup, and the two
must match.

## The store

SQLAlchemy Core, never the ORM. `store/tables.py` declares two
tables: `events`, the raw events with the fields worth filtering on
extracted into indexed columns and the whole event as JSON, never
updated; and `sessions`, one row per session derived from them. The
hash on `events` is SHA-256 over the event with sorted keys and
`labels` left out, which is what makes a resent batch insert nothing.

Migrations are Alembic, under `store/migrations/`, run by `serve` at
start and by the `migrate` command; `store.migrate()` builds the
Alembic configuration itself, so there is no `alembic.ini`. A new
migration is written by hand into `versions/` (the initial one is the
model) and its revision chained to the previous; a test compares the
migrated schema with the declared tables, so a column added to
`tables.py` without a migration fails the suite.

SQLite runs in WAL mode with `synchronous=NORMAL`. The store is
synchronous: endpoints that touch it are awaited through Starlette's
thread pool, one code path for every database and plain tests.
PostgreSQL later is a `DATABASE_URL` change plus the `postgres` extra.

## The projection

`projection.py` folds a session's events, in `seq` order, into its
row, and recomputes the row from every stored event of the session
each time a batch touches it, in the same transaction that stores the
batch. Status is not stored; `status_of()` derives it at read time
from the timestamps and the thresholds above. `rebuild` replays the
whole table, so a change to what a row holds is a code change, a new
migration for any new column, and a rebuild.

Completeness is derived from `seq`: expected is the terminal event's
`seq` when a finish or abandon arrived and otherwise the highest seen;
gaps are the ranges missing below it; complete means a terminal event
arrived and nothing is missing.

## The tests

`just test` runs pytest over `tests/`. The suite is driven by the
wrapture pytest plugin ([observability](observability.md#in-the-tests)),
and async tests run on asyncio through the anyio plugin, speaking
ASGI to the app in-process through httpx so the test's recording
context flows into the request.

`tests/fixtures/` holds `events.jsonl` files from real runs of the
showcase and example workshops under the extension's self-test,
recorded by extension 0.2.0. The projection tests derive the gap,
missing-tail and missing-head cases by dropping lines from them, and
`shifted_to_now()` in `conftest.py` moves a fixture's timestamps so a
session reads as live whenever the test runs. The `seeded` fixture
fills a store with every shape the query and API tests need from the
three recordings: a complete session posted under a labelled token, a
gapped one, a headless one, a lost one, a live one, a two-session
chain with an identified learner, the same workshop under two
collections, and a collection two instances took. To record a new
fixture, run a workshop's self-test in place and take the
`_workshop/events.jsonl` it leaves:

```console
$ jupyter workshop test path/to/workshop --in-place
$ cp path/to/workshop/_workshop/events.jsonl tests/fixtures/<name>.jsonl
```

## The query layer

`queries.py` holds every question as a function taking a connection,
the typed `Filters` and the settings, and returning plain dataclasses.
The routes in `api/query.py` are thin wrappers that parse the query
string, run the function on the thread pool and return its result, so
FastAPI derives the OpenAPI document from the same dataclasses the
answers are built from. The MCP tools of a later phase wrap the same
functions, so the two can never answer differently.

Sessions are narrowed by the indexed columns in SQL and by what only
Python knows afterwards: the label selector, since labels are JSON,
and the status, since it is derived at read time. Chaining sessions
into journeys, the funnel, the percentiles and the data quality note
are computed in Python over the selected rows, one code path for
SQLite and PostgreSQL. A new question is a function in `queries.py`,
a route in `api/query.py`, a tool in `mcp.py`, a test in
`tests/test_queries.py`, its observe entry in `wrapture.toml` if it
is a top-level question, and its row on the [API](api.md) and
[MCP](mcp.md) pages.

`mcp.py` builds the MCP server from the official SDK, one tool per
question, and mounts its transport at `/mcp` behind the same bearer
check as the API; the application's lifespan runs the transport's
session manager. `sql.py` is the read-only SQL tool on a second
engine, with the statement guard, the deadline and the row cap; the
route and the tool both call it.

## The vendored schema

`src/workshop_analytics/schema/events.schema.json` is the extension's
`events.schema.json`, copied at a release and never edited here.
`SCHEMA_VERSION` beside it records which release. When the extension
publishes a new one, copy the file from
`packages/core/src/schema/events.schema.json` in the extension
checkout, set the version, and run the suite: the fixtures validate
against the schema and every kind's fields are checked, so a change
in the contract shows up here as failing tests rather than as
rejected batches in production.
