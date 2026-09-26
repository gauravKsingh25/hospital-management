"""queue entry position

Revision ID: c7a1e5f2d9b3
Revises: b4f9d20ac6e1
Create Date: 2026-09-14 12:00:00.000000+00:00

The OPD queue's order used to be *computed* — priority tier, then arrival —
which meant there was nothing for reception to change when the order was
wrong. This stores it. `position` is an integer rank within one doctor's day,
seeded here for every existing token from the order that was in force, so the
board reads exactly as it did the moment before this ran and changes only when
somebody drags a row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7a1e5f2d9b3"
down_revision: str | None = "b4f9d20ac6e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "queue_entries",
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    # Backfill from the old computed order, per doctor per day. Closed tokens
    # are numbered too — cheap, and it keeps history sortable.
    op.execute(
        sa.text(
            """
            UPDATE queue_entries AS q
               SET position = ranked.rn
              FROM (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY hospital_id, doctor_id, queue_date
                               ORDER BY priority, checked_in_at
                           ) AS rn
                      FROM queue_entries
                     WHERE deleted_at IS NULL
                   ) AS ranked
             WHERE q.id = ranked.id
            """
        )
    )
    op.alter_column("queue_entries", "position", server_default=None)


def downgrade() -> None:
    op.drop_column("queue_entries", "position")
