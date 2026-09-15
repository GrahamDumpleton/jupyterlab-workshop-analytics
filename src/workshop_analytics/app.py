"""The ASGI application factory."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, mcp
from .api import dashboard, health, live, query, sink
from .config import Settings
from .ingest import Ingest, RateLimiter
from .live import Broadcaster
from .schema import EventValidator
from .sql import SqlTool
from .store import make_engine, migrate
from .tokens import DenyList, decode_key

DASHBOARD_DIR = Path(__file__).with_name("dashboard")


def assets_version(directory: Path) -> str:
    """A short digest of the static files, for cache-busting their URLs.

    The pages link their script and stylesheet with this as a query
    string, so a browser that cached one version fetches the next
    after a restart rather than running old script against new markup.
    """

    digest = hashlib.sha256()

    for path in sorted(directory.iterdir()):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())

    return digest.hexdigest()[:12]


def create_app(
    settings: Settings | None = None, *, run_migrations: bool = True
) -> FastAPI:
    """Build the application from settings, migrating the store first.

    The signing key is decoded here so a bad or missing key fails at
    startup, plainly, rather than on the first request.
    """

    settings = settings or Settings.from_environment()
    key = decode_key(settings.signing_key)
    engine = make_engine(settings.database_url)

    if run_migrations:
        migrate(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The MCP transport's session manager lives for the application;
        # mounting its app does not run its own lifespan.
        async with app.state.mcp.session_manager.run():
            yield

        engine.dispose()

        if app.state.sql.engine is not None:
            app.state.sql.engine.dispose()

    app = FastAPI(
        title="jupyterlab-workshop analytics",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    app.state.settings = settings
    app.state.signing_key = key
    app.state.denied = DenyList(settings.denied_tokens_file)
    app.state.engine = engine
    app.state.validator = EventValidator()
    app.state.broadcaster = Broadcaster()
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.ingest = Ingest(
        engine=engine,
        settings=settings,
        validator=app.state.validator,
        broadcaster=app.state.broadcaster,
    )
    app.state.templates = Jinja2Templates(directory=str(DASHBOARD_DIR / "templates"))
    app.state.templates.env.filters["duration"] = dashboard.duration_text
    app.state.templates.env.filters["detail"] = dashboard.detail_text
    app.state.assets_version = assets_version(DASHBOARD_DIR / "static")
    app.state.sql = SqlTool(settings)

    app.include_router(health.router)
    app.include_router(sink.router)
    app.include_router(live.router)
    app.include_router(query.router)
    app.include_router(dashboard.router)
    app.state.mcp = mcp.mount(app)
    app.mount(
        "/static", StaticFiles(directory=str(DASHBOARD_DIR / "static")), name="static"
    )

    return app
