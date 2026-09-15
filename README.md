# jupyterlab-workshop-analytics

The service that receives the progress events
[jupyterlab-workshop](https://github.com/GrahamDumpleton/jupyterlab-workshop)
workshops report, stores them, shows a class in progress live, and
answers questions about what happened. Workshops post batches of
events to it from wherever they run, a JupyterHub, a Binder image, a
JupyterLite site or a laptop; a supervisor watches the sessions move
through the pages on one page; and an analyst asks the query API
where learners stop, how long pages take and which checks they fail.

It is a Python service, FastAPI on uvicorn with SQLite behind it, run
from a checkout for development and as a container image in
deployment. It is not published to PyPI.

## Running it locally

```console
$ just install
$ export TOKEN_SIGNING_KEY=$(just key-generate)
$ just serve
```

The service listens on `http://127.0.0.1:8080`. Give a workshop
deployment a token to post with, and yourself one to watch with:

```console
$ just token-issue --name my-class --expires 90d --label course=intro
$ just token-issue --name me --expires 30d --scope dashboard
$ just token-issue --name analyst --expires 30d --scope api
```

Put the first in the deployment's `analytics` block as `token`, with
`sink` set to `http://127.0.0.1:8080/events`; paste the second into
the login form at `http://127.0.0.1:8080/`; send the third as a bearer
token to the query API, whose document is at
`http://127.0.0.1:8080/docs`, or give it to an assistant as the MCP
server at `http://127.0.0.1:8080/mcp`. `just import
tests/fixtures/hello-jupyterlab.jsonl` seeds the store with a recorded
run when there is no workshop to hand.

## Documentation

- [Tokens](docs/tokens.md): the signing key, issuing and revoking
  tokens, scopes and token-bound labels.

- [The dashboard](docs/dashboard.md): signing in, the label selector,
  what each status means, and a session's own page.

- [The query API](docs/api.md): the routes, the filters, workshop
  identity, journeys and outcomes, completeness and data quality, and
  the read-only SQL tool.

- [The MCP server](docs/mcp.md): connecting an assistant to `/mcp`,
  the tools, and the reporting skill under `skills/`.

- [The command line](docs/cli.md): every `workshop-analytics`
  command.

- [Observability](docs/observability.md): what `wrapture.toml`
  traces and how to see it in otel-desktop-viewer.

- [Development](docs/development.md): the environment, the tests, and
  refreshing the vendored event schema.

The event contract itself, the fields every event carries and what
each kind adds, is documented by the extension under
[progress events](https://jupyterlab-workshop.readthedocs.io/en/latest/analytics.html).
