"""Storage: the engine, the tables, the migrations and the writes."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from .engine import is_sqlite, make_engine, sqlite_path
from .tables import events, metadata, sessions

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def alembic_config(url: str) -> Config:
    """An Alembic configuration pointed at the packaged migrations."""

    config = Config()

    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

    return config


def migrate(engine: Engine) -> None:
    """Bring the database up to the latest migration."""

    config = alembic_config(str(engine.url.render_as_string(hide_password=False)))

    config.attributes["connection"] = None

    with engine.begin() as connection:
        config.attributes["connection"] = connection

        command.upgrade(config, "head")


__all__ = [
    "MIGRATIONS_DIR",
    "alembic_config",
    "events",
    "is_sqlite",
    "make_engine",
    "metadata",
    "migrate",
    "sessions",
    "sqlite_path",
]
