"""The store: migrations match the table definitions, and SQLite is tuned."""

from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text

from workshop_analytics.config import Settings
from workshop_analytics.store import make_engine, migrate, sqlite_path
from workshop_analytics.store.tables import metadata


def test_the_migrations_produce_the_declared_tables(engine: Engine) -> None:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diff = compare_metadata(context, metadata)

    assert diff == []


def test_migrating_twice_is_harmless(engine: Engine) -> None:
    migrate(engine)

    with engine.connect() as connection:
        version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()

    assert version == "0003"


def test_a_file_database_runs_in_wal_mode(settings: Settings) -> None:
    engine = make_engine(settings.database_url)

    with engine.connect() as connection:
        journal = connection.execute(text("PRAGMA journal_mode")).scalar()
        synchronous = connection.execute(text("PRAGMA synchronous")).scalar()

    engine.dispose()

    assert journal == "wal"
    assert synchronous == 1
    assert sqlite_path(settings.database_url) == Path(
        settings.database_url.removeprefix("sqlite:///")
    )


def test_an_in_memory_database_is_shared_across_connections() -> None:
    engine = make_engine("sqlite://")

    migrate(engine)

    with engine.connect() as first, engine.connect() as second:
        first.execute(text("INSERT INTO alembic_version VALUES ('x')"))
        first.commit()

        rows = second.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar()

    engine.dispose()

    assert rows == 2
    assert sqlite_path("sqlite://") is None
