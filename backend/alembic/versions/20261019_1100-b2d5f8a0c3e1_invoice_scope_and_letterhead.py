"""invoice scope and letterhead

Revision ID: b2d5f8a0c3e1
Revises: a1c4e7b9d2f0
Create Date: 2026-10-19 11:00:00.000000+00:00

`invoices.scope` — OPD or IPD. The two kinds of bill have different owners: the
counter makes and freezes OPD bills, the accountant corrects and finalises IPD
ones. Existing invoices are backfilled: IPD when the visit has an admission and
the invoice was created after it, OPD otherwise (a consultation billed before an
admission on the same visit stays OPD).

`invoices.letterhead` — the hospital's identity snapshotted at issue (filled by
the logo change), so reprints match the day of issue. Null on existing rows,
which print from the live profile.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2d5f8a0c3e1"
down_revision: str | None = "a1c4e7b9d2f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE = postgresql.ENUM("OPD", "IPD", name="invoicescope", create_type=False)


def upgrade() -> None:
    _SCOPE.create(op.get_bind(), checkfirst=True)
    op.add_column("invoices", sa.Column("scope", _SCOPE, nullable=False, server_default="OPD"))
    op.create_index(op.f("ix_invoices_scope"), "invoices", ["scope"], unique=False)
    op.add_column(
        "invoices",
        sa.Column("letterhead", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        """
        UPDATE invoices i
           SET scope = 'IPD'
          FROM admissions a
         WHERE a.encounter_id = i.encounter_id
           AND a.status <> 'CANCELLED'
           AND a.deleted_at IS NULL
           AND i.created_at >= a.admitted_at
        """
    )
    # The default only fills existing rows; the application always sets it.
    op.alter_column("invoices", "scope", server_default=None)


def downgrade() -> None:
    op.drop_column("invoices", "letterhead")
    op.drop_index(op.f("ix_invoices_scope"), table_name="invoices")
    op.drop_column("invoices", "scope")
    _SCOPE.drop(op.get_bind(), checkfirst=True)
