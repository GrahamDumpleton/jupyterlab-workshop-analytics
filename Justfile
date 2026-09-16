# Development tasks for jupyterlab-workshop-analytics.
#
# Python is managed with uv. Run `just --list` to see the targets.

set positional-arguments

# List the available targets.
default:
    @just --list

# Set up the development environment.
install:
    uv sync

# Run the service locally with the repository's wrapture.toml applied (needs TOKEN_SIGNING_KEY).
serve *args:
    WRAPTURE_CONFIG=wrapture.toml uv run workshop-analytics serve "$@"

# Run the test suite; extra arguments pass through to pytest.
test *args:
    uv run pytest "$@"

# Check Python with the ruff linter and formatter.
lint:
    uv run ruff check .
    uv run ruff format --check .

# Reformat and apply auto-fixes.
format:
    uv run ruff check --fix .
    uv run ruff format .

# Type check with mypy.
typecheck:
    uv run mypy

# Bring the database up to the latest schema.
migrate:
    uv run workshop-analytics migrate

# Recompute every session from the stored events.
rebuild:
    uv run workshop-analytics rebuild

# Feed an events.jsonl file through the sink's pipeline, e.g. `just import tests/fixtures/hello-jupyterlab.jsonl`.
import *args:
    uv run workshop-analytics import "$@"

# Write the stored events as JSON lines that import reads back, e.g. `just export --output events.jsonl`.
export *args:
    uv run workshop-analytics export "$@"

# Print a fresh signing key for TOKEN_SIGNING_KEY.
key-generate:
    uv run workshop-analytics key generate

# Issue a token, e.g. `just token-issue --name showcase --expires 90d --label deployment=showcase`.
token-issue *args:
    uv run workshop-analytics token issue "$@"

# Decode a token's claims.
token-inspect token:
    uv run workshop-analytics token inspect "$1"

# Start otel-desktop-viewer to receive the service's traces, metrics and logs.
otel:
    otel-desktop-viewer

# Remove build and tool caches.
clean:
    rm -rf dist .pytest_cache .mypy_cache .ruff_cache
    find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +

# Build the container image from deploy/Dockerfile as jupyterlab-workshop-analytics:local.
image:
    docker build -f deploy/Dockerfile -t jupyterlab-workshop-analytics:local .

# Run the local image on port 8080 with a fresh key and a named volume for the store.
image-run:
    docker run --rm -p 8080:8080 -v workshop-analytics-data:/data -e TOKEN_SIGNING_KEY="$(uv run workshop-analytics key generate)" jupyterlab-workshop-analytics:local

# Render a Kubernetes overlay, e.g. `just manifests local`.
manifests overlay="local":
    kubectl kustomize deploy/kubernetes/overlays/{{overlay}}
