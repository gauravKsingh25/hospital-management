"""drug allergy overrides

Revision ID: f6b9d2e4a7c5
Revises: e5a8c1d3f6b4
Create Date: 2026-10-30 10:00:00.000000+00:00

SAFE-01: a drug that clashes with a recorded drug allergy is refused unless the
prescriber gives a reason. The reason is kept on the order it allowed — the
ward chart's `medication_orders` and the OPD's `orders` — together with which
allergies the doctor was shown, so one recorded afterwards still stops the dose.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "f6b9d2e4a7c5"
down_revision: str | None = "e5a8c1d3f6b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "medication_orders",
        sa.Column(
            "allergy_override_reason", sqlmodel.sql.sqltypes.AutoString(length=300), nullable=True
        ),
    )
    op.add_column(
        "medication_orders",
        # `[]` on every existing order: none of them overrode anything.
        sa.Column(
            "allergy_acknowledged_ids", sa.JSON(), server_default=sa.text("'[]'"), nullable=True
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "allergy_override_reason", sqlmodel.sql.sqltypes.AutoString(length=300), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("orders", "allergy_override_reason")
    op.drop_column("medication_orders", "allergy_acknowledged_ids")
    op.drop_column("medication_orders", "allergy_override_reason")
