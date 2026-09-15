"""The Alembic environment: runs migrations on a connection the service hands in.

`workshop_analytics.store.migrate()` opens the connection and passes it
through `config.attributes["connection"]`; run from the `alembic`
command line instead, the environment connects from `sqlalchemy.url`.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from workshop_analytics.store.tables import metadata

config = context.config

target_metadata = metadata


def run_migrations_offline() -> None:
    """Emit the SQL for the migrations without a database."""

    url = config.get_main_option("sqlalchemy.url")

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run the migrations on the supplied or a freshly made connection."""

    connection = config.attributes.get("connection")

    if connection is not None:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()

        return

    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with engine.connect() as fresh:
        context.configure(
            connection=fresh, target_metadata=target_metadata, render_as_batch=True
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
