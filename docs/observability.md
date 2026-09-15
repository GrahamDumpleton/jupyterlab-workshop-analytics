# Observability

The service is instrumented with [wrapture](https://wrapture.readthedocs.io),
and what is traced is decided in a TOML file rather than in the code.
`wrapture.toml` in the repository root is the local development
configuration; a deployment mounts its own copy, so what is traced in
production changes without a rebuild.

## How it is applied

`workshop-analytics serve` performs wrapture's manual setup: when
`WRAPTURE_CONFIG` names a file, the command loads and applies it
before importing the application, so the patches the file asks for
are in place before the modules they cover are imported. `just serve`
sets the variable to the repository's file. With the variable unset
the service runs untraced and the three markers embedded in the code
cost nothing. There is no `python -m wrapture` runner and no autowrapt
injection, since the service owns its own startup and runs one
process.

## What the shipped file records

Three layers, with one rule each:

- **Third-party layers** come from `[[instrument]]` entries naming
  wrapture-instrumentation's packaged instrumentations, kept current
  by their authors: `fastapi` for the request boundary with the
  matched route and the endpoint beneath it, `sqlalchemy` for each
  statement as a leaf carrying the dialect and the operation, `sqlite3`
  beneath it while SQLite is the store, and `jinja2` for the
  dashboard's template renders, with its loading pipeline switched
  off. The `fastapi` entry ignores `/healthz` and `/static/*` and
  masks a `token` query parameter over and above wrapture's built-in
  sensitive names. Each package declares its aspects, the groups of
  call sites it binds, with their own capture defaults: the `fastapi`
  package's `views` aspect records an endpoint's arguments as types
  and its result as a shape, so a route returning the live payload
  records `<dict 3 keys>` and never the sessions in it, and an entry
  can override that under `[instrument.views]`.

- **The service's own call sites** come from `[[observe]]` entries by
  module: the ingest pipeline, the projection and the store's writes
  at the granularity of a batch (the per-event helpers beneath them
  would add a span per event and say nothing a batch's span does
  not), the broadcaster, the store's `migrate`, so the statements
  that bring the schema up to date at startup nest under one call
  instead of each standing as its own root, the query layer's
  questions, one span per question with the filters as its
  arguments and the statements it ran beneath it, and the SQL tool's
  runs with the analyst's statement as an argument. Dropping or
  adding a call site needs no code change. The top-level `capture =
  "summary"` keeps recorded values bounded, the batch itself is
  redacted where it is an argument, and the functions that return
  bulk data, the parser returning the batch, `live_rows` returning
  the page's sessions and the questions returning whole reports,
  have `capture_result = "shape"`, so the call is seen with the size
  of what it returned and the data is not.

- **What only the code knows** is embedded, because nothing outside
  can place it: `block("ingest.parse")`, `block("ingest.store")` and
  `block("ingest.broadcast")` mark the phases inside one batch;
  `annotate(received=, stored=, duplicates=, rejected=)` puts the
  counts on the pipeline's event; and a body that is not a batch is
  noted against the request with `note_exception()` when it is turned
  into a 400 rather than raised. All three are inert when no sink is
  active.

A `[[log]]` entry captures the service's own loggers at INFO, so the
warning for a rejected event, or for a refused token, arrives nested
under the request that carried it.

A request for a batch reads like this in the printer:

```
POST /events (fastapi.applications:FastAPI.__call__)  -> '202 Accepted'
  workshop_analytics.ingest:Ingest.accept(...)  -> IngestResult(received=25, stored=25, ...)
    block: ingest.parse
    block: ingest.store
      workshop_analytics.store.writes:insert_events(...)
      sqlalchemy.engine.default:DefaultDialect.do_execute(...)
      workshop_analytics.projection:apply_events(...)
    block: ingest.broadcast
      workshop_analytics.live:Broadcaster.publish(...)
```

## Where it goes

The `[otel]` table exports all three signals over OTLP, with
`service_name = "workshop-analytics"`. Its `[otel.environment]`
defaults point at `http://localhost:4318` with `http/protobuf`, which
is [otel-desktop-viewer](https://github.com/CtrlSpice/otel-desktop-viewer)
on a laptop: `just otel` starts it, and its UI is on port 8000. A real
`OTEL_EXPORTER_OTLP_ENDPOINT` in the environment overrides the
default, which is how a deployment points at the cluster's collector
without editing the file.

A call to an MCP tool is a `POST /mcp` request like any other, with
the tool's question and its statements nested beneath it.

Each request arrives in the viewer as one trace: a SERVER span named
`POST /events` by its route, the pipeline and its phases nested
beneath with the counts as attributes, each statement a CLIENT span
with `db.system.name` and `db.operation.name`, and the log lines
correlated with the request. The metrics signal aggregates request
durations by method, status and route, and call durations per bound
path. A batch that was refused shows the exception on the request
span.

The `[[sink]]` printer at the end of the file is a shallow live view
for the terminal while developing, four levels deep, enough to show a
request, its endpoint, the pipeline and its phases; a deployed file
leaves it out.

## What is left out, and why

Bound parameters are never recorded by the database instrumentations,
under any setting, and SQL text reduces to its length unless the
`statement` setting is turned on; the service's statements are all
compiled from the expression language, so turning it on is safe, and
off is the shipped default. The token never reaches a trace: it
travels as a header, which the request middleware does not record,
and the query fallback is masked by name. Event bodies are not
captured: the pipeline's batch argument is redacted, the parser's,
the live view's and the query layer's results record as shapes, the
pipeline's result leaves its session deltas out of its repr, and the
endpoints record their results as shapes under the `fastapi`
package's default. The dashboard's render context is masked wholesale
by the Jinja2 instrumentation.

## In the tests

`tests/conftest.py` enables `wrapture.pytest_plugin`, so every test
gets the leak sweep and the `tape` fixture, and a failing test's
report shows the call tree. Tests of the request pipeline apply the
packaged `fastapi` instrumentation with `wrapture.instrumentation()`
around the app factory and assert on the request event, the phase
blocks and their annotations; one of them asserts that the request
event is the parent of the pipeline's call across the thread pool
hop, which is the check that the trace stays one tree. The projection
test compares `wrapture.canonical()` of its call tree with the golden
file under `tests/golden/`; set `UPDATE_GOLDEN=1` to rewrite it after
a deliberate change.
