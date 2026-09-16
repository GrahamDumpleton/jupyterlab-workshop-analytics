# Agent guidance for jupyterlab-workshop-analytics

## Project

jupyterlab-workshop-analytics is the service that receives the
progress events that [jupyterlab-workshop](https://github.com/GrahamDumpleton/jupyterlab-workshop)
workshops report, stores them, projects them into sessions, shows the
sessions live for a supervised class, and answers questions about them
through a REST API and an MCP server. The extension repository owns
the contract: what an event is (`events.schema.json`, vendored here
under `src/workshop_analytics/schema/`) and how a deployment names a
sink. This repository owns everything after the event arrives.

The service serves any way workshops are hosted: a JupyterHub, a
JupyterLite site, a Binder image, a plain JupyterLab pointed at a
sink. Nothing in it assumes one of them. It is not published to PyPI:
it is a git checkout for development and a container image for
deployment.

Layout:

- `src/workshop_analytics/` is the package. `cli.py` is the
  `workshop-analytics` command; `app.py` the ASGI factory; `ingest.py`
  the sink pipeline shared by the endpoint and the `import` command;
  `projection.py` the sessions projection and its status rules;
  `live.py` the broadcaster and the SSE stream; `queries.py` every
  question the query API answers, as functions returning dataclasses;
  `mcp.py` the same questions as MCP tools at `/mcp`; `sql.py` the
  read-only SQL tool; `tokens.py` the signed tokens; `store/` the
  SQLAlchemy Core tables, engine and Alembic migrations; `api/` the
  routers; `dashboard/` the Jinja2 templates and the one static
  script.

- `skills/jupyterlab-workshop-analytics/` is the reporting skill an
  assistant reads before asking the service questions. It stays short
  because the definitions come from `describe`.

- `tests/` is the pytest suite, driven by the wrapture pytest plugin.
  `tests/fixtures/` holds `events.jsonl` files from real workshop runs;
  `tests/golden/` holds the call-tree fingerprint the projection test
  compares against.

- `docs/` is the documentation, Markdown read on GitHub, with the
  README as the entry point. `wrapture.toml` is the local development
  observability configuration; `deploy/` holds what a deployment
  needs: the Dockerfile, the deployed `wrapture.toml` the image
  carries, and the Kubernetes base and overlays under
  `deploy/kubernetes/`. `.github/workflows/` runs the checks on every
  push and publishes the image on a version tag.

The `scratch/` directory is not part of the git repository. It holds
temporary working files; never reference it from anything committed.

## Tooling: uv and the Justfile

All Python environment and package management is done with
[uv](https://docs.astral.sh/uv/): `uv run <command>`, `uv add`,
`uv sync`. Never use the venv module, bare pip or `python -m build`.

The Justfile wraps the common tasks; prefer its targets and run
`just --list` to see them. `just install` syncs the environment,
`just test` runs the suite (arguments pass through to pytest),
`just lint` and `just typecheck` check ruff and mypy, `just format`
reformats, `just serve` runs the service with the repository's
`wrapture.toml` applied, `just otel` starts otel-desktop-viewer, and
`just key-generate`, `just token-issue`, `just import`, `just migrate`
and `just rebuild` wrap the CLI.

## Style

- Do not use emdashes in any files in this project. Rephrase with
  commas, parentheses, colons, or separate sentences instead.

- In bulleted lists where items run to multiple lines, put a blank
  line between the bullets, in docstrings, Markdown files, and any
  other prose. Be consistent within a list: if one item needs the
  spacing, space every item in that list, never a mix.

- Python code must always use type hints, on every function and
  method signature and on attributes and variables where the type is
  not obvious from the assignment. mypy runs strict.

- Use vertical white space liberally inside function and method
  bodies. Write code in paragraphs: group the statements that together
  perform one step and separate each group from the next with a blank
  line. Do not cram a body into one contiguous blob, and equally do
  not put a blank line between every single statement.

- Where it helps the reader, start a paragraph of code with a short
  comment saying what that step does or why it is needed, with a blank
  line between the comment and the code below it. Prefer one comment
  per logical block over line-by-line commentary.

- Put a blank line between a function or method docstring and the
  first line of code in the body.

- Every function, method or property that is part of the public API
  has a docstring saying what it does. Trivial accessors and dunder
  methods implementing standard protocols are the exceptions.

- Observability is declared in `wrapture.toml`, not in the code: the
  third-party layers come from `[[instrument]]` entries and the
  service's own modules from `[[observe]]` entries. The only wrapture
  calls in the code are `block()`, `annotate()` and
  `note_exception()`, for what only the code knows, and they must
  stay inert when nothing listens.

- A change to a route, a command, a claim or a setting carries its
  documentation in the same change, on the `docs/` page that covers
  it.

- The vendored `events.schema.json` is copied from the extension at a
  release and never edited here; `SCHEMA_VERSION` in
  `schema/__init__.py` records which. The copy is strict where the
  contract is; the service is not. A field the copy does not know is
  kept in the stored event and warned about once, never rejected, so
  an additive change to the contract lands here, and is rolled out,
  before the extension that sends it is released.

## Git

- Git commit messages must never include a co-authored-by agent
  message or any similar agent attribution trailer.

- An AI agent must never commit changes on its own initiative. Finish
  the piece of work, summarize it, and wait to be told to commit.
  Permission to commit applies only to the work it was given for; it
  does not carry forward to later steps of a multi-step plan, each of
  which needs its own review and its own instruction to commit.
  Uncommitted changes are how the review happens.

- `main` is the working branch. Version tags drive the container
  image build only; there is no package release.
