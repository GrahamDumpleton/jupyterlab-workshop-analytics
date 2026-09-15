# syntax=docker/dockerfile:1
#
# The service's container image, built from the repository root:
#
#     docker build -f deploy/Dockerfile -t jupyterlab-workshop-analytics .
#
# Two stages: uv resolves the locked dependencies into a virtual
# environment, and a plain Python image carries that environment and
# nothing else. The image holds the whole package, so every CLI command
# runs in a deployed container with the key and the database already in
# its environment, which is how tokens are issued in production.

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies first, so a change to the source does not redo them.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.14-slim-bookworm

# A non-root user, the SQLite volume, and the deployed observability
# configuration, which a cluster may mount its own copy over.
RUN groupadd --system --gid 1000 analytics \
    && useradd --system --uid 1000 --gid analytics --home /app analytics \
    && mkdir -p /data /etc/workshop-analytics \
    && chown analytics:analytics /data

COPY --from=build --chown=analytics:analytics /app /app
COPY deploy/wrapture.toml /etc/workshop-analytics/wrapture.toml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/analytics.db

USER analytics
WORKDIR /app
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"

CMD ["workshop-analytics", "serve", "--host", "0.0.0.0", "--port", "8080"]
