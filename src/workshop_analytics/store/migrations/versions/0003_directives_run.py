"""The directives each session ran, by id.

The page list a session started with carries, from extension 0.2.1,
the directives on each page; this column holds the ids the session's
events reported running, so a report can say what was never run
without reading the events again. Existing rows read empty until
`rebuild` fills them.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column, empty for every existing session."""

    op.add_column(
        "sessions",
        sa.Column("directives_run", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    """Drop the column again."""

    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("directives_run")
