"""The title of the collection a session belongs to.

From extension 0.2.2 a collection index may declare an `id`, which the
events carry as `collection_id` beside the subscription in
`collection`, and a `collection_title` for display. The identity the
service keys on goes in the existing `collection` column, the id when
there is one; this column holds the title. Existing rows read empty
until `rebuild` fills them from the stored events.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column, empty for every existing session."""

    op.add_column(
        "sessions",
        sa.Column(
            "collection_title", sa.String(255), nullable=False, server_default=""
        ),
    )


def downgrade() -> None:
    """Drop the column again."""

    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("collection_title")
