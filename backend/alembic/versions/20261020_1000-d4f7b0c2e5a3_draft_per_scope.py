"""one live draft per visit and scope

Revision ID: d4f7b0c2e5a3
Revises: c3e6a9b1d4f2
Create Date: 2026-10-20 10:00:00.000000+00:00

A stay's IPD draft may be open while the bill for what the visit owed before
the admission is issued again (after it was cancelled to correct it). That
OPD bill passes through a draft of its own for a moment, so the rule "one live
draft per visit" becomes "one live draft per visit and scope". Two staff
members still cannot each build half of the same bill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f7b0c2e5a3"
down_revision: str | None = "c3e6a9b1d4f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIVE_DRAFT = sa.text("deleted_at IS NULL AND status = 'DRAFT'")


def upgrade() -> None:
    op.drop_index("uq_invoices_encounter_draft", table_name="invoices")
    op.create_index(
        "uq_invoices_encounter_draft",
        "invoices",
        ["encounter_id", "scope"],
        unique=True,
        postgresql_where=_LIVE_DRAFT,
    )


def downgrade() -> None:
    op.drop_index("uq_invoices_encounter_draft", table_name="invoices")
    op.create_index(
        "uq_invoices_encounter_draft",
        "invoices",
        ["encounter_id"],
        unique=True,
        postgresql_where=_LIVE_DRAFT,
    )
