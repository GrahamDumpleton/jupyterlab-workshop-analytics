"""The sessions' count of gates skipped.

A session that moved past unmet requirements under soft gating is
counted here, so a finish the workshop's checks did not confirm can
be told from one they did. Existing rows read zero until `rebuild`
recounts them from the events.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column, zero for every existing session."""

    op.add_column(
        "sessions",
        sa.Column("gates_skipped", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Drop the column again."""

    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("gates_skipped")
